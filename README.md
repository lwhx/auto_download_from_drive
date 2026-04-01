# auto_download_from_drive

[中文文档](./zh_README.md)

`auto_download_from_drive` is a Linux daemon for **incremental one-way downloads** from an **rclone-mounted directory** to a local destination. It only downloads files that appear **after** the initial baseline scan, persists state on disk, and includes a local-only Flask web panel for monitoring and config management.

## What It Does

- Watches one or more mounted source directories on a timer
- Marks pre-existing files as `baseline` on first initialization and skips them
- Queues only newly detected files for download
- Downloads with `rclone copy`
- Tracks retries, progress, logs, and persistent file state
- Periodically refreshes the rclone mount service, but only when there are no active or queued download tasks
- Exposes a web panel with config editing, state stats, logs, and real-time transfer progress

This is **not** bidirectional sync, mirror sync, or deletion sync. Local files are never removed by the daemon.

## Architecture

The daemon and web panel do not talk through direct IPC. They communicate through shared files under the install directory.

```text
[ Caddy / Nginx / other reverse proxy ]
                |
                v
[ web-panel.service ]  -> reads/writes config + reads runtime files
                |
                v
[ /opt/sync ]
  - config.json
  - sync_state.json
  - active_transfers.json
  - runtime_status.json
  - sync.log
                ^
                |
[ sync.service ] -> scans, queues, downloads, refreshes mount
                |
                v
[ rclone-mounted source directories ]
```

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

## Current Features

### Incremental baseline model

When an enabled rule is initialized for the first time, every file already present in `source_path` is recorded as `baseline`. Those files are intentionally skipped. Only files discovered later become `pending` and are eligible for download.

### Multiple rules

`config.json` supports multiple `source_path -> dest_path` rules. Each rule has its own `enabled` flag and its own state bucket under `sync_state.json`.

### Concurrent downloads

The daemon starts a worker pool controlled by `max_concurrent_downloads`. Each worker runs `rclone copy` for a single file.

### Real-time progress via rclone RC

Each active transfer launches `rclone` with a temporary local RC port in the `5572-5582` range. The web panel polls those RC ports and pushes progress updates to connected clients over Socket.IO every second.

### Retry and permanent failure handling

Failed downloads are retried on later scan cycles. Once `retry_count >= max_retry_count`, the file is marked `permanent_failed`.

Note: because the current implementation increments `retry_count` before comparing it, setting `max_retry_count` to `0` causes the first failure to become `permanent_failed` immediately.

### Automatic mount refresh

Every `rclone_refresh_interval_seconds`, the daemon:

1. checks whether queued and active downloads are both empty
2. skips this refresh cycle immediately if download work still exists
3. pauses scanning only after confirming the daemon is idle
4. restarts the configured rclone systemd service
5. probes enabled source paths until the mount is ready again
6. resumes normal scanning

### Persistent state and recovery

State lives in `sync_state.json`, with a `.json.bak` fallback if the main file cannot be loaded. Missing source files are pruned from state automatically; local downloaded files remain untouched.

### Web panel with authenticated config editing

The web panel provides:

- config view and save
- add/delete rules
- aggregate status stats
- raw state inspection
- log tailing
- live transfer progress

Saving `/api/config` only attempts `systemctl restart sync.service` when the daemon reports zero active downloads and zero queued downloads; otherwise it saves the config and skips the restart.

## File State Lifecycle

```text
existing during first initialization -> baseline
newly discovered                    -> pending
pending -> success                  -> synced
pending -> failure                  -> failed
failed  -> retry_count limit hit    -> permanent_failed
```

State keys inside the transfer registry are built as `<rule_id>:<source_file_path>`.

## Requirements

- Linux with `systemd`
- Python 3
- `python3-venv`
- `rclone`
- An existing rclone mount managed by systemd
- Root privileges for `start.sh` and `update.sh`
- A reverse proxy if you want remote browser access

Debian or Ubuntu is the intended environment.

## Installation

Run the installer as root:

```bash
sudo ./start.sh
```

The installer currently does all of the following:

- removes a previous `/opt/sync` install
- stops and disables `sync.service` and `web-panel.service`
- recreates `/opt/sync` and `/opt/sync/web_panel`
- downloads project files from the GitHub `main` branch
- creates `/opt/sync/config.json`
- creates `/opt/sync/web_panel/.env`
- creates a `web-panel` system user
- creates a Python virtualenv and installs `web_panel/requirements.txt`
- writes `sync.service` and `web-panel.service`
- writes `/etc/sudoers.d/web-panel`
- writes a polkit rule when `/etc/polkit-1/rules.d` exists
- enables and starts both services

Because `start.sh` downloads from GitHub instead of copying the local checkout, the installed version follows the remote `main` branch, not necessarily your local uncommitted workspace.

## First-Time Configuration

### 1. Edit `/opt/sync/config.json`

At minimum, update:

- `rclone_service_name`
- each rule's `source_path`
- each rule's `dest_path`
- each rule's `enabled`

Then restart the daemon:

```bash
sudo systemctl restart sync.service
```

Example:

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

### 2. Edit `/opt/sync/web_panel/.env`

The current web panel requires both of these values:

- `WEB_PANEL_API_KEY`
- `WEB_PANEL_SECRET_KEY`

`start.sh` auto-generates `WEB_PANEL_SECRET_KEY`, but you must still replace the placeholder API key and set allowed origins correctly.

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

Then restart the panel:

```bash
sudo systemctl restart web-panel.service
```

### 3. Configure a reverse proxy

The panel binds to `127.0.0.1:5000` only. Publish it through Caddy, Nginx, or another proxy and forward the usual headers.

Caddy example:

```caddy
panel.example.com {
    @allowed remote_ip YOUR.IP.ADDRESS
    handle @allowed {
        reverse_proxy 127.0.0.1:5000
    }
    respond 403
}
```

## Configuration Reference

### `config.json`

| Field | Type | Description |
|---|---|---|
| `scan_interval_seconds` | int | Delay between incremental scans |
| `rclone_refresh_interval_seconds` | int | Delay between mount refreshes |
| `max_concurrent_downloads` | int | Number of download worker threads |
| `max_retry_count` | int | Failure threshold before `permanent_failed` |
| `bandwidth_limit_mbps` | number | `0` disables `--bwlimit`; otherwise passed as `XM` to rclone |
| `rclone_command` | string | Binary name or full path for `rclone` |
| `rclone_service_name` | string | systemd unit to restart during mount refresh |
| `rules` | array | Sync rules |

### Rule fields

| Field | Type | Description |
|---|---|---|
| `source_path` | string | Absolute path to an rclone-mounted source directory |
| `dest_path` | string | Absolute path to the local destination directory |
| `enabled` | bool | Enables scanning and downloading for that rule |

### Web panel `.env`

| Variable | Required | Default | Description |
|---|---|---|---|
| `WEB_PANEL_API_KEY` | yes | none | API key for `/api/*` authentication |
| `WEB_PANEL_SECRET_KEY` | yes | none at runtime | Flask session secret |
| `WEB_PANEL_ALLOWED_ORIGINS` | no | `http://localhost,https://localhost` | Comma-separated allowed origins for CORS and Socket.IO |
| `WEB_PANEL_SESSION_TTL_SECONDS` | no | `1800` | Sliding authenticated session lifetime |
| `WEB_PANEL_LOG_LEVEL` | no | `INFO` | Web panel log level |
| `WEB_PANEL_AUTH_MAX_FAILURES` | no | `10` | Failed auth attempts allowed in one window |
| `WEB_PANEL_AUTH_WINDOW_SECONDS` | no | `600` | Failure counting window |
| `WEB_PANEL_AUTH_LOCKOUT_SECONDS` | no | `900` | Temporary lockout duration |
| `WEB_PANEL_AUTH_CLEANUP_INTERVAL` | no | `300` | Cleanup interval for stale rate-limit entries |

## Web Panel Security Model

The current panel is stricter than the old README described:

- `WEB_PANEL_API_KEY` is required at startup
- `WEB_PANEL_SECRET_KEY` is also required at startup
- successful API key auth is promoted to an HttpOnly secure session
- unsafe session-based requests require `Origin` or `Referer` validation
- unsafe session-based requests also require `X-CSRF-Token`
- failed auth attempts are rate-limited per client IP
- Socket.IO connections are rejected when there is no valid authenticated session

The bundled frontend handles the auth flow automatically by calling `/api/auth`, storing the CSRF token in memory, and reconnecting the socket after login.

## Management API

Main endpoints implemented in `web_panel/app.py`:

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

## Services and Privilege Model

`start.sh` installs:

- `sync.service` running as `root`
- `web-panel.service` running as `web-panel`

To allow the panel to restart `sync.service` after config saves, the installer writes:

- a sudoers rule for `systemctl start|stop|restart|status sync.service`
- a polkit rule that allows `web-panel` to manage `sync.service` directly when polkit rules are available

In `web_panel/app.py`, service control tries `sudo -n systemctl ...` first and falls back to direct `systemctl`.

## Updating

To update an installed deployment while preserving `/opt/sync/config.json` and `/opt/sync/web_panel/.env`:

```bash
sudo ./update.sh
```

`update.sh` downloads the latest tracked project files, fixes ownership and permissions, upgrades the panel virtualenv, and restarts services that were active before the update.

## Useful Commands

```bash
sudo systemctl status sync.service
sudo systemctl status web-panel.service

sudo journalctl -u sync.service -f
sudo journalctl -u web-panel.service -f
sudo tail -f /var/log/web-panel/error.log

cat /opt/sync/sync_state.json | python3 -m json.tool
cat /opt/sync/active_transfers.json | python3 -m json.tool
```

## Notes and Limitations

- The daemon treats the first scan of each enabled rule as a baseline snapshot, not a backfill import.
- `bandwidth_limit_mbps` is named in Mbps in config, but the current implementation passes the numeric value to rclone as `M` bytes-per-second style units. Treat the field as "rclone `M` units" unless you adjust the code.
- The panel's `POST /api/config/rules` and `DELETE /api/config/rules/<rule_index>` endpoints modify `config.json` but do not restart `sync.service` by themselves. The daemon only picks up rule changes after a service restart.
- The web UI label currently says `MB/s` for bandwidth while the backend field name is `bandwidth_limit_mbps`.
- `start.sh` is intentionally destructive to previous installs under `/opt/sync`.
