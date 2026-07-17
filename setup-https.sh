#!/bin/bash
# FinFlow HTTPS 設定腳本 —— Oracle Linux 9 (aarch64) 版本
# 在 Oracle Cloud VM 上以 sudo 執行
# 用途：安裝 Caddy 反向代理，把 server.py 的 8000 port 包在 HTTPS（443）後面
#
# 執行前提：
#   1. server.py 已透過 systemd 服務啟動（uvicorn --host 127.0.0.1 --port 8000）
#   2. 已將本目錄的 Caddyfile 上傳到 Oracle VM
#   3. /home/opc/finflow-queue/finflow-queue.env 已存在，且已填入 ORACLE_PUBLIC_IP
#      （真實公開 IP 不寫死在 Caddyfile／本腳本裡，改從這份不進版本控制的 env
#      檔讀取，避免又發生跟先前 bot-gateway.service 金鑰一樣的外洩問題）

set -e

ENV_FILE="/home/opc/finflow-queue/finflow-queue.env"

echo "=== 步驟 0：讀取 Oracle 公開 IP（來自 finflow-queue.env） ==="
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR：找不到 $ENV_FILE，請先照 DEPLOY.md 的 Step 2 建立這個檔案"
    exit 1
fi

ORACLE_PUBLIC_IP=$(grep -E '^ORACLE_PUBLIC_IP=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '[:space:]')
if [ -z "$ORACLE_PUBLIC_IP" ]; then
    echo "ERROR：$ENV_FILE 裡的 ORACLE_PUBLIC_IP 是空的，請先填入你的 Oracle 公開 IP 再重新執行"
    exit 1
fi
echo "使用的公開 IP：$ORACLE_PUBLIC_IP"

# DOMAIN_NAME 是選填（只有走 Step 4-A Cloudflare 代管憑證才需要），這裡不強制
# 檢查存在與否——留空是合法狀態（純方案 A 自簽憑證），不應該擋下腳本執行。
# Caddy 進程本身不需要這裡額外處理：caddy-override.conf 用 EnvironmentFile
# 載入整份 finflow-queue.env，DOMAIN_NAME 會跟 ORACLE_PUBLIC_IP 一樣自動生效，
# 這裡讀取單純是為了最後驗證步驟能順手印出對應的 curl 指令。
DOMAIN_NAME=$(grep -E '^DOMAIN_NAME=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '[:space:]')
if [ -n "$DOMAIN_NAME" ]; then
    echo "偵測到網域：$DOMAIN_NAME（已設定 DOMAIN_NAME，稍後會一併印出對應的驗證指令）"
fi

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

echo "=== 步驟 4：讓 Caddy 服務讀得到 ORACLE_PUBLIC_IP ==="
# Caddyfile 裡用 {$ORACLE_PUBLIC_IP} 引用這個環境變數，但 caddy 套件安裝的
# caddy.service 預設不會載入 finflow-queue.env，這裡用 drop-in override 補上。
# override 的內容維護在獨立檔案 caddy-override.conf 裡（可被 git 追蹤、單獨
# review diff），不再內嵌於本腳本的 heredoc 中。
if [ ! -f caddy-override.conf ]; then
    echo "ERROR：找不到 caddy-override.conf，請確認它在同一目錄下"
    exit 1
fi
sudo mkdir -p /etc/systemd/system/caddy.service.d
sudo cp caddy-override.conf /etc/systemd/system/caddy.service.d/override.conf
sudo systemctl daemon-reload

echo "=== 步驟 5：啟動並設定 Caddy 開機自啟 ==="
sudo systemctl enable caddy
sudo systemctl restart caddy
sleep 2
sudo systemctl status caddy --no-pager

echo ""
echo "=== 步驟 6：OS 層防火牆開放 443 ==="
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
echo "  curl -k https://$ORACLE_PUBLIC_IP/healthz"
if [ -n "$DOMAIN_NAME" ]; then
    echo "  curl https://$DOMAIN_NAME/healthz   # 已設定 DOMAIN_NAME，這行不用加 -k；"
    echo "                                        # 走 Cloudflare 的話記得先照 DEPLOY.md Step 4-A"
    echo "                                        # 把橘色雲朵打開、加密模式設成「完整」"
fi
