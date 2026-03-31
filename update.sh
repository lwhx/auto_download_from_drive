#!/bin/bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 请使用 root 权限运行: sudo ./update.sh"
    exit 1
fi

INSTALL_DIR="/opt/sync"
WEB_DIR="$INSTALL_DIR/web_panel"
SERVICE_USER="web-panel"
RAW_BASE="https://raw.githubusercontent.com/Z1rconium/auto_download_from_drive/main"
TMP_DIR="$(mktemp -d)"
ACTIVE_SERVICES=()

on_exit() {
    local exit_code="$1"

    if [ "$exit_code" -ne 0 ] && [ "${#ACTIVE_SERVICES[@]}" -gt 0 ]; then
        echo ""
        echo "更新失败，尝试恢复已停止的服务..."
        for svc in "${ACTIVE_SERVICES[@]}"; do
            systemctl start "$svc" 2>/dev/null || true
        done
    fi

    rm -rf "$TMP_DIR"

    exit "$exit_code"
}
trap 'on_exit $?' EXIT

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: 缺少依赖命令: $1"
        exit 1
    fi
}

dl() {
    local src="$1" dst="$2"
    local tmp_file="$TMP_DIR/$src"

    mkdir -p "$(dirname "$tmp_file")"
    echo "  -> 下载 $src"
    wget -q -O "$tmp_file" "$RAW_BASE/$src" || {
        echo "ERROR: 下载失败: $src"
        exit 1
    }

    install -D "$tmp_file" "$dst"
}

service_exists() {
    local svc="$1"
    local state
    state="$(systemctl show -p LoadState --value "$svc" 2>/dev/null || true)"
    [ "$state" != "not-found" ] && [ -n "$state" ]
}

require_cmd wget
require_cmd python3
require_cmd systemctl

if [ ! -d "$INSTALL_DIR" ] || [ ! -d "$WEB_DIR" ]; then
    echo "ERROR: 未检测到安装目录 $INSTALL_DIR，请先执行 sudo ./start.sh"
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "ERROR: 未检测到系统用户 $SERVICE_USER，请先执行 sudo ./start.sh"
    exit 1
fi

echo "================================================="
echo " auto_download_from_drive 一键更新脚本"
echo "================================================="
echo "更新目录: $INSTALL_DIR"
echo "保留配置: $INSTALL_DIR/config.json, $WEB_DIR/.env"
echo ""

echo "[1/5] 下载最新项目文件..."
dl "sync_daemon.py"                 "$INSTALL_DIR/sync_daemon.py"
dl "web_panel/app.py"               "$WEB_DIR/app.py"
dl "web_panel/rclone_monitor.py"    "$WEB_DIR/rclone_monitor.py"
dl "web_panel/requirements.txt"     "$WEB_DIR/requirements.txt"
dl "web_panel/templates/index.html" "$WEB_DIR/templates/index.html"

echo "[2/5] 停止服务..."
for svc in sync.service web-panel.service; do
    if service_exists "$svc"; then
        if systemctl is-active --quiet "$svc"; then
            ACTIVE_SERVICES+=("$svc")
            systemctl stop "$svc" 2>/dev/null || true
            echo "  -> 已停止 $svc"
        else
            echo "  -> $svc 当前未运行，跳过停止"
        fi
    else
        echo "  -> 未找到 $svc，跳过"
    fi
done

echo "[3/5] 修正权限..."
chown root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"
chown root:root "$INSTALL_DIR/sync_daemon.py"
chmod 644 "$INSTALL_DIR/sync_daemon.py"

if [ -f "$INSTALL_DIR/config.json" ]; then
    chown root:"$SERVICE_USER" "$INSTALL_DIR/config.json"
    chmod 664 "$INSTALL_DIR/config.json"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$WEB_DIR"
chmod 750 "$WEB_DIR"

if [ -f "$WEB_DIR/.env" ]; then
    chmod 600 "$WEB_DIR/.env"
fi

echo "[4/5] 更新 Python 虚拟环境依赖..."
if [ ! -d "$WEB_DIR/venv" ]; then
    python3 -m venv "$WEB_DIR/venv" || {
        echo "ERROR: 虚拟环境创建失败，请先安装 python3-venv"
        exit 1
    }
fi

"$WEB_DIR/venv/bin/pip" install -q --upgrade -r "$WEB_DIR/requirements.txt" || {
    echo "ERROR: Python 依赖更新失败"
    exit 1
}

chown -R "$SERVICE_USER":"$SERVICE_USER" "$WEB_DIR/venv"

echo "[5/5] 启动服务..."
systemctl daemon-reload
for svc in "${ACTIVE_SERVICES[@]}"; do
    if service_exists "$svc"; then
        systemctl restart "$svc"
        echo "  -> 已重启 $svc"
    fi
done

echo ""
echo "================================================="
echo " 更新完成"
echo "================================================="
echo "已保留配置文件:"
echo "  - $INSTALL_DIR/config.json"
echo "  - $WEB_DIR/.env"
echo ""
echo "服务状态:"
if service_exists "sync.service"; then
    systemctl is-active sync.service && echo "  [OK] sync.service       运行中" || echo "  [!!] sync.service       未运行"
fi
if service_exists "web-panel.service"; then
    systemctl is-active web-panel.service && echo "  [OK] web-panel.service  运行中" || echo "  [!!] web-panel.service  未运行"
fi
