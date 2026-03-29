#!/bin/bash
# 自动安装与运行 Web Panel (通过 systemd)

if [ "$EUID" -ne 0 ]; then
  echo "请使用 root 权限运行此脚本 (sudo ./start.sh)"
  exit 1
fi

cd "$(dirname "$0")"
APP_DIR="/root/sync/web_panel"

echo "================================================="
echo "开始安装 Web 管理面板..."
echo "================================================="

if [ ! -d "venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv venv || { echo "虚拟环境创建失败！若是 Debian/Ubuntu 请先运行: apt install python3-venv"; exit 1; }
fi

if [ ! -f "venv/installed" ]; then
    echo "安装依赖..."
    ./venv/bin/python3 -m pip install -r requirements.txt || { echo "依赖安装失败！请清理掉坏的 venv（rm -rf venv）并确保安装了 python3-pip"; exit 1; }
    touch venv/installed
fi

# Create logs directory
mkdir -p /var/log/web-panel
chmod 750 /var/log/web-panel

# Generate systemd service file
SERVICE_FILE="/etc/systemd/system/web-panel.service"
echo "生成 Systemd 服务文件: $SERVICE_FILE"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Web Panel for Sync Daemon
After=network.target sync.service
Wants=sync.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --bind 127.0.0.1:5000 --timeout 300 --graceful-timeout 2 --access-logfile /var/log/web-panel/access.log --error-logfile /var/log/web-panel/error.log --log-level info app:app
Restart=always
RestartSec=5
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "重载 Systemd 并启动服务..."
systemctl daemon-reload
systemctl enable web-panel.service
systemctl restart web-panel.service

echo ""
echo "================================================="
echo "✅ Web 管理面板已成功配置为完全后台运行！"
echo "状态检查: systemctl status web-panel.service"
echo "配置 Nginx反向代理后即可访问对应域名"
echo "依赖服务: sync.service"
echo "================================================="
