# auto_download_from_drive (rclone Incremental Sync Daemon)

[中文](./zh_README.md)

A background daemon designed to monitor **rclone mounted directories** over the long term, automatically downloading **only new files** to a local destination.

## Features

Built with the philosophy of "**Persistent Daemon + Incremental Only + Robust Recovery**," the core features include:

### 1) Incremental Downloads & Baseline Management
- **Bootstrap Scan**: Upon the first run or when a new rule is added, existing files are marked as `baseline` and will not be triggered for download.
- **New File Only**: Only files that appearing after the initial scan are processed, preventing massive redownloads of historical data.

### 2) Multi-Rule Independence
- Configure multiple `source_path -> dest_path` rules.
- Each rule can be independently toggled via the `enabled` field in `config.json`.

### 3) Periodic Scanning & Decoupled Architecture
- Continuous monitoring based on `scan_interval_seconds`.
- Scanning, downloading, and state management are fully decoupled for high stability.

### 4) Concurrency & Bandwidth Control
- Multi-threaded concurrent downloads (`max_concurrent_downloads`).
- Integrated `rclone --bwlimit` support via `bandwidth_limit_mbps` to prevent network congestion.

### 5) Error Handling & Retries
- Failed downloads are automatically queued for retry, subject to `max_retry_count`.
- Detailed state tracking: `last_error`, `last_attempt`, and `retry_count`.

### 6) Automated Mount Refreshing
- Periodically restarts the rclone mount service (e.g., `systemctl restart rclone-pikpak.service`).
- Automatically pauses scanning and waits for idle workers before refreshing to prevent stale mount issues.

### 7) State Persistence & Auto-Cleanup
- Persistence via `sync_state.json`, with automatic recovery from `.bak` if corrupted.
- **Dynamic Cleanup**: When a file is deleted from the source, its state entry is automatically removed to keep the database lean.

### 8) Web Management Panel
- Includes a Built-in Web UI for real-time monitoring.
- Track progress, modify configurations, and view logs via a browser.

---

## Directory Structure

- `sync_daemon.py`: The core daemon script.
- `config.json`: Central configuration file.
- `sync_state.json`: Persistent state database (auto-generated).
- `sync.log`: Runtime logs (with rotating file support).
- `sync_daemon.service`: Example systemd unit file.
- `web_panel/`: Management dashboard source code.

---

## Requirements

- **OS**: Linux + systemd (Debian/Ubuntu recommended).
- **Python**: 3.11+.
- **Dependencies**: Installed and configured `rclone`.
- **Services**: An existing rclone mount systemd service.

---

## Quick Start

### 1. Initialize Configuration
```bash
python3 sync_daemon.py
```
If `config.json` doesn't exist, a template will be generated. Edit it and fill in your `rules`.

### 2. Run as a Service
Deployment via systemd is highly recommended:
```bash
sudo cp sync_daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sync_daemon.service
```

### 3. Start the Web Panel
```bash
cd web_panel
./start.sh
```
Access the dashboard at http://127.0.0.1:12701.

---

## Configuration Guide (config.json)

| Field                             | Description                                                               |
| --------------------------------- | ------------------------------------------------------------------------- |
| `scan_interval_seconds`           | Cycle duration for scanning new files (sec).                              |
| `rclone_refresh_interval_seconds` | Interval to restart the rclone mount service (sec).                       |
| `max_concurrent_downloads`        | Number of parallel download threads.                                      |
| `max_retry_count`                 | Maximum retries before marking a file as permanently failed.              |
| `bandwidth_limit_mbps`            | Download limit in Mbps (0 for unlimited).                                 |
| `rules`                           | Array of sync rules containing `source_path`, `dest_path`, and `enabled`. |

---

## Lifecycle States

File states tracked in `sync_state.json`:
- `baseline`: Files found during initial setup (ignored for download).
- `pending`: Newly discovered files awaiting download.
- `synced`: Successfully downloaded.
- `failed`: Download failed, pending retry in the next cycle.
- `permanent_failed`: Retry limit exceeded; requires manual intervention or state reset.

---

## Key Considerations

- **One-Way Only**: This project only downloads files from the source. It **does not** delete local files or perform two-way syncing.
- **Source Mirroring**: State records are cleaned up based on source availability. If a source file is removed, its record disappears (local data remains untouched).
- **Permissions**: Ensure the service user has `read` access to the source and `write` access to the destination.