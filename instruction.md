## 1. 脚本实现的功能

1. 以 root 权限在服务器上“一键部署” auto_download_from_drive：
   - 清理上一次安装残留：停止并禁用 `sync.service`、`web-panel.service`，删除旧的 systemd 单元文件、sudoers、polkit 规则、安装目录、日志目录以及旧的 service 用户。
   - 在 `/opt/sync` 下重新创建运行目录与 Web 面板目录（含模板、日志目录）。
   - 通过 `wget` 从 GitHub 仓库下载核心运行文件：`sync_daemon.py`、Web 面板代码和模板、requirements 依赖文件，并放到指定目录。
   - 自动生成基础配置文件 `config.json`（同步规则、rclone 服务名、并发数、重试次数等）和 Web 面板的 `.env`（API 密钥、允许访问来源、Flask Session 秘钥等），并设定合理的权限。
   - 创建低权限用户 `web-panel`，分配 Web 面板目录和日志目录的所有权，限制该用户权限。
   - 在 Web 面板目录创建 Python 虚拟环境并安装 `requirements.txt` 中的依赖，确保 Web 服务独立运行。
   - 写入并启用 `sync.service` 与 `web-panel.service` 两个 systemd 服务，配置好启动参数、日志输出和自动重启策略。
   - 写入 sudoers 和 polkit 规则，允许 `web-panel` 用户在受控条件下管理 `sync.service`，方便通过 Web 面板控制守护进程。
   - 最后刷新 systemd 配置、启用并启动两个服务，并输出当前运行状态以及常用日志查看命令。

## 2. 仍需人工修改 / 配置的地方

1. `config.json` 中的同步规则与 rclone 相关配置：
   - `rclone_service_name` 需要改成实际的 rclone 挂载 systemd 服务名（例如 `rclone-pikpak` 等）。
   - `rules[].source_path` 和 `rules[].dest_path` 必须根据实际挂载路径和本地下载目录进行填写和核对。
   - `rules[].enabled` 在路径确认无误后，从 `false` 改为 `true` 才会真正生效。
   - 修改后需要执行 `sudo systemctl restart sync.service` 使配置生效。

2. Web 面板 `.env` 中的安全相关配置：
   - `WEB_PANEL_API_KEY` 必须改为强随机密码，用作访问 Web 面板的凭据，不能保留默认占位值。
   - `WEB_PANEL_ALLOWED_ORIGINS` 需要填写实际反向代理域名（如 `https://panel.example.com`），否则跨域访问可能无法正常工作。
   - 修改后需执行 `sudo systemctl restart web-panel.service` 重启 Web 面板。

3. 反向代理与访问控制（如使用 Caddy）：
   - 需要根据实际域名、IP、访问策略手动编写 Caddy（或其他反代软件）的配置，将外部请求代理到本机 `127.0.0.1:5000`。
   - 如需限制来源 IP、强制 HTTPS、增加额外安全策略，也需要在反代配置中手动完成。

4. 系统环境前置条件与依赖：
   - 目标机器需要预先安装并配置好 rclone 挂载服务（含对应的 systemd 单元），脚本不会自动创建 rclone 服务。
   - 系统需具备 `python3`、`python3-venv`、`wget` 等基础环境，不满足时需要手动安装后再运行脚本。

5. 权限与安全策略的人工确认：
   - sudoers 和 polkit 规则给予 `web-panel` 用户管理 `sync.service` 的权限，运维人员应在部署后确认这些权限符合本机的安全策略。
   - 如需自定义安装路径、服务用户、端口等，需要根据需求手动调整脚本或相关配置文件。

