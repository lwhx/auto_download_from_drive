# auto_download_from_drive（rclone 增量下载守护进程）

[English](./README.md)

`auto_download_from_drive` 是一个运行在 Linux 上的**单向增量下载守护进程**，用于把 **rclone 挂载目录**中新出现的文件自动下载到本地目录。它只会处理**基线扫描之后新增**的文件，状态持久化到磁盘，并附带一个仅监听本机的 Flask Web 管理面板。

## 项目作用

- 定时扫描一个或多个已挂载的源目录
- 首次初始化时把已有文件标记为 `baseline` 并跳过
- 仅将之后新出现的文件加入下载队列
- 通过 `rclone copy` 下载单个文件
- 持久化重试、进度、日志和文件状态
- 定期重启 rclone 挂载服务，缓解挂载失活问题
- 提供 Web 面板用于配置、状态查看、日志查看和实时进度监控

这不是双向同步，也不是镜像同步，更不会删除本地文件。

## 架构

守护进程和 Web 面板之间没有直接 IPC，它们通过安装目录下的共享文件协作。

```text
[ Caddy / Nginx / 其他反向代理 ]
                |
                v
[ web-panel.service ]  -> 读取/写入配置，读取运行态文件
                |
                v
[ /opt/sync ]
  - config.json
  - sync_state.json
  - active_transfers.json
  - sync.log
                ^
                |
[ sync.service ] -> 扫描、排队、下载、刷新挂载
                |
                v
[ rclone 挂载后的源目录 ]
```

## 仓库结构

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

## 当前特性

### 基线增量模型

某条启用规则第一次初始化时，`source_path` 中当时已存在的所有文件都会记录为 `baseline`，这些文件不会被回补下载。只有后续新发现的文件才会进入 `pending` 并有资格被下载。

### 多规则支持

`config.json` 支持多条 `source_path -> dest_path` 规则。每条规则都有自己的 `enabled` 开关，也有自己在 `sync_state.json` 下的状态分区。

### 并发下载

守护进程按 `max_concurrent_downloads` 启动 worker 池，每个 worker 使用一次 `rclone copy` 处理一个文件。

### 基于 rclone RC 的实时进度

每个活跃传输都会给 `rclone` 分配一个临时本地 RC 端口，范围是 `5572-5582`。Web 面板会轮询这些 RC 端口，并每秒通过 Socket.IO 向已连接浏览器推送一次进度更新。

### 重试与永久失败

下载失败后，文件会在后续扫描周期再次重试。当 `retry_count >= max_retry_count` 时，状态会变成 `permanent_failed`。

注意：当前实现是先递增 `retry_count` 再比较，所以如果把 `max_retry_count` 设为 `0`，第一次失败就会直接进入 `permanent_failed`。

### 自动刷新挂载

每经过 `rclone_refresh_interval_seconds`，守护进程会：

1. 暂停扫描
2. 等待队列和正在进行的下载排空
3. 重启配置中的 rclone systemd 服务
4. 持续探测已启用规则的源路径，直到挂载恢复可用
5. 恢复正常扫描

### 状态持久化与恢复

状态保存在 `sync_state.json`，如果主文件无法读取，会尝试回退到 `.json.bak`。源端消失的文件会自动从状态中清理，本地已经下载的文件不会被删除。

### 带认证的 Web 配置面板

Web 面板当前支持：

- 查看和保存配置
- 新增和删除规则
- 聚合状态统计
- 查看原始 state
- 查看日志尾部
- 查看实时传输进度

保存 `/api/config` 时会自动尝试执行 `systemctl restart sync.service`。

## 文件状态流转

```text
规则首次初始化时已存在 -> baseline
后续新发现             -> pending
pending -> 成功         -> synced
pending -> 失败         -> failed
failed  -> 达到重试上限 -> permanent_failed
```

传输注册表中的键格式是 `<rule_id>:<source_file_path>`。

## 运行要求

- Linux，且使用 `systemd`
- Python 3
- `python3-venv`
- `rclone`
- 一个已经由 systemd 管理的 rclone 挂载服务
- 运行 `start.sh` 和 `update.sh` 时需要 root 权限
- 如果要远程访问浏览器面板，需要反向代理

默认目标环境是 Debian 或 Ubuntu。

## 安装

以 root 身份运行安装脚本：

```bash
sudo ./start.sh
```

当前安装脚本会执行以下操作：

- 删除已有的 `/opt/sync` 安装
- 停止并禁用 `sync.service` 和 `web-panel.service`
- 重新创建 `/opt/sync` 和 `/opt/sync/web_panel`
- 从 GitHub `main` 分支下载项目文件
- 创建 `/opt/sync/config.json`
- 创建 `/opt/sync/web_panel/.env`
- 创建 `web-panel` 系统用户
- 创建 Python 虚拟环境并安装 `web_panel/requirements.txt`
- 写入 `sync.service` 和 `web-panel.service`
- 写入 `/etc/sudoers.d/web-panel`
- 若存在 `/etc/polkit-1/rules.d`，则写入 polkit 规则
- 启用并启动两个服务

由于 `start.sh` 是从 GitHub 下载文件，而不是复制当前工作区，所以实际安装版本跟随远端 `main` 分支，不一定等于你本地尚未提交的改动。

## 首次配置

### 1. 编辑 `/opt/sync/config.json`

最少需要修改这些字段：

- `rclone_service_name`
- 每条规则的 `source_path`
- 每条规则的 `dest_path`
- 每条规则的 `enabled`

然后重启守护进程：

```bash
sudo systemctl restart sync.service
```

示例：

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

### 2. 编辑 `/opt/sync/web_panel/.env`

当前 Web 面板启动时要求以下两个变量都必须存在：

- `WEB_PANEL_API_KEY`
- `WEB_PANEL_SECRET_KEY`

`start.sh` 会自动生成 `WEB_PANEL_SECRET_KEY`，但你仍然必须替换占位的 API Key，并正确设置允许来源。

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

然后重启面板：

```bash
sudo systemctl restart web-panel.service
```

### 3. 配置反向代理

面板只绑定在 `127.0.0.1:5000`，需要通过 Caddy、Nginx 或其他反向代理暴露出去，并转发常见代理头。

Caddy 示例：

```caddy
panel.example.com {
    @allowed remote_ip YOUR.IP.ADDRESS
    handle @allowed {
        reverse_proxy 127.0.0.1:5000
    }
    respond 403
}
```

## 配置说明

### `config.json`

| 字段 | 类型 | 说明 |
|---|---|---|
| `scan_interval_seconds` | int | 增量扫描间隔 |
| `rclone_refresh_interval_seconds` | int | 挂载刷新间隔 |
| `max_concurrent_downloads` | int | 下载 worker 线程数 |
| `max_retry_count` | int | 进入 `permanent_failed` 前的失败阈值 |
| `bandwidth_limit_mbps` | number | `0` 表示不加 `--bwlimit`；否则按 `XM` 传给 rclone |
| `rclone_command` | string | `rclone` 可执行文件名或绝对路径 |
| `rclone_service_name` | string | 刷新挂载时要重启的 systemd unit |
| `rules` | array | 同步规则列表 |

### 规则字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_path` | string | rclone 挂载源目录的绝对路径 |
| `dest_path` | string | 本地下载目标目录的绝对路径 |
| `enabled` | bool | 是否启用该规则 |

### Web 面板 `.env`

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `WEB_PANEL_API_KEY` | 是 | 无 | `/api/*` 的认证 API Key |
| `WEB_PANEL_SECRET_KEY` | 是 | 运行时无默认 | Flask session 密钥 |
| `WEB_PANEL_ALLOWED_ORIGINS` | 否 | `http://localhost,https://localhost` | CORS 和 Socket.IO 允许来源，逗号分隔 |
| `WEB_PANEL_SESSION_TTL_SECONDS` | 否 | `1800` | 滑动 session 有效期 |
| `WEB_PANEL_LOG_LEVEL` | 否 | `INFO` | Web 面板日志级别 |
| `WEB_PANEL_AUTH_MAX_FAILURES` | 否 | `10` | 单个时间窗口内允许的认证失败次数 |
| `WEB_PANEL_AUTH_WINDOW_SECONDS` | 否 | `600` | 认证失败统计窗口 |
| `WEB_PANEL_AUTH_LOCKOUT_SECONDS` | 否 | `900` | 临时封禁时长 |
| `WEB_PANEL_AUTH_CLEANUP_INTERVAL` | 否 | `300` | 清理过期限流记录的间隔 |

## Web 面板安全模型

当前面板比旧版 README 描述得更严格：

- `WEB_PANEL_API_KEY` 启动时必须存在
- `WEB_PANEL_SECRET_KEY` 启动时必须存在
- API Key 认证成功后会升级为 HttpOnly secure session
- 基于 session 的危险请求必须通过 `Origin` 或 `Referer` 校验
- 基于 session 的危险请求还必须带 `X-CSRF-Token`
- 认证失败会按客户端 IP 做限流和临时封禁
- 没有有效认证 session 时，Socket.IO 连接会被拒绝

仓库内的前端页面已经自动实现这套流程：先调用 `/api/auth`，把 CSRF token 保存在内存中，认证成功后再建立 socket 连接。

## 管理 API

`web_panel/app.py` 中当前实现的主要接口：

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

## 服务与权限模型

`start.sh` 会安装：

- 以 `root` 运行的 `sync.service`
- 以 `web-panel` 运行的 `web-panel.service`

为了让 Web 面板在保存配置后可以重启 `sync.service`，安装脚本还会写入：

- 一个 sudoers 规则，允许执行 `systemctl start|stop|restart|status sync.service`
- 一个 polkit 规则，在支持 polkit 规则的环境中允许 `web-panel` 直接管理 `sync.service`

在 `web_panel/app.py` 中，服务控制会优先尝试 `sudo -n systemctl ...`，失败后再回退到直接执行 `systemctl`。

## 更新

如果要保留 `/opt/sync/config.json` 和 `/opt/sync/web_panel/.env` 的前提下更新已安装实例：

```bash
sudo ./update.sh
```

`update.sh` 会下载最新项目文件、修正属主和权限、升级 Web 面板虚拟环境依赖，并重启更新前原本处于运行状态的服务。

## 常用命令

```bash
sudo systemctl status sync.service
sudo systemctl status web-panel.service

sudo journalctl -u sync.service -f
sudo journalctl -u web-panel.service -f
sudo tail -f /var/log/web-panel/error.log

cat /opt/sync/sync_state.json | python3 -m json.tool
cat /opt/sync/active_transfers.json | python3 -m json.tool
```

## 注意事项与限制

- 每条启用规则的首次扫描是“建立基线”，不是“回补历史文件”。
- `bandwidth_limit_mbps` 这个名字写的是 Mbps，但当前实现是把数值直接按 `M` 传给 rclone。除非你改代码，否则更准确的理解应接近“rclone 的 `M` 单位值”。
- 面板的 `POST /api/config/rules` 和 `DELETE /api/config/rules/<rule_index>` 只会改写 `config.json`，不会自动重启 `sync.service`。规则变更要在服务重启后才会被守护进程实际加载。
- Web UI 中带宽字段的文案现在写的是 `MB/s`，而后端字段名是 `bandwidth_limit_mbps`。
- `start.sh` 对已有 `/opt/sync` 安装是破坏性重装。
