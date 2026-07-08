#!/bin/bash
# FinFlow HTTPS 設定腳本 —— Oracle Linux 9 (aarch64) 版本
# 在 Oracle Cloud VM 上以 sudo 執行
# 用途：安裝 Caddy 反向代理，把 server.py 的 8000 port 包在 HTTPS（443）後面
#
# 執行前提：
#   1. server.py 已透過 systemd 服務啟動（uvicorn --host 127.0.0.1 --port 8000）
#   2. 已將本目錄的 Caddyfile 上傳到 Oracle VM

set -e

echo "=== 步驟 1：確認 dnf 環境 ==="
sudo dnf makecache --quiet

echo "=== 步驟 2：安裝 Caddy (Oracle Linux 9 / RHEL 方式) ==="
# 方式：直接加 Caddy 官方 COPR repo
sudo dnf install -y 'dnf-command(copr)' 2>/dev/null || true
sudo dnf copr enable @caddy/caddy -y

sudo dnf install -y caddy
caddy version && echo "Caddy 安裝成功"

echo "=== 步驟 3：套用 Caddyfile ==="
# 確認 Caddyfile 存在
if [ ! -f Caddyfile ]; then
    echo "ERROR：找不到 Caddyfile，請確認它在同一目錄下"
    exit 1
fi
sudo cp Caddyfile /etc/caddy/Caddyfile

echo "=== 步驟 4：啟動並設定 Caddy 開機自啟 ==="
sudo systemctl enable caddy
sudo systemctl restart caddy
sleep 2
sudo systemctl status caddy --no-pager

echo ""
echo "=== 步驟 5：OS 層防火牆開放 443 ==="
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-all

echo ""
echo "=== 完成 ==="
echo "接下來還需要手動到 OCI 控制台完成以下設定："
echo "  Networking → Virtual Cloud Networks → ai-computing-edge-vcn"
echo "  → Security Lists → Default Security List"
echo "  → Add Ingress Rules："
echo "    Source CIDR: 0.0.0.0/0"
echo "    Protocol: TCP"
echo "    Destination Port Range: 443"
echo ""
echo "設定完成後執行驗證："
echo "  curl -k https://127.0.0.1/healthz"
echo "  curl -k https://158.101.16.137/healthz"
