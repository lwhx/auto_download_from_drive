# Web Panel 安全审计报告

## 场景假设
- 通过 Caddy 反代将 Flask app (127.0.0.1:5000) 暴露到公网
- 启用 HTTPS + API Key 认证
- 用途：个人管理系统

---

## 安全问题分层分析

### 🔴 **危险等级 1 - 严重（Critical）**

#### 1.1 Socket.IO 完全无认证
**位置**: `app.py` 第 206-213 行

```python
@socketio.on('connect')
def handle_connect():
    print('Client connected')

# 在 index.html 中
socket.on('progress_update', renderProgress);
```

**问题**:
- Socket.IO 连接完全**不检查 API Key**
- 任何人只需连接到 `wss://your-domain/socket.io/` 即可接收实时进度数据
- 泄露：当前传输的文件名、下载速度、进度百分比等信息

**根本原因**:
Socket.IO 使用不同的连接机制，不经过 Flask 路由，需要单独实现认证

**建议修复** (严重性: 🔥🔥🔥必须立即修复):
```python
@socketio.on('connect')
def handle_connect(auth):
    if not API_KEY:
        logger.warning('Socket.IO: no API_KEY configured')
        return False
    
    token = auth.get('token') if auth else None
    if token != API_KEY:
        logger.warning(f'Socket.IO unauthorized from {request.remote_addr}')
        return False
    
    logger.info(f'Socket.IO client connected: {request.remote_addr}')
```

客户端:
```javascript
const socket = io({
    auth: {
        token: apiKey
    }
});
```

---

#### 1.2 API Key 以明文存储在 LocalStorage
**位置**: `index.html` 第 71-72 行

```javascript
let apiKey = localStorage.getItem('sync_api_key') || '';
// ...
localStorage.setItem('sync_api_key', apiKey);
```

**问题**:
- localStorage 对同源的所有 JS 都可读
- 任何 XSS 漏洞都会直接暴露 API Key
- 第三方脚本（广告、分析等）都可以访问

**风险链**:
1. 攻击者在网站上发现 XSS
2. 窃取 localStorage 中的 API Key
3. 用 Key 调用所有 API 端点

**建议修复** (严重性: 🔥🔥🔥必须重新设计):
- ✅ 使用 **sessionStorage** 替代（更易清理，但仍只有会话级保护）
- ✅ 更好方案：采用 **基于 Session 的认证**（Cookie + HttpOnly）
  
```python
from flask_session import Session

app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# 登录端点
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == API_KEY:
        session['authenticated'] = True
        session.permanent = True
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid'}), 401

# 认证装饰器
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated
```

---

#### 1.3 配置修改端点权限过大 - 命令注入风险
**位置**: `app.py` 第 88-112 行

```python
@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    new_config = request.json
    # ...保存...
    sync_service = new_config.get('sync_service_name', '').strip()
    if sync_service:
        subprocess.run(
            ['systemctl', 'restart', sync_service],
            capture_output=True, text=True, timeout=15
        )
```

**问题**:
- 虽然用了列表参数（相对安全），但接受任意 `sync_service_name`
- 攻击者可以重启任意服务（而不仅是预期的 `sync_daemon.service`）
- 攻击流程：
  1. 调用 `/api/config` 设置 `sync_service_name` = `important-service.service`
  2. 导致重启业务关键服务 → DoS

**还有**：`rclone_command` 字段也可被修改，虽然目前没有直接执行，但如果后续代码调用它就危险了

**建议修复** (严重性: 🔥🔥🔥必须限制):
```python
# 仅允许修改，不允许执行service命令
ALLOWED_SERVICES = {'sync_daemon.service', 'rclone-pikpak.service'}

@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    new_config = request.json
    
    # 验证 sync_service_name
    sync_service = new_config.get('sync_service_name', '').strip()
    if sync_service and sync_service not in ALLOWED_SERVICES:
        return jsonify({'error': 'Invalid service name'}), 400
    
    # 保存配置（不直接执行命令）
    with open(CONFIG_PATH, 'w') as f:
        json.dump(new_config, f, indent=2)
    
    # 如需要重启，应该由守护进程主动检查
    return jsonify({'success': True, 'message': '配置已保存'})
```

---

#### 1.4 日志端点泄露敏感信息
**位置**: `app.py` 第 142-152 行

```python
@app.route('/api/logs', methods=['GET'])
@require_api_key
def get_logs():
    lines = int(request.args.get('lines', 100))
    # ...返回整个日志文件...
    return jsonify({'logs': all_lines[-lines:]})
```

**问题**:
- 完整暴露日志内容，可能包含：
  - 源文件路径 → 暴露文件结构
  - 错误信息 → 暴露系统细节
  - 系统路径、用户信息等
- 无日志级别过滤
- 无敏感数据遮挡

**建议修复** (严重性: 🔥🔥中等):
```python
@app.route('/api/logs', methods=['GET'])
@require_api_key
def get_logs():
    lines = int(request.args.get('lines', 50))  # 减少默认行数
    level = request.args.get('level', 'INFO')    # 支持按级别过滤
    
    if not os.path.exists(LOG_PATH):
        return jsonify({'logs': []})
    
    try:
        with open(LOG_PATH, 'r') as f:
            all_lines = f.readlines()
        
        # 过滤并检查日志行是否包含敏感信息
        filtered = []
        for line in all_lines[-lines:]:
            if level not in line:
                continue
            # 可选：用 [REDACTED] 替换敏感路径
            line = re.sub(r'/mnt/\w+/', '[REDACTED_PATH]/', line)
            filtered.append(line)
        
        return jsonify({'logs': filtered})
    except Exception as e:
        logger.error(f'Error reading logs: {e}')
        return jsonify({'error': 'Failed to read logs'}), 500
```

---

### 🟠 **危险等级 2 - 高风险（High）**

#### 2.1 无速率限制 - 暴力破解/DDoS 风险
**问题**:
- API Key 只有一个字符串，如果被人获得就完全失守
- 没有实现 Rate Limiting，允许任意频率请求
- 10 个字符的随机密钥空间 ≈ $10^{10}$ （不是很大）
- 暴力破解流程：每秒 1000 请求，1-2 小时即可穷举

**建议修复** (严重性: 🔥🔥推荐实现):
```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

@app.route('/api/config', methods=['POST'])
@limiter.limit("5 per minute")  # 特别限制写操作
@require_api_key
def update_config():
    ...
```

或简化版本：
```python
from collections import defaultdict
from time import time

request_log = defaultdict  (list)

def rate_limit_check(ip, max_requests=10, window_seconds=60):
    now = time()
    request_log[ip] = [t for t in request_log[ip] if now - t < window_seconds]
    
    if len(request_log[ip]) >= max_requests:
        return False
    request_log[ip].append(now)
    return True
```

---

#### 2.2 缺少会话管理和登出功能
**问题**:
- API Key 无法撤销，永久生效
- 没有会话超时机制
- 没有登出功能
- 一旦泄露无法快速失效

**建议修复** (严重性: 🔥🔥推荐实现):
```python
# 在 config.json 中添加
{
    "api_key_rotation_days": 90,
    "session_timeout_minutes": 60
}

# app.py
from datetime import datetime, timedelta

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

# 定期检查 API Key 是否应该轮换
def check_api_key_expiry():
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
    key_create_time = config.get('_api_key_created_at')
    if key_create_time:
        age_days = (datetime.now() - datetime.fromisoformat(key_create_time)).days
        if age_days > config.get('api_key_rotation_days', 90):
            logger.warning('API Key is expired, please rotate')
```

---

#### 2.3 POST 请求缺少大小限制
**位置**: `app.py` 第 88 行

```python
@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    new_config = request.json  # 没有大小检查
```

**问题**:
- 允许发送任意大的 JSON 数据
- 可导致内存耗尽或 DoS

**修复**:
```python
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB 限制

@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    if len(str(request.json)) > 100_000:  # 额外的体积检查
        return jsonify({'error': 'Config too large'}), 413
    ...
```

---

#### 2.4 没有 HTTPS 强制和安全头
**问题**:
- 如果用户通过 HTTP 访问（即使 Caddy 配置了反向代理），可能被中间人攻击
- 缺少 HTTP 安全头

**建议修复** (严重性: 🔥🔥推荐实现):
```python
from flask_talisman import Talisman

Talisman(app, 
    force_https=True,
    strict_transport_security=True,
    content_security_policy={
        'default-src': "'self'",
        'script-src': ["'self'", 'https://cdn.tailwindcss.com', 'https://cdn.socket.io/4.5.4/socket.io.min.js'],
        'style-src': ["'self'", 'https://cdn.tailwindcss.com']
    }
)
```

---

### 🟡 **危险等级 3 - 中风险（Medium）**

#### 3.1 没有审计日志
**问题**:
- 没有记录"谁在何时修改了什么配置"
- 无法溯源攻击或误操作

**建议修复** (严重性: 🔧推荐实现):
```python
import logging

audit_logger = logging.getLogger('audit')
audit_handler = logging.FileHandler('audit.log')
audit_logger.addHandler(audit_handler)

def log_audit(action, actor, details):
    audit_logger.info(f"[{actor}] {action}: {details}")

@app.route('/api/config', methods=['POST'])
@require_api_key
def update_config():
    # ...修改后...
    log_audit('CONFIG_CHANGE', request.remote_addr, 
              f'Updated scan_interval to {new_config["scan_interval_seconds"]}')
```

---

#### 3.2 API Key 作为查询参数泄露
**当前**:
- API Key 在请求头 `X-API-Key` 中（相对安全）
- ✅ 不会被浏览器历史记录保存

**但如果改成查询参数** (⚠️ 不要这样做):
```
GET /api/config?api_key=xxx
```
会被记录在：
- 浏览器历史 
- 代理服务器日志
- CDN 日志
- Web 服务器日志

---

#### 3.3 跨域资源风险
**位置**: `app.py` 第 30 行

```python
CORS(app, resources={r"/api/*": {"origins": cors_origins}})
```

**问题**:
- 虽然配置了 CORS，但如果 `WEB_PANEL_ALLOWED_ORIGINS` 包含通配符 `*` 就完全无限制
- 其他网站可以跨域调用 API

**建议修复** (严重性: 🔧轻）:
```python
# 不要设置为 *
# WEB_PANEL_ALLOWED_ORIGINS = "*"

# 正确做法：明确列出
# WEB_PANEL_ALLOWED_ORIGINS = "https://example.com,https://app.example.com"

cors_origins = [o.strip() for o in ALLOWED_ORIGINS.split(',')]
if '*' in cors_origins:
    logger.warning('⚠️ CORS configured with wildcard - this is insecure!')

CORS(app, 
    resources={r"/api/*": {"origins": cors_origins}},
    supports_credentials=True,  # 允许跨域 Cookie
    allow_headers=['Content-Type', 'X-API-Key']
)
```

---

### 🟢 **危险等级 4 - 低风险（Low）**

#### 4.1 缺少 CSRF 保护
**问题**:
- 虽然 API Key 提供了某种保护，但其他网站仍可能冒充用户
- 虽然概率不大（需要知道 API Key），但完整的 CSRF 令牌更安全

**现状**: API Key 本身就是一种 CSRF 防护（攻击者需要知道 Key）
**改进**: 结合 Session 后加 CSRF Token

---

#### 4.2 版本号泄露
**问题**:
- Flask/Python 版本可能在响应头中泄露
- 攻击者可针对特定版本的已知漏洞

**修复**:
```python
@app.after_request
def remove_header(response):
    response.headers.pop('Server', None)
    return response
```

---

## 🎯 **优先级建议清单**

### 立即修复（本周）
| 优先级 | 项目 | 工作量 | 影响 |
|-------|------|------|------|
| 🔴 P0 | Socket.IO 认证 | 2h | **致命漏洞** - 完全无认证 |
| 🔴 P0 | API Key 存储方式 | 4h | **致命漏洞** - XSS 即被窃取 |
| 🔴 P0 | 配置修改限制 | 1h | **命令注入** - 可重启任意服务 |

### 短期修复（本月）
| 优先级 | 项目 | 工作量 | 影响 |
|-------|------|------|------|
| 🟠 P1 | 速率限制 | 1h | **防暴力破解** |
| 🟠 P1 | HTTPS 强制 + 安全头 | 1h | 降低中间人攻击风险 |
| 🟠 P1 | 会话管理 | 3h | 更好的身份验证 |

### 中期改进（本季度）
| 优先级 | 项目 | 工作量 | 影响 |
|-------|------|------|------|
| 🟡 P2 | 审计日志 | 2h | 追踪和调查能力 |
| 🟡 P2 | 日志敏感数据过滤 | 1h | 降低信息泄露 |
| 🟢 P3 | 版本号隐藏 | 0.5h | 提高难度 |

---

## 📋 **部署清单**

### 在公网暴露前必须做到：

- [ ] Socket.IO 实现 API Key 认证
- [ ] 改用基于 Session 的认证（而不是 localStorage 存储密钥）
- [ ] 配置端点禁止直接执行命令，改为白名单模式
- [ ] 设置 `MAX_CONTENT_LENGTH` 限制
- [ ] 添加 Rate Limiter
- [ ] 配置 HTTPS 强制和安全头 (CSP, HSTS)
- [ ] 隐藏 Server 版本号
- [ ] 验证 Caddy 反向代理配置（启用 HTTPS，正确的 upstream 地址）
- [ ] 生成强随机 API Key（≥32 字符）
- [ ] 启用审计日志

### Caddy 配置示例：
```caddyfile
sync.example.com {
    # 强制 HTTPS，设置 HSTS
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
    
    # 限制速率
    rate_limit {
        zone api /api* 50/10s
    }
    
    # 反向代理
    reverse_proxy 127.0.0.1:5000 {
        # 移除原始服务器信息
        header_down -Server
        header_down -X-Powered-By
        
        # 添加安全头
        header_down X-Content-Type-Options "nosniff"
        header_down X-Frame-Options "DENY"
        header_down X-XSS-Protection "1; mode=block"
    }
}
```

---

## 总结

| 安全维度 | 现状 | 评分 |
|---------|------|------|
| 认证 | API Key + X-API-Key Header（可接受） | ⭐⭐⭐ |
| 授权 | 所有通过认证的请求有相同权限（过度） | ⭐⭐ |
| 加密 | HTTPS via Caddy（假设正确配置） | ⭐⭐⭐⭐ |
| 会话 | 无（localStorage 不安全） | ⭐ |
| 输入验证 | 有基本验证（不完整） | ⭐⭐ |
| 日志 | 无审计日志 | ⭐ |
| 速率限制 | 无 | ⭐ |
| **整体** | **不适合公网** | **⭐⭐** |

**建议**: 在实现上述 P0-P1 修复前，**不要暴露到公网**。当前配置适合仅在本地网络或 VPN 使用。
