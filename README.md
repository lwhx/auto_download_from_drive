# auto_download_from_drive

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Socket.IO](https://img.shields.io/badge/Socket.IO-5.x-010101?logo=socketdotio&logoColor=white)](https://socket.io/)
[![Gunicorn](https://img.shields.io/badge/Gunicorn-21%2B-499848?logo=gunicorn&logoColor=white)](https://gunicorn.org/)
[![rclone](https://img.shields.io/badge/rclone-mounted%20remote-3F79E0)](https://rclone.org/)
[![systemd](https://img.shields.io/badge/systemd-service-FFB300)](https://systemd.io/)
[![Linux](https://img.shields.io/badge/Linux-Debian%20%2F%20Ubuntu-FCC624?logo=linux&logoColor=black)](https://kernel.org/)

<p align="center">
  <img src="./docs/architecture-overview.svg" alt="Architecture overview diagram" width="980">
</p>

[中文文档](./zh_README.md)

`auto_download_from_drive` is a Linux daemon for **one-way incremental downloads** from **rclone-mounted directories** into local storage.

It is designed for a very specific workflow:

- first scan = build a baseline snapshot
- only files discovered later are queued for download
- downloads are executed with `rclone copy`
- state, logs, runtime counters, and transfer progress are persisted on disk
- a local-only Flask panel handles config editing, monitoring, and live progress

This is **not** bidirectional sync, mirror sync, or deletion sync.

## At a Glance

- **Incremental by design**: existing files are marked as `baseline`, not backfilled
- **Multi-rule support**: multiple `source_path -> dest_path` pairs in one daemon
- **Concurrent downloads**: controlled by `max_concurrent_downloads`
- **Persistent runtime state**: survives restarts and supports recovery
- **Safe-ish mount refresh flow**: only restarts the rclone mount when no downloads are active or queued
- **Authenticated web panel**: API key login, session auth, CSRF checks, Socket.IO progress streaming

## Technology Stack

| Layer | Stack |
|---|---|
| Daemon runtime | Python 3, standard library threads/queue/subprocess |
| File transfer | `rclone copy`, optional RC API for progress |
| Service management | `systemd` |
| Web panel backend | Flask 3.0, Flask-SocketIO 5.x, Flask-CORS |
| Web serving | Gunicorn, gevent, gevent-websocket |
| Frontend delivery | server-rendered HTML + Socket.IO client |
| Deployment model | Debian/Ubuntu-style Linux host with local reverse proxy |

Key Python dependencies from [`web_panel/requirements.txt`](./web_panel/requirements.txt):

- `flask==3.0.0`
- `flask-socketio==5.3.5`
- `flask-cors==4.0.0`
- `requests==2.31.0`
- `python-socketio==5.10.0`
- `gunicorn>=21.2.0`
- `python-dotenv>=1.0.0`
- `gevent>=23.9.1`
- `gevent-websocket>=0.10.1`

## Architecture

The daemon and the web panel do **not** communicate through direct IPC. They coordinate through shared files under the install directory.

```text
[ Reverse Proxy ]
        |
        v
[ web-panel.service ]
        |
        | reads/writes
        v
[ /opt/sync ]
  - config.json
  - sync_state.json
  - active_transfers.json
  - runtime_status.json
  - sync.log
        ^
        | reads/writes
        |
[ sync.service ]
        |
        v
[ rclone-mounted source directories ]
```

## How It Works

### File lifecycle

```text
existing during first initialization -> baseline
newly discovered                    -> pending
pending -> success                  -> synced
pending -> failure                  -> failed
failed  -> retry_count limit hit    -> permanent_failed
```

Transfer registry keys are built as `<rule_id>:<source_file_path>`.

### Mount refresh behavior

Every `rclone_refresh_interval_seconds`, the daemon:

1. checks whether active downloads and queued downloads are both zero
2. skips refresh immediately if work is still pending
3. pauses scanning only after the daemon is idle
4. restarts the configured rclone systemd unit
5. probes enabled source paths until the mount is ready again
6. resumes normal scanning

## Quick Start

### Production install

```bash
sudo ./start.sh
```

The installer currently:

- recreates `/opt/sync`
- downloads tracked files from the GitHub `main` branch
- creates `config.json` and `web_panel/.env`
- creates the `web-panel` system user
- installs the Python virtualenv for the web panel
- writes `sync.service` and `web-panel.service`
- writes sudoers/polkit rules so the panel can manage `sync.service`

Important:

- `start.sh` is destructive to an existing `/opt/sync` install
- installation follows the remote GitHub `main` branch, not your local uncommitted workspace

### First-time configuration

Edit `/opt/sync/config.json`:

```json
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 3,
  "max_retry_count": 5,
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "rclone-pikpak",
  "rules": [
    {
      "source_path": "/mnt/pikpak/incoming",
      "dest_path": "/data/downloads",
      "enabled": true
    }
  ]
}
```

Then:

```bash
sudo systemctl restart sync.service
```

Edit `/opt/sync/web_panel/.env`:

```env
WEB_PANEL_API_KEY=replace-with-a-strong-random-value
WEB_PANEL_ALLOWED_ORIGINS=https://panel.example.com
WEB_PANEL_SECRET_KEY=generated-or-custom-secret
WEB_PANEL_SESSION_TTL_SECONDS=1800
WEB_PANEL_LOG_LEVEL=INFO
WEB_PANEL_AUTH_MAX_FAILURES=10
WEB_PANEL_AUTH_WINDOW_SECONDS=600
WEB_PANEL_AUTH_LOCKOUT_SECONDS=900
WEB_PANEL_AUTH_CLEANUP_INTERVAL=300
```

Then:

```bash
sudo systemctl restart web-panel.service
```

### Reverse proxy

The panel binds to `127.0.0.1:5000` only. Publish it through Caddy, Nginx, or another reverse proxy.

Example with Caddy:

```caddy
panel.example.com {
    @allowed remote_ip YOUR.IP.ADDRESS
    handle @allowed {
        reverse_proxy 127.0.0.1:5000
    }
    respond 403
}
```

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r web_panel/requirements.txt
python3 -m py_compile sync_daemon.py web_panel/app.py web_panel/rclone_monitor.py
python3 web_panel/app.py
```

For local panel testing, set the required env vars first:

```bash
export WEB_PANEL_API_KEY=dev-key
export WEB_PANEL_SECRET_KEY=dev-secret
export WEB_PANEL_ALLOWED_ORIGINS=http://localhost:5000
```

## Configuration Reference

### `config.json`

| Field | Type | Description |
|---|---|---|
| `scan_interval_seconds` | int | Delay between incremental scans |
| `rclone_refresh_interval_seconds` | int | Delay between mount refresh cycles |
| `max_concurrent_downloads` | int | Number of download worker threads |
| `max_retry_count` | int | Failure threshold before `permanent_failed` |
| `bandwidth_limit_mbps` | number | `0` disables `--bwlimit`; otherwise passed to rclone as `XM` |
| `rclone_command` | string | Binary name or absolute path for `rclone` |
| `rclone_service_name` | string | systemd unit restarted during mount refresh |
| `rules` | array | Download rule list |

### Rule fields

| Field | Type | Description |
|---|---|---|
| `source_path` | string | Absolute path to an rclone-mounted source directory |
| `dest_path` | string | Absolute path to the local destination directory |
| `enabled` | bool | Enables scanning and downloading for the rule |

### Web panel `.env`

| Variable | Required | Default | Description |
|---|---|---|---|
| `WEB_PANEL_API_KEY` | yes | none | API key for `/api/*` authentication |
| `WEB_PANEL_SECRET_KEY` | yes | none at runtime | Flask session secret |
| `WEB_PANEL_ALLOWED_ORIGINS` | no | `http://localhost,https://localhost` | Allowed origins for CORS and Socket.IO |
| `WEB_PANEL_SESSION_TTL_SECONDS` | no | `1800` | Sliding session lifetime |
| `WEB_PANEL_LOG_LEVEL` | no | `INFO` | Panel log level |
| `WEB_PANEL_AUTH_MAX_FAILURES` | no | `10` | Allowed failures within one auth window |
| `WEB_PANEL_AUTH_WINDOW_SECONDS` | no | `600` | Failure counting window |
| `WEB_PANEL_AUTH_LOCKOUT_SECONDS` | no | `900` | Temporary lockout duration |
| `WEB_PANEL_AUTH_CLEANUP_INTERVAL` | no | `300` | Cleanup interval for stale auth entries |

## Web Panel Security Model

Current behavior in [`web_panel/app.py`](./web_panel/app.py):

- `WEB_PANEL_API_KEY` is required at startup
- `WEB_PANEL_SECRET_KEY` is required at startup
- successful API key auth is promoted to an HttpOnly session
- unsafe session-based requests require valid `Origin` or `Referer`
- unsafe session-based requests also require `X-CSRF-Token`
- failed auth attempts are rate-limited per client IP
- Socket.IO connections without a valid session are rejected

## Management API

Main endpoints exposed by the panel:

- `POST /api/auth`
- `GET /api/config`
- `POST /api/config`
- `POST /api/config/rules`
- `DELETE /api/config/rules/<rule_index>`
- `GET /api/state`
- `GET /api/stats`
- `GET /api/logs`
- `GET /api/transfers`
- `GET /api/progress`

## Repository Layout

```text
.
├── sync_daemon.py
├── start.sh
├── update.sh
├── README.md
├── zh_README.md
└── web_panel/
    ├── app.py
    ├── rclone_monitor.py
    ├── requirements.txt
    ├── README.md
    └── templates/
        └── index.html
```

## Operations

### Update an existing installation

```bash
sudo ./update.sh
```

`update.sh` preserves:

- `/opt/sync/config.json`
- `/opt/sync/web_panel/.env`

### Useful commands

```bash
sudo systemctl status sync.service
sudo systemctl status web-panel.service

sudo journalctl -u sync.service -f
sudo journalctl -u web-panel.service -f
sudo tail -f /var/log/web-panel/error.log

cat /opt/sync/sync_state.json | python3 -m json.tool
cat /opt/sync/active_transfers.json | python3 -m json.tool
```

## Known Limitations

- The first scan of an enabled rule creates a baseline snapshot instead of backfilling existing files.
- `bandwidth_limit_mbps` is named like Mbps, but the current implementation passes the numeric value to rclone as `M`.
- `POST /api/config/rules` and `DELETE /api/config/rules/<rule_index>` update `config.json`, but do not restart `sync.service` by themselves.
- The web UI currently labels bandwidth as `MB/s` while the backend field name is `bandwidth_limit_mbps`.
- `max_retry_count=0` makes the first failure immediately become `permanent_failed`.

## Related Files

- [`sync_daemon.py`](./sync_daemon.py)
- [`start.sh`](./start.sh)
- [`update.sh`](./update.sh)
- [`web_panel/app.py`](./web_panel/app.py)
- [`web_panel/README.md`](./web_panel/README.md)
