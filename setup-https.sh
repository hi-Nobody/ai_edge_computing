#!/bin/bash
# FinFlow HTTPS 設定腳本 —— 在 Oracle Cloud VM 上以 sudo 執行
# 用途：安裝 Caddy 反向代理，把 main.py 的 8000 port 包在 HTTPS（443）後面
#
# 執行前提：main.py 已用 `uvicorn main:app --host 127.0.0.1 --port 8000` 啟動
#          （注意 host 是 127.0.0.1，不再對外直接暴露 8000，所有對外流量都先經過 Caddy）

set -e

echo "=== 步驟 1：安裝 Caddy ==="
sudo apt update
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

echo "=== 步驟 2：套用 Caddyfile（預設方案 A：自簽憑證）==="
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

echo "=== 步驟 3：提醒 —— Oracle 的網路設定有兩層，兩層都要開 443 ==="
echo "    (a) OS 層防火牆（iptables，Ubuntu 預設可能已開放，可用以下指令確認）："
echo "        sudo iptables -L INPUT -n | grep 443"
echo "    (b) Oracle Cloud 控制台的 Security List / Network Security Group："
echo "        必須手動到 OCI 控制台 -> VCN -> Security Lists 新增 Ingress Rule："
echo "        Source: 0.0.0.0/0, Protocol: TCP, Destination Port: 443"
echo "        這一步是雲端層級的設定，這支腳本沒辦法幫你自動做，"
echo "        漏掉這一步是最常見的『設定完還是連不上』的原因"
echo ""
echo "=== 步驟 4：驗證 ==="
echo "    curl -k https://localhost/healthz   # -k 是因為自簽憑證，正常應該還是會回 {\"ok\":true}"
echo ""
echo "=== 完成 ==="
echo "Caddy 自簽憑證已啟用。邊緣節點端的 bootstrap.py 請確認 VERIFY_TLS=false（預設值）。"
echo "若改用方案 B（自己的網域），請編輯 Caddyfile 並改回 bootstrap.py 的 VERIFY_TLS=true。"
