# FinFlow 分散式邊緣運算系統部署指南（v5，補上 Discord 支援後）

> 本文件取代前一版 DEPLOY.md。主要差異：改用 venv 部署（迴避 Ubuntu 新版 pip 限制）、
> 檔名由 main.py 改為 server.py、**不需要額外設定 cron**（維護邏輯已改回自動背景執行緒）、
> **新增 Discord Slash Command 支援**（`bot_gateway.py` 補上 `/discord/interactions`
> 端點，可與 Telegram/LINE 並存或互相切換）。
>
> 另外根據實際部署經驗補充：本文件範例路徑用 `/home/ubuntu`，但如果你是在
> **Oracle Linux**（OCI 的 `opc` 使用者常見於此）上部署，實測會遇到 **SELinux**
> 擋下 `EnvironmentFile=` 指向 `/home/opc/...` 底下檔案的狀況（`systemd` 的
> `init_t` domain 預設不能讀取一般家目錄的 `user_home_t` 檔案），導致
> `systemctl restart` 出現「Job ... failed because of unavailable resources or
> another system error」。修法是幫該檔案加上正確的 SELinux context，**不要**
> 直接關掉 SELinux：
> ```bash
> sudo semanage fcontext -a -t systemd_unit_file_t "/home/opc/finflow-queue/finflow-queue.env"
> sudo restorecon -v /home/opc/finflow-queue/finflow-queue.env
> ```
> （若 `bot-gateway.service` 之後也改用 `EnvironmentFile=` 而非目前的 inline
> `Environment=`，記得對那個檔案也做一次同樣的處理。）

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
# 若要用 Discord，也把 register_discord_commands.py 一併上傳（僅註冊指令時
# 一次性執行，不屬於常駐服務的一部分，放同資料夾方便管理即可）
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # 已含 pynacl，Discord 簽章驗證需要
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

### 建立 Discord Bot（若要用 Discord）

Discord 跟 Telegram/LINE 的架構不一樣：Discord 沒有「使用者傳訊息就觸發 webhook」
這種機制（那是 Gateway WebSocket 常駐連線的範疇），本專案改用 Discord 的
**Slash Command + Interactions Endpoint**（使用者輸入 `/ask prompt:<內容>`
觸發，HTTP 一次性請求，跟 Telegram/LINE 一樣是無狀態服務就能處理）。

1. 到 [Discord Developer Portal](https://discord.com/developers/applications)
   建立一個 New Application，記下：
   - **Application ID**（General Information 頁籤）→ 給 `DISCORD_APPLICATION_ID`
     （只有註冊指令的腳本需要，服務本身不需要）
   - **Public Key**（同一頁）→ 給 `DISCORD_PUBLIC_KEY`（服務驗證簽章要用）
   - 到 Bot 頁籤按 Reset Token 拿到 **Bot Token** → 給 `DISCORD_BOT_TOKEN`
     （同樣只有註冊指令的腳本需要）
2. 註冊 Slash Command（一次性動作，之後改指令內容才需要重跑）：
   ```bash
   export DISCORD_BOT_TOKEN="<Bot Token>"
   export DISCORD_APPLICATION_ID="<Application ID>"
   # 若想先在自己的測試伺服器立即生效，設定這個（否則 Global Command 最多要等 1 小時）：
   # export DISCORD_GUILD_ID="<你的測試伺服器 ID>"
   python3 register_discord_commands.py
   ```
3. 到 General Information 頁籤，把 **Interactions Endpoint URL** 填成
   `https://<你的網域>/discord/interactions`，儲存時 Discord 會立刻打一次
   PING 過去驗證簽章與連線是否正常（`bot_gateway.py` 要先跑起來才能通過這一步）。
   **這一步只接受受信任 CA 簽發的 TLS 憑證，方案 A 的自簽憑證會驗證失敗**，
   請改用方案 B（正式網域 + Let's Encrypt）或方案 C（Cloudflare Tunnel）。
4. 到 OAuth2 → URL Generator，勾選 `applications.commands`（如果要在伺服器
   中使用還要勾 `bot` 並給基本權限），產生邀請連結，把 Bot 加進你的伺服器。

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
Environment=DISCORD_PUBLIC_KEY=<你的 Discord Public Key>
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
curl https://<你的網域>/discord/interactions       # 預期 401（沒帶正確 Ed25519 簽章標頭，屬正常；
                                                    # 這裡故意不加 -k，因為方案 A 自簽憑證對
                                                    # Discord 本來就不適用，見上方 Discord 小節說明）
```
接著直接傳訊息給你的 Telegram Bot、LINE 官方帳號，或在已加入 Bot 的 Discord 伺服器輸入
`/ask prompt:你好`，應該會在 `journalctl -u bot-gateway -f` 看到處理紀錄，並收到回覆。
Discord 的部分，先看到訊息顯示「思考中…」（deferred 回應），幾秒到幾分鐘後
（視邊緣節點忙碌程度）會被編輯成真正的答案。

### 架構取捨說明（避免你日後誤以為是遺漏）

- 對話歷史只做「保留最近 N 則」的簡單截斷，沒有沿用 `/v1/chat/completions` 那套
  AI 摘要壓縮機制——因為 bot-gateway 改走 `/jobs` + 輪詢，才能自訂等待時間（預設
  15 分鐘），不受 `/v1/chat/completions` 內建 90 秒 long-poll 上限影響（邊緣節點
  若還在啟動 Ollama、下載模型，90 秒常常不夠）。
- Webhook 去重（避免重複處理）用行程內記憶體，服務重啟會清空，風險極低（見
  `bot_gateway.py` 檔頭註解的完整說明）。
- LINE 用「replyToken 快速 ACK 一句『處理中』＋ push 送真正答案」的兩段式設計，
  避免 replyToken 過期；Telegram 沒有這個限制，直接等結果送出即可。
- Discord 用「deferred 回應（顯示『思考中…』）＋ interaction token followup 編輯」
  的兩段式設計，概念上跟 LINE 類似，但技術機制不同：Discord 的 3 秒回應
  時限比 LINE replyToken 更嚴格，且 followup 編輯有效期是 15 分鐘（對應
  `JOB_WAIT_TIMEOUT_SEC` 預設值），逾時後即使任務算完成也無法再編輯那則訊息，
  只能算逾時失敗。

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

## 變更紀錄摘要

### v5（本次）

| 檔案 | 變更 |
|------|------|
| `bot_gateway.py` | 新增 Discord 支援：`POST /discord/interactions` 端點，Ed25519 簽章驗證（`DISCORD_PUBLIC_KEY`）、Slash Command `/ask` 的 deferred 回應 + 背景任務 + interaction token followup 編輯訊息 |
| `register_discord_commands.py`（新增） | 一次性腳本，呼叫 Discord API 註冊 `/ask` 這個 Slash Command（Global 或指定 Guild） |
| `requirements.txt` | 新增 `pynacl`（Discord Ed25519 簽章驗證需要） |
| `bot-gateway.service` | 新增 `DISCORD_PUBLIC_KEY` 環境變數 |
| `Caddyfile` | 註解更新：`/discord/*` 路由現在有實際服務接手；補充 Discord Interactions Endpoint 不接受自簽憑證（方案 A）的限制 |
| `DEPLOY.md`（本檔） | 新增「建立 Discord Bot」小節；補充 Oracle Linux 上 `EnvironmentFile=` 搭配 SELinux 的已知問題與修法（實際部署時遇到並排除） |

### v4

| 檔案 | 變更 |
|------|------|
| `server.py` | 修正：能力比對死碼、DAG 依賴 vacuous-truth 漏洞、依賴失敗無串聯機制、`/system/cron` 未授權、缺少自動背景巡檢。補回：`/jobs/aggregate`、`POST /jobs`+`GET /jobs/{id}`、Session 自動壓縮。保留：優先權、DLQ、中斷偵測、扁平化 nodes schema。新增：`GET /healthz`（無需驗證，供 Caddy / 監控腳本探活用；先前 DEPLOY.md 引用此端點但實際不存在） |
| `bootstrap.py` | 修正：VRAM 偵測失敗回傳值（0.0→None）、Ollama 啟動等待方式（固定 sleep→主動健康檢查）、Kaggle/Colab 日誌持久化路徑 |
| `DEPLOY.md`（本檔） | 採用 venv 部署、移除 cron 設定步驟（已不需要） |
| `Caddyfile` | 補上：`/telegram/*`、`/line/*` 原本指向不存在的 8001 服務（會 502），現在該服務已建立，路由恢復正常 |
| `setup-https.sh` | 未變更 |
| `bot-gateway/bot_gateway.py`（新增） | Telegram / LINE webhook middleware，監聽 8001，驗證簽章後轉發進佇列、非阻塞輪詢、推播回覆 |

## 已知仍刻意保留的限制（非遺漏，是現階段判斷不值得處理）

- 速率限制（rate limiting）尚未實作
- 跨節點模型輸出品質自動評分尚未實作
- Discord 只做了單一 Slash Command（`/ask`），沒有多指令（如 `/status` 查詢
  節點狀態、`/cancel` 取消任務）；也沒有處理按鈕、Modal 等其他 interaction 類型
- `requested_model`／`type` 欄位目前主要供稽核記錄使用，實際派工仍以 `required_capability` 為準，兩者語意上有重疊但刻意不合併，避免一次改動過多既有欄位語意
