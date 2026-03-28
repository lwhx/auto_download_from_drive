# auto_download_from_drive (rclone 自动增量下载守护进程)

[English Version](./README.md)

该项目用于长期运行监控 rclone 挂载目录，仅下载**新出现**的文件到本地目标目录。

## 特性

本项目基于“**长期守护 + 仅同步新文件 + 可恢复运行**”的设计理念，核心特性如下：

### 1) 增量下载与基线管理
- 首次启动时执行 **bootstrap 基线扫描**，将源目录中已存在的文件标记为 `baseline`。
- 仅处理后续扫描中“新出现”的文件，避免重复下载历史文件。

### 2) 多规则独立监控
- 支持配置多条 `source_path -> dest_path` 规则。
- 每条规则可在 `config.json` 中独立启用（`enabled`），方便分阶段管理。

### 3) 周期扫描与解耦架构
- 按 `scan_interval_seconds` 周期扫描。
- 扫描、下载、状态管理完全解耦，确保系统稳定可靠。

### 4) 并发控制与限速
- 支持多线程并发下载（`max_concurrent_downloads`）。
- 整合 `rclone --bwlimit` 支持，通过 `bandwidth_limit_mbps` 精确控制带宽。

### 5) 容错与重试机制
- 下载失败自动进入重试队列，支持 `max_retry_count` 上限控制。
- 状态详细记录：`last_error`、`last_attempt`、`retry_count`。

### 6) 挂载自动刷新
- 定期执行 `systemctl restart <rclone_service_name>` 刷新挂载。
- 刷新期间自动暂停扫描，等待下载任务空闲，并包含挂载可用性探测逻辑。

### 7) 状态持久化与清理
- 记录存储于 `sync_state.json`，支持主文件损坏时从 `.bak` 自动恢复。
- **自动清理**：源端删除文件后，其状态记录会被同步移除，保持状态精简。

### 8) Web 管理面板
- 提供内置的 Web UI 监控页面。
- 支持实时查看同步进度、修改配置、查阅日志。

---

## 目录结构

- `sync_daemon.py`: 主守护进程脚本。
- `config.json`: 核心配置文件。
- `sync_state.json`: 同步状态数据库（持久化）。
- `sync.log`: 运行时日志。
- `sync_daemon.service`: systemd 服务单元示例。
- `web_panel/`: 管理后台源码。

---

## 运行环境

- **OS**: Linux + systemd (推荐 Debian/Ubuntu)。
- **Python**: 3.11+。
- **依赖**: 已安装并配置好的 `rclone`。
- **服务**: 现有的 rclone 挂载服务（如 `rclone-pikpak.service`）。

---

## 快速上手

### 1. 初始化配置
```bash
python3 sync_daemon.py
```
若 `config.json` 不存在，程序将生成模板。请编辑其中的 `rules` 填入正确的路径。

### 2. 启动服务
建议通过 systemd 部署：
```bash
sudo cp sync_daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sync_daemon.service
```

### 3. 开启 Web 面板
```bash
cd web_panel
./start.sh
```
访问 http://127.0.0.1:12701 即可查看状态。

---

## 配置详解 (config.json)

| 字段 | 说明 |
|---|---|
| `scan_interval_seconds` | 扫描源端新文件的周期 (秒) |
| `rclone_refresh_interval_seconds` | 重启 rclone 挂载服务的周期 (秒) |
| `max_concurrent_downloads` | 并发下载线程数 |
| `max_retry_count` | 失败任务重试上限 |
| `bandwidth_limit_mbps` | 下载限速 (Mbps)，0 为不限 |
| `rules` | 包含 `source_path`, `dest_path`, `enabled` 的规则数组 |

---

## 状态说明

文件在 `sync_state.json` 中的生命周期状态：
- `baseline`: 初始扫描到的文件（不下载）。
- `pending`: 发现的新文件，等待下载。
- `synced`: 下载成功。
- `failed`: 下载失败，等待下个周期重试。
- `permanent_failed`: 重试次数超限，不再自动处理。

---

## 注意事项

- **单向同步**: 本项目仅负责从源下载新文件，**不会**删除本地已下载的文件，也**不会**执行双向同步。
- **源端敏感**: 定期清理状态是为了同步源端的真实情况，若源端文件丢失，状态记录会消失。
- **权限**: 确保运行用户对 `dest_path` 有写权限，对 `source_path` 有读权限。
