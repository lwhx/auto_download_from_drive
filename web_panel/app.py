from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, jsonify, request, session
from flask_socketio import SocketIO
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import os
import secrets
import threading
import subprocess
import time
import logging
import hmac
from functools import wraps
from datetime import datetime
from urllib.parse import urlparse
from rclone_monitor import get_all_transfers_progress

# ==================== Configuration ====================
app = Flask(__name__)
# Trust exactly 1 upstream proxy (Nginx) so request.remote_addr reflects the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Load environment variables for authentication and CORS
API_KEY = os.getenv('WEB_PANEL_API_KEY', '').strip()
ALLOWED_ORIGINS = os.getenv('WEB_PANEL_ALLOWED_ORIGINS', 'http://localhost,https://localhost')
LOG_LEVEL = os.getenv('WEB_PANEL_LOG_LEVEL', 'INFO').upper()
SESSION_TTL_SECONDS = int(os.getenv('WEB_PANEL_SESSION_TTL_SECONDS', '1800'))
SECRET_KEY = os.getenv('WEB_PANEL_SECRET_KEY', '').strip()

missing_required_settings = []
if not API_KEY:
    missing_required_settings.append('WEB_PANEL_API_KEY')
if not SECRET_KEY:
    missing_required_settings.append('WEB_PANEL_SECRET_KEY')
if missing_required_settings:
    missing_list = ', '.join(missing_required_settings)
    raise RuntimeError(f'Missing required environment variables: {missing_list}')

app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('web_panel')

# Configure CORS with restricted origins
cors_origins = [o.strip() for o in ALLOWED_ORIGINS.split(',')]
CORS(app, resources={r"/api/*": {"origins": cors_origins}})
socketio = SocketIO(app, cors_allowed_origins=cors_origins)
UNSAFE_HTTP_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
CSRF_SESSION_KEY = 'csrf_token'

# ==================== Brute-force protection ====================
AUTH_RATE_LIMIT_MAX_FAILURES    = int(os.getenv('WEB_PANEL_AUTH_MAX_FAILURES', '10'))
AUTH_RATE_LIMIT_WINDOW_SECONDS  = int(os.getenv('WEB_PANEL_AUTH_WINDOW_SECONDS', '600'))
AUTH_RATE_LIMIT_LOCKOUT_SECONDS = int(os.getenv('WEB_PANEL_AUTH_LOCKOUT_SECONDS', '900'))
AUTH_RATE_LIMIT_CLEANUP_INTERVAL = int(os.getenv('WEB_PANEL_AUTH_CLEANUP_INTERVAL', '300'))

# ==================== Log API protection ====================
LOG_API_MAX_LINES = int(os.getenv('WEB_PANEL_LOG_MAX_LINES', '10000'))

# ip -> {'count': int, 'window_start': float, 'locked_until': float}
_auth_failures: dict = {}
_auth_failures_lock = threading.Lock()
_auth_last_cleanup: float = 0.0


def _auth_cleanup_if_needed(now: float) -> None:
    """Remove stale entries from _auth_failures. Must be called under _auth_failures_lock."""
    global _auth_last_cleanup
    if now - _auth_last_cleanup < AUTH_RATE_LIMIT_CLEANUP_INTERVAL:
        return
    _auth_last_cleanup = now
    stale = [
        ip for ip, e in _auth_failures.items()
        if e['locked_until'] <= now and e['window_start'] + AUTH_RATE_LIMIT_WINDOW_SECONDS <= now
    ]
    for ip in stale:
        del _auth_failures[ip]


def _is_ip_rate_limited(ip: str, now: float) -> bool:
    with _auth_failures_lock:
        entry = _auth_failures.get(ip)
        return entry is not None and entry['locked_until'] > now


def _record_auth_failure(ip: str, now: float) -> None:
    with _auth_failures_lock:
        _auth_cleanup_if_needed(now)
        entry = _auth_failures.get(ip)
        if entry is None or now - entry['window_start'] > AUTH_RATE_LIMIT_WINDOW_SECONDS:
            _auth_failures[ip] = {'count': 1, 'window_start': now, 'locked_until': 0.0}
        else:
            entry['count'] += 1
            if entry['count'] >= AUTH_RATE_LIMIT_MAX_FAILURES:
                entry['locked_until'] = now + AUTH_RATE_LIMIT_LOCKOUT_SECONDS
                logger.warning(
                    'Auth lockout triggered for %s after %d failures; locked for %ds',
                    ip, entry['count'], AUTH_RATE_LIMIT_LOCKOUT_SECONDS
                )


def _clear_auth_failures(ip: str) -> None:
    with _auth_failures_lock:
        _auth_failures.pop(ip, None)


# ==================== Authentication ====================
def _has_valid_session():
    expires_at = session.get('api_auth_until', 0)
    return isinstance(expires_at, int) and expires_at > int(time.time())

def _refresh_authenticated_session(reissue_csrf=False):
    session['api_auth_until'] = int(time.time()) + SESSION_TTL_SECONDS
    csrf_token = session.get(CSRF_SESSION_KEY, '')
    if reissue_csrf or not isinstance(csrf_token, str) or not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = csrf_token
    return csrf_token

def _normalize_origin(origin):
    value = (origin or '').strip()
    if not value:
        return None

    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'

def _extract_request_origin():
    origin = _normalize_origin(request.headers.get('Origin', ''))
    if origin:
        return origin

    referer = request.headers.get('Referer', '')
    return _normalize_origin(referer)

def _allowed_request_origins():
    allowed = {_normalize_origin(origin) for origin in cors_origins}
    allowed.add(_normalize_origin(request.host_url))
    return {origin for origin in allowed if origin}

def _validate_session_csrf():
    request_origin = _extract_request_origin()
    if not request_origin or request_origin not in _allowed_request_origins():
        logger.warning(
            'Blocked unsafe session request with invalid origin from %s: %s %s origin=%s referer=%s',
            request.remote_addr,
            request.method,
            request.path,
            request.headers.get('Origin', ''),
            request.headers.get('Referer', '')
        )
        return False, (jsonify({'error': 'Forbidden - invalid request origin'}), 403)

    expected_token = session.get(CSRF_SESSION_KEY, '')
    provided_token = request.headers.get('X-CSRF-Token', '')
    if (
        not isinstance(expected_token, str) or
        not expected_token or
        not isinstance(provided_token, str) or
        not provided_token or
        not hmac.compare_digest(provided_token, expected_token)
    ):
        logger.warning(
            'Blocked unsafe session request with invalid CSRF token from %s: %s %s',
            request.remote_addr,
            request.method,
            request.path
        )
        return False, (jsonify({'error': 'Forbidden - invalid CSRF token'}), 403)

    return True, None

def _require_api_key_or_session():
    if _has_valid_session():
        # Sliding expiration to reduce frequent re-auth prompts.
        csrf_token = _refresh_authenticated_session()
        return {'auth_via': 'session', 'csrf_token': csrf_token}, None

    ip = request.remote_addr
    now = time.time()

    if _is_ip_rate_limited(ip, now):
        logger.warning('Rate-limited auth attempt from %s', ip)
        return None, (jsonify({'error': 'Too many failed attempts. Try again later.'}), 429)

    api_key = request.headers.get('X-API-Key', '')
    if not api_key or not hmac.compare_digest(api_key, API_KEY):
        _record_auth_failure(ip, now)
        logger.warning(f'Unauthorized API access attempt from {request.remote_addr}')
        return None, (jsonify({'error': 'Unauthorized - Invalid or missing API key'}), 401)

    _clear_auth_failures(ip)
    # Promote a valid API key auth to short-lived HttpOnly session.
    csrf_token = _refresh_authenticated_session(reissue_csrf=True)
    logger.info(f'API request from {request.remote_addr}: {request.method} {request.path}')
    return {'auth_via': 'api_key', 'csrf_token': csrf_token}, None


def require_api_key(f):
    """Decorator to require API Key authentication for protected routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_context, error = _require_api_key_or_session()
        if error:
            return error
        if request.method in UNSAFE_HTTP_METHODS and auth_context['auth_via'] == 'session':
            ok, csrf_error = _validate_session_csrf()
            if not ok:
                return csrf_error
        return f(*args, **kwargs)
    
    return decorated_function

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
STATE_PATH = os.path.join(BASE_DIR, 'sync_state.json')
LOG_PATH = os.path.join(BASE_DIR, 'sync.log')
TRANSFERS_PATH = os.path.join(BASE_DIR, 'active_transfers.json')
RUNTIME_STATUS_PATH = os.path.join(BASE_DIR, 'runtime_status.json')
SYNC_SERVICE_NAME = 'sync.service'
RCLONE_SERVICE_PREFIX = 'rclone-'

BLOCKED_PATH_PREFIXES = ('/etc', '/var', '/proc', '/sys', '/dev', '/boot', '/sbin', '/usr')


def _validate_rule_path(path_value, field_name):
    if not isinstance(path_value, str) or not path_value.strip():
        return False, f'{field_name} is required'

    path_value = path_value.strip()

    if not os.path.isabs(path_value):
        return False, f'{field_name} must be an absolute path'

    normalized = os.path.normpath(path_value)

    for prefix in BLOCKED_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + '/'):
            return False, f'{field_name} must not point to system directory: {prefix}'

    return True, None


def _normalize_config(config):
    normalized = dict(config or {})
    normalized['sync_service_name'] = SYNC_SERVICE_NAME

    rclone_service_name = str(normalized.get('rclone_service_name', '')).strip()
    if not rclone_service_name:
        return None, 'rclone_service_name is required'
    if not rclone_service_name.startswith(RCLONE_SERVICE_PREFIX):
        return None, f'rclone_service_name must start with "{RCLONE_SERVICE_PREFIX}"'

    normalized['rclone_service_name'] = rclone_service_name
    return normalized, None


def _run_systemctl(action, service_name, timeout=15):
    if action == 'restart' and service_name != SYNC_SERVICE_NAME:
        return False, f'systemctl restart is only allowed for {SYNC_SERVICE_NAME}'

    command = ['systemctl', action, service_name]

    if os.geteuid() == 0:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        err = (result.stderr or result.stdout or '').strip()
        return result.returncode == 0, err

    errors = []

    # Prefer sudoers-based NOPASSWD path for service operations.
    try:
        sudo_result = subprocess.run(
            ['sudo', '-n'] + command,
            capture_output=True, text=True, timeout=timeout
        )
        if sudo_result.returncode == 0:
            return True, ''
        sudo_err = (sudo_result.stderr or sudo_result.stdout or '').strip()
        if sudo_err:
            errors.append(sudo_err)
    except Exception as e:
        errors.append(str(e))

    # Fallback to direct systemctl (for environments that rely on polkit rules).
    try:
        direct_result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if direct_result.returncode == 0:
            return True, ''
        direct_err = (direct_result.stderr or direct_result.stdout or '').strip()
        if direct_err:
            errors.append(direct_err)
    except Exception as e:
        errors.append(str(e))

    deduped = []
    for msg in errors:
        if msg and msg not in deduped:
            deduped.append(msg)
    return False, ' | '.join(deduped)


def _read_runtime_status():
    if not os.path.exists(RUNTIME_STATUS_PATH):
        return None, 'runtime_status.json is missing'

    try:
        with open(RUNTIME_STATUS_PATH, 'r') as f:
            status = json.load(f)
    except Exception as e:
        return None, f'failed to read runtime_status.json: {e}'

    if not isinstance(status, dict):
        return None, 'runtime_status.json is invalid'

    active_downloads = int(status.get('active_downloads', 0))
    queued_downloads = int(status.get('queued_downloads', 0))
    status['active_downloads'] = active_downloads
    status['queued_downloads'] = queued_downloads
    status['service_restart_allowed'] = bool(
        status.get('service_restart_allowed', active_downloads == 0 and queued_downloads == 0)
    )
    return status, None


def _can_restart_sync_service():
    status, error = _read_runtime_status()
    if error:
        return False, f'无法确认当前下载是否空闲：{error}'

    if not status.get('service_restart_allowed', False):
        active_downloads = status.get('active_downloads', 0)
        queued_downloads = status.get('queued_downloads', 0)
        return False, (
            f'检测到下载任务未清空（active={active_downloads}, queued={queued_downloads}），'
            f'已跳过自动重启 {SYNC_SERVICE_NAME}'
        )

    return True, ''

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/auth', methods=['POST'])
def auth():
    auth_context, error = _require_api_key_or_session()
    if error:
        return error
    return jsonify({'success': True, 'csrf_token': auth_context['csrf_token']})

@app.route('/api/config', methods=['GET'])
@require_api_key
def get_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
        normalized_config, error = _normalize_config(config)
        if error:
            return jsonify({'error': error}), 500
        return jsonify(normalized_config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    try:
        new_config = request.json or {}
        if not isinstance(new_config, dict):
            return jsonify({'error': 'request body must be a JSON object'}), 400
        if new_config.get('scan_interval_seconds', 0) <= 0:
            return jsonify({'error': 'scan_interval_seconds must be > 0'}), 400
        if new_config.get('rclone_refresh_interval_seconds', 0) <= 0:
            return jsonify({'error': 'rclone_refresh_interval_seconds must be > 0'}), 400
        if new_config.get('max_concurrent_downloads', 0) < 1:
            return jsonify({'error': 'max_concurrent_downloads must be >= 1'}), 400
        if new_config.get('max_retry_count', -1) < 0:
            return jsonify({'error': 'max_retry_count must be >= 0'}), 400
        if new_config.get('bandwidth_limit_mbps', -1) < 0:
            return jsonify({'error': 'bandwidth_limit_mbps must be >= 0'}), 400

        for i, rule in enumerate(new_config.get('rules', [])):
            if not isinstance(rule, dict):
                return jsonify({'error': f'rules[{i}] must be an object'}), 400
            ok, err = _validate_rule_path(rule.get('source_path', ''), f'rules[{i}].source_path')
            if not ok:
                return jsonify({'error': err}), 400
            ok, err = _validate_rule_path(rule.get('dest_path', ''), f'rules[{i}].dest_path')
            if not ok:
                return jsonify({'error': err}), 400

        normalized_config, error = _normalize_config(new_config)
        if error:
            return jsonify({'error': error}), 400

        with open(CONFIG_PATH, 'w') as f:
            json.dump(normalized_config, f, indent=2)

        # 同步脚本 service 固定为 sync.service，但仅允许在下载与队列都空闲时自动 restart。
        sync_service = SYNC_SERVICE_NAME
        restart_msg = ''
        try:
            can_restart, reason = _can_restart_sync_service()
            if not can_restart:
                restart_msg = f'，但未自动重启 {sync_service}：{reason}'
            else:
                ok, err = _run_systemctl('restart', sync_service, timeout=15)
                if ok:
                    restart_msg = f'，已自动重启 {sync_service}'
                else:
                    restart_msg = f'，但重启 {sync_service} 失败：{err or "unknown error"}'
        except Exception as e:
            restart_msg = f'，但重启 {sync_service} 出错：{e}'

        return jsonify({'success': True, 'message': f'配置已保存{restart_msg}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/rules', methods=['POST'])
@require_api_key
def add_config_rule():
    try:
        payload = request.json or {}
        source_path = (payload.get('source_path') or '').strip()
        dest_path = (payload.get('dest_path') or '').strip()

        ok, err = _validate_rule_path(source_path, 'source_path')
        if not ok:
            return jsonify({'error': err}), 400
        ok, err = _validate_rule_path(dest_path, 'dest_path')
        if not ok:
            return jsonify({'error': err}), 400

        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)

        if 'rules' not in config:
            config['rules'] = []
        if not isinstance(config['rules'], list):
            return jsonify({'error': 'config.rules must be an array'}), 400

        new_rule = {
            'source_path': source_path,
            'dest_path': dest_path,
            'enabled': True,
            '_comment': 'Set enabled=true after validating source and destination paths.'
        }
        config['rules'].append(new_rule)

        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

        return jsonify({'success': True, 'message': '规则已添加', 'rule': new_rule})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/rules/<int:rule_index>', methods=['DELETE'])
@require_api_key
def delete_config_rule(rule_index):
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)

        rules = config.get('rules', [])
        if not isinstance(rules, list):
            return jsonify({'error': 'config.rules must be an array'}), 400
        if rule_index < 0 or rule_index >= len(rules):
            return jsonify({'error': 'rule index out of range'}), 400

        deleted_rule = rules.pop(rule_index)
        config['rules'] = rules

        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

        return jsonify({'success': True, 'message': '规则已删除', 'rule': deleted_rule})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/state', methods=['GET'])
@require_api_key
def get_state():
    try:
        if not os.path.exists(STATE_PATH):
            return jsonify({'rules': {}})
        with open(STATE_PATH, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
@require_api_key
def get_stats():
    try:
        if not os.path.exists(STATE_PATH):
            return jsonify({'total': 0, 'baseline': 0, 'pending': 0, 'synced': 0, 'failed': 0, 'permanent_failed': 0})

        with open(STATE_PATH, 'r') as f:
            state = json.load(f)

        stats = {'total': 0, 'baseline': 0, 'pending': 0, 'synced': 0, 'failed': 0, 'permanent_failed': 0}
        for rule_id, rule_data in state.get('rules', {}).items():
            for file_path, file_info in rule_data.get('files', {}).items():
                stats['total'] += 1
                status = file_info.get('status', 'unknown')
                if status in stats:
                    stats[status] += 1

        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs', methods=['GET'])
@require_api_key
def get_logs():
    try:
        lines = int(request.args.get('lines', 100))
        lines = min(lines, LOG_API_MAX_LINES)
        log_file = LOG_PATH if os.path.exists(LOG_PATH) else LOG_PATH + '.1'

        if not os.path.exists(log_file):
            return jsonify({'logs': []})

        with open(log_file, 'r') as f:
            all_lines = f.readlines()
            return jsonify({'logs': all_lines[-lines:]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transfers', methods=['GET'])
@require_api_key
def get_transfers():
    try:
        if not os.path.exists(TRANSFERS_PATH):
            return jsonify({'transfers': {}})
        with open(TRANSFERS_PATH, 'r') as f:
            return jsonify({'transfers': json.load(f)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress', methods=['GET'])
@require_api_key
def get_progress():
    try:
        return jsonify(get_all_transfers_progress(TRANSFERS_PATH))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def background_progress_monitor():
    while True:
        try:
            progress_data = get_all_transfers_progress(TRANSFERS_PATH)
            socketio.emit('progress_update', progress_data)
        except Exception as e:
            print(f"Progress monitor error: {e}")
        socketio.sleep(1)

# Start background task globally (runs when Gunicorn imports app)
socketio.start_background_task(background_progress_monitor)

@socketio.on('connect')
def handle_connect(auth=None):
    if API_KEY and not _has_valid_session():
        logger.warning(f'Unauthenticated SocketIO connection rejected from {request.remote_addr}')
        return False

    expected_token = session.get(CSRF_SESSION_KEY, '')
    provided_token = ''
    if isinstance(auth, dict):
        provided_token = auth.get('csrf_token', '')

    if (
        not isinstance(expected_token, str) or
        not expected_token or
        not isinstance(provided_token, str) or
        not provided_token or
        not hmac.compare_digest(provided_token, expected_token)
    ):
        logger.warning(f'SocketIO connection rejected due to invalid CSRF token from {request.remote_addr}')
        return False

    logger.info(f'SocketIO client connected: {request.remote_addr}')

@socketio.on('disconnect')
def handle_disconnect():
    try:
        addr = request.remote_addr
    except RuntimeError:
        addr = 'unknown'
    logger.info(f'SocketIO client disconnected: {addr}')

if __name__ == '__main__':
    
    # Log startup information
    logger.info('Web panel starting with required API authentication enabled')
    logger.info(f'Listening on 127.0.0.1:5000 (local only)')
    logger.info(f'Allowed CORS origins: {", ".join(cors_origins)}')
    
    # Use eventlet worker for socketio, bind to localhost only
    socketio.run(app, host='127.0.0.1', port=5000, debug=False)
