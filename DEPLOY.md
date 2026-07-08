# FinFlow 分散式邊緣運算系統部署指南（v4，合併 Gemini 修改版後）

> 本文件取代前一版 DEPLOY.md。主要差異：改用 venv 部署（迴避 Ubuntu 新版 pip 限制）、
> 檔名由 main.py 改為 server.py、**不需要額外設定 cron**（維護邏輯已改回自動背景執行緒）。

---

## Step 1：Oracle 核心端安裝

由於 Ubuntu 較新版本對系統層級 `pip install` 有限制（PEP 668），請使用虛擬環境：

```bash
mkdir -p /home/ubuntu/finflow-queue && cd /home/ubuntu/finflow-queue
# 將 server.py 上傳至此資料夾
sudo apt update && sudo apt install -y python3-venv
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pydantic requests
```

## Step 2：規劃金鑰

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"   # 重複執行產生每把金鑰
```

決定好 `CLIENT_API_KEY`（給你自己的開發工具用）與每個邊緣節點各自的金鑰後，**先把它們登記起來**，這是 Per-node 金鑰設計換來「能單獨撤掉某個節點」的代價（見前述對話的詳細說明）。

## Step 3：設定為常駐服務

```bash
sudo tee /etc/systemd/system/finflow-queue.service << 'SERVICEEOF'
[Unit]
Description=FinFlow Edge Queue Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/finflow-queue
Environment=CLIENT_API_KEY=change-me-to-your-own-client-secret
Environment=NODE_API_KEYS_JSON={"kaggle-1":"change-me-A","lightning-1":"change-me-B","colab-1":"change-me-C"}
Environment=QUEUE_DB_PATH=/home/ubuntu/finflow-queue/finflow_queue.db
Environment=TELEGRAM_BOT_TOKEN=
Environment=TELEGRAM_CHAT_ID=
ExecStart=/home/ubuntu/finflow-queue/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=ubuntu
WorkingDirectory=/home/ubuntu/finflow-queue

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable finflow-queue
sudo systemctl start finflow-queue
sudo systemctl status finflow-queue
```

**不需要額外設定 cron 排程**——容錯巡檢（逾時重排、DLQ、資源枯竭通知）已在 server.py 啟動時自動以背景執行緒每 15 秒跑一次，`/system/cron` 端點只是保留給你手動觸發測試用（已加上 `CLIENT_API_KEY` 驗證）。

## Step 4：啟用 HTTPS

沿用前一版的 `Caddyfile` / `setup-https.sh`，內容未變（這部分跟本次的 server.py/bootstrap.py 修正無關）：

```bash
chmod +x setup-https.sh
sudo ./setup-https.sh
```

別忘了到 OCI 控制台的 Security List 開放 443 port，這一步腳本做不到。

## Step 4.5：部署 Bot Gateway（Telegram / LINE middleware，補上 8001 的洞）

`Caddyfile` 裡的 `/telegram/*`、`/line/*` 會轉發到 `127.0.0.1:8001`，這一步就是把監聽在
8001 的服務建起來。**必須先完成 Step 1-4（Oracle 核心端 + HTTPS）**，因為這個服務會呼叫
內部的 `127.0.0.1:8000`。

```bash
mkdir -p /home/opc/bot-gateway && cd /home/opc/bot-gateway
# 將 bot_gateway.py、requirements.txt 上傳至此資料夾
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 建立 Telegram Bot（若要用 Telegram）

1. 跟 [@BotFather](https://t.me/BotFather) 對話，`/newbot` 取得 `TELEGRAM_BOT_TOKEN`。
2. 自己隨機產生一組 `TELEGRAM_WEBHOOK_SECRET`（跟 Step 2 的金鑰產生方式一樣：
   `python3 -c "import secrets; print(secrets.token_hex(16))"`），這是防止有人假冒
   Telegram 直接打你的 webhook 用的。
3. 呼叫 Telegram API 註冊 webhook（把 `<TOKEN>`、`<SECRET>`、`<你的網域或IP>` 換成實際值）：
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=https://<你的網域或IP>/telegram/webhook" \
     -d "secret_token=<SECRET>"
   ```
   若用方案 A 的自簽憑證，Telegram 官方 API 不接受自簽憑證的 webhook URL——這是
   **方案 A 的已知限制**，Telegram webhook 要嘛用方案 B（正式網域 + Let's Encrypt），
   要嘛用方案 C（Cloudflare Tunnel，Cloudflare 邊緣本身就有正式憑證）。LINE 則沒有這個限制。

### 建立 LINE Bot（若要用 LINE）

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 建立 Messaging API
   channel，取得 `Channel secret`（給 `LINE_CHANNEL_SECRET`）與
   `Channel access token`（給 `LINE_CHANNEL_ACCESS_TOKEN`）。
2. 在 Console 的 Webhook URL 欄位填入 `https://<你的網域>/line/webhook`，並開啟
   「Use webhook」。同樣受方案 A 自簽憑證限制，建議用方案 B 或 C。
3. **額度提醒**：LINE Messaging API 每月僅 200 則免費訊息（push），超過需付費，
   詳見前述架構規劃文件第 4d 節的說明；若預期用量大，優先用 Telegram。

### 設定為常駐服務

```bash
sudo tee /etc/systemd/system/bot-gateway.service << 'SERVICEEOF'
[Unit]
Description=FinFlow Bot Gateway (Telegram/LINE webhook middleware)
After=network.target finflow-queue.service
Requires=finflow-queue.service

[Service]
Type=simple
WorkingDirectory=/home/opc/bot-gateway
Environment=ORACLE_INTERNAL_URL=http://127.0.0.1:8000
Environment=CLIENT_API_KEY=<跟 finflow-queue.service 裡的 CLIENT_API_KEY 完全一致>
Environment=TELEGRAM_BOT_TOKEN=<你的 Telegram Bot Token>
Environment=TELEGRAM_WEBHOOK_SECRET=<你自訂的 webhook secret>
Environment=LINE_CHANNEL_SECRET=<你的 LINE Channel Secret>
Environment=LINE_CHANNEL_ACCESS_TOKEN=<你的 LINE Channel Access Token>
Environment=GATEWAY_DB_PATH=/home/opc/bot-gateway/bot_gateway.db
Environment=JOB_WAIT_TIMEOUT_SEC=900
Environment=HISTORY_MAX_MESSAGES=20
ExecStart=/home/opc/bot-gateway/venv/bin/uvicorn bot_gateway:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
User=opc

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable bot-gateway
sudo systemctl start bot-gateway
sudo systemctl status bot-gateway
```

### 驗證

```bash
# 本機健康檢查（bot-gateway 自己的）
curl http://127.0.0.1:8001/healthz

# 經過 Caddy 的路徑（若用方案 A 自簽憑證，記得加 -k）
curl -k https://<Oracle公開IP>/telegram/webhook   # 預期 401（沒帶正確 secret token，屬正常）
```
接著直接傳訊息給你的 Telegram Bot 或 LINE 官方帳號，應該會在 `journalctl -u bot-gateway -f`
看到處理紀錄，並收到回覆。

### 架構取捨說明（避免你日後誤以為是遺漏）

- 對話歷史只做「保留最近 N 則」的簡單截斷，沒有沿用 `/v1/chat/completions` 那套
  AI 摘要壓縮機制——因為 bot-gateway 改走 `/jobs` + 輪詢，才能自訂等待時間（預設
  15 分鐘），不受 `/v1/chat/completions` 內建 90 秒 long-poll 上限影響（邊緣節點
  若還在啟動 Ollama、下載模型，90 秒常常不夠）。
- Webhook 去重（避免重複處理）用行程內記憶體，服務重啟會清空，風險極低（見
  `bot_gateway.py` 檔頭註解的完整說明）。
- LINE 用「replyToken 快速 ACK 一句『處理中』＋ push 送真正答案」的兩段式設計，
  避免 replyToken 過期；Telegram 沒有這個限制，直接等結果送出即可。

---

## Step 5：邊緣節點端啟動

```python
import os
os.environ["ORACLE_URL"] = "https://<你的Oracle公開IP或網域>"
os.environ["NODE_ID"] = "kaggle-1"          # 必須跟 Step 2 登記的一致
os.environ["NODE_API_KEY"] = "<金鑰A>"        # 必須跟 Step 2 登記的一致
os.environ["MODEL_NAME"] = "qwen2.5-coder:14b"

!pip install requests -q
!python bootstrap.py
```

## Step 6：驗證

```bash
curl -k https://<Oracle公開IP>/healthz 2>/dev/null || echo "（若無 /healthz 端點請改用下方 /jobs 測試）"

curl -k -X POST https://<Oracle公開IP>/v1/chat/completions \
  -H "x-api-key: <CLIENT_API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"請說一句話確認你收到了"}]}'
```

---

## 本次（v4）變更紀錄摘要

| 檔案 | 變更 |
|------|------|
| `server.py` | 修正：能力比對死碼、DAG 依賴 vacuous-truth 漏洞、依賴失敗無串聯機制、`/system/cron` 未授權、缺少自動背景巡檢。補回：`/jobs/aggregate`、`POST /jobs`+`GET /jobs/{id}`、Session 自動壓縮。保留：優先權、DLQ、中斷偵測、扁平化 nodes schema |
| `bootstrap.py` | 修正：VRAM 偵測失敗回傳值（0.0→None）、Ollama 啟動等待方式（固定 sleep→主動健康檢查）、Kaggle/Colab 日誌持久化路徑 |
| `DEPLOY.md`（本檔） | 採用 venv 部署、移除 cron 設定步驟（已不需要） |
| `Caddyfile` | 補上：`/telegram/*`、`/line/*` 原本指向不存在的 8001 服務（會 502），現在該服務已建立，路由恢復正常 |
| `setup-https.sh` | 未變更 |
| `server.py` | 新增：`GET /healthz`（無需驗證，供 Caddy / 監控腳本探活用；先前 DEPLOY.md 引用此端點但實際不存在） |
| `bot-gateway/bot_gateway.py`（新增） | Telegram / LINE webhook middleware，監聽 8001，驗證簽章後轉發進佇列、非阻塞輪詢、推播回覆 |

## 已知仍刻意保留的限制（非遺漏，是現階段判斷不值得處理）

- 速率限制（rate limiting）尚未實作
- 跨節點模型輸出品質自動評分尚未實作
- `requested_model`／`type` 欄位目前主要供稽核記錄使用，實際派工仍以 `required_capability` 為準，兩者語意上有重疊但刻意不合併，避免一次改動過多既有欄位語意
