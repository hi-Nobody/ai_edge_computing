# FinFlow 分散式邊緣運算系統部署指南（v10，bot 閘道相關檔案整理進 bot-gateway/ 後）

> 本文件取代前一版 DEPLOY.md。主要差異：改用 venv 部署（迴避 Ubuntu 新版 pip 限制）、
> 檔名由 main.py 改為 server.py、**不需要額外設定 cron**（維護邏輯已改回自動背景執行緒）、
> **新增 Discord Slash Command 支援**（`bot_gateway.py` 補上 `/discord/interactions`
> 端點，可與 Telegram/LINE 並存或互相切換）、**`bootstrap.py` 改用 `edge.conf` 集中管理設定**
> （支援 CLI 參數臨時覆蓋，見「Step 5」）、**新增 `g4f_worker.py` 虛擬節點**（不需要 GPU，
> 用 g4f 逆向 API 當作額外一個運算節點，見「Step 5.5」）、**`Caddyfile`／`setup-https.sh`
> 的 Oracle 公開 IP 改從 `finflow-queue.env` 的 `ORACLE_PUBLIC_IP` 讀取**，不再寫死在會被
> commit 的檔案裡（見「Step 4」）、**邊緣運算相關檔案（`bootstrap.py`／`edge.conf`／
> `g4f_worker.py`）整理進 `edge-worker/` 資料夾**，`server.py` 恢復 `GET /nodes` 監控端點、
> **`bot_gateway.py`／`bot-gateway.service`／`register_discord_commands.py`／
> bot 專用 `requirements.txt` 四個檔案整理進 `bot-gateway/` 資料夾**（見「Step 4.5」，VM 上的
> 部署路徑同步從 `/home/opc/ui-bot` 改為 `/home/opc/bot-gateway`）、`bot-gateway.service`
> 改用 `EnvironmentFile` 讀取 `finflow-queue.env`，不再把金鑰明碼寫在 unit file 裡（跟
> `finflow-queue.service` 同一套修法，兩個服務現在共用同一份機敏設定檔）。
>
> 另外根據實際部署經驗補充：本文件範例路徑統一使用 `/home/opc`（OCI 預設使用者）。
> 若你在 **Oracle Linux** 上部署，實測會遇到 **SELinux**
> 擋下 `EnvironmentFile=` 指向 `/home/opc/...` 底下檔案的狀況（`systemd` 的
> `init_t` domain 預設不能讀取一般家目錄的 `user_home_t` 檔案），導致
> `systemctl restart` 出現「Job ... failed because of unavailable resources or
> another system error」。修法是幫該檔案加上正確的 SELinux context，**不要**
> 直接關掉 SELinux：
> ```bash
> sudo semanage fcontext -a -t systemd_unit_file_t "/home/opc/finflow-queue/finflow-queue.env"
> sudo restorecon -v /home/opc/finflow-queue/finflow-queue.env
> ```
> `finflow-queue.service` 與 `bot-gateway.service` 現在共用同一份 `finflow-queue.env`，
> 上面這個 relabel 只需要做一次，兩個服務都會受惠，不需要對 `bot-gateway.service`
> 再另外處理一次 `EnvironmentFile` 的 SELinux context。
>
> 但 `bot-gateway/venv/`（Python 執行檔本身）需要**另外**relabel，跟 `EnvironmentFile`
> 是不同的坑（一個是「讀設定檔」被擋，一個是「執行程式」被擋）：
> ```bash
> sudo semanage fcontext -a -t bin_t '/home/opc/bot-gateway/venv/bin(/.*)?'
> sudo restorecon -Rv /home/opc/bot-gateway/venv/bin
> sudo semanage fcontext -a -t lib_t '/home/opc/bot-gateway/venv/lib(/.*)?\.so(\.[0-9]+)*'
> sudo restorecon -Rv /home/opc/bot-gateway/venv/lib
> ```

---

## Step 1：Oracle 核心端安裝

本文件範例路徑統一使用 `/home/opc`（Oracle Linux 上 OCI 預設的使用者），並用虛擬環境安裝
（不論是 Ubuntu 新版 pip 的 PEP 668 限制，或是 Oracle Linux，用 venv 都是最省事的做法）：

```bash
mkdir -p /home/opc/finflow-queue && cd /home/opc/finflow-queue
# 將 server.py 上傳至此資料夾
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pydantic requests
```

## Step 2：規劃金鑰

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"   # 重複執行產生每把金鑰
```

決定好 `CLIENT_API_KEY`（給你自己的開發工具用）與每個邊緣節點各自的金鑰後，**先把它們登記起來**，這是 Per-node 金鑰設計換來「能單獨撤掉某個節點」的代價（見前述對話的詳細說明）。

把這些金鑰填進 `/home/opc/finflow-queue/finflow-queue.env`（**這個檔案不要 commit 進版本控制**，
repo 裡的 `finflow-queue.env` 只是空值佔位的範本，實機上請填入真實值）：

```bash
CLIENT_API_KEY=<你剛產生的金鑰>
NODE_API_KEYS_JSON={"kaggle-1":"<金鑰A>","lightning-1":"<金鑰B>","colab-1":"<金鑰C>"}
QUEUE_DB_PATH=/home/opc/finflow-queue/finflow_queue.db
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ORACLE_PUBLIC_IP=<你的 Oracle 執行個體公開 IP，Step 4 的 setup-https.sh／Caddyfile 會讀這個值>
```

## Step 3：設定為常駐服務

`finflow-queue.service` 用 `EnvironmentFile=` 讀取上一步的 `finflow-queue.env`，**真實金鑰不會出現在
這份會被 commit 的 unit file 裡**（這也是為什麼上一步特別提醒 `finflow-queue.env` 不要 commit：
兩者搭配才能讓「秘密值」跟「服務設定」分離）：

```bash
sudo cp /home/opc/finflow-queue/finflow-queue.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable finflow-queue
sudo systemctl start finflow-queue
sudo systemctl status finflow-queue
```

`finflow-queue.service` 內容如下（repo 裡已經是這份，不需要再手動 `tee`）：

```ini
[Unit]
Description=FinFlow Edge Queue Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/opc/finflow-queue
EnvironmentFile=/home/opc/finflow-queue/finflow-queue.env
ExecStart=/home/opc/finflow-queue/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=opc

[Install]
WantedBy=multi-user.target
```

**Oracle Linux 上請記得先做 SELinux 修正**（見文件最上方的說明），否則 `EnvironmentFile=` 指向
`/home/opc` 底下的檔案會被 SELinux 擋下，出現「Job ... failed because of unavailable resources or
another system error」：

```bash
sudo semanage fcontext -a -t systemd_unit_file_t "/home/opc/finflow-queue/finflow-queue.env"
sudo restorecon -v /home/opc/finflow-queue/finflow-queue.env
```

**不需要額外設定 cron 排程**——容錯巡檢（逾時重排、DLQ、資源枯竭通知）已在 server.py 啟動時自動以背景執行緒每 15 秒跑一次，`/system/cron` 端點只是保留給你手動觸發測試用（已加上 `CLIENT_API_KEY` 驗證）。

## Step 4：啟用 HTTPS

`Caddyfile` 跟 `setup-https.sh` 現在都改成從 `finflow-queue.env` 讀 `ORACLE_PUBLIC_IP`，
不再把真實公開 IP 寫死進這兩個會被 commit 的檔案（用法跟 Step 3 的金鑰分離是同一套邏輯）。
執行前**務必先確認 Step 2 的 `finflow-queue.env` 裡 `ORACLE_PUBLIC_IP` 已經填好**，腳本會在
一開始檢查這個值，沒填會直接報錯退出：

```bash
chmod +x setup-https.sh
sudo ./setup-https.sh
```

腳本內部流程：讀取 `ORACLE_PUBLIC_IP` → 安裝 Caddy → 複製 `Caddyfile` 到
`/etc/caddy/Caddyfile`（檔案內容是 `{$ORACLE_PUBLIC_IP}` 佔位符，不是真實 IP）→
幫 `caddy.service` 建立一個 systemd drop-in（`EnvironmentFile=finflow-queue.env`），
讓 Caddy 進程啟動時能讀到這個環境變數並代入 `{$ORACLE_PUBLIC_IP}` → 啟動 Caddy →
開防火牆。

別忘了到 OCI 控制台的 Security List 開放 443 port，這一步腳本做不到。

## Step 4.5：部署 Bot Gateway（Telegram / LINE middleware，補上 8001 的洞）

`Caddyfile` 裡的 `/telegram/*`、`/line/*` 會轉發到 `127.0.0.1:8001`，這一步就是把監聽在
8001 的服務建起來。**必須先完成 Step 1-4（Oracle 核心端 + HTTPS）**，因為這個服務會呼叫
內部的 `127.0.0.1:8000`。

```bash
mkdir -p /home/opc/bot-gateway && cd /home/opc/bot-gateway
# 把 repo 裡 bot-gateway/ 資料夾底下的四個檔案（bot_gateway.py、bot-gateway.service、
# register_discord_commands.py、requirements.txt）整包上傳至此資料夾。
# bot-gateway.service 稍後會複製到 /etc/systemd/system/，不需要留在這裡執行；
# register_discord_commands.py 只有註冊 Discord 指令時手動執行一次，
# 不屬於常駐服務的一部分，放同資料夾純粹方便管理。
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
sudo cp bot-gateway.service /etc/systemd/system/bot-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable bot-gateway
sudo systemctl start bot-gateway
sudo systemctl status bot-gateway
```

`bot-gateway.service` 內容如下（已包含在 `bot-gateway/` 資料夾裡，通常不需要手動修改；
機敏設定如 `CLIENT_API_KEY`、各平台金鑰都從 `finflow-queue.env` 讀取，跟
`finflow-queue.service` 共用同一份，不會出現在這個 unit file 裡）：

```ini
[Unit]
Description=FinFlow Bot Gateway (Telegram/LINE/Discord webhook middleware)
After=network.target finflow-queue.service
Requires=finflow-queue.service

[Service]
Type=simple
WorkingDirectory=/home/opc/bot-gateway
EnvironmentFile=/home/opc/finflow-queue/finflow-queue.env
Environment=ORACLE_INTERNAL_URL=http://127.0.0.1:8000
Environment=GATEWAY_DB_PATH=/home/opc/bot-gateway/bot_gateway.db
Environment=JOB_WAIT_TIMEOUT_SEC=900
Environment=HISTORY_MAX_MESSAGES=20
ExecStart=/home/opc/bot-gateway/venv/bin/uvicorn bot_gateway:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
User=opc

[Install]
WantedBy=multi-user.target
```

部署前，記得先在 `finflow-queue.env` 裡把 `TELEGRAM_WEBHOOK_SECRET`、
`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`DISCORD_PUBLIC_KEY`
這幾個欄位填好（`TELEGRAM_BOT_TOKEN` 跟 `server.py` 共用同一個變數，若沒填過
也要填），否則對應平台的驗證會全部失敗。

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

`bootstrap.py`、`edge.conf`、`g4f_worker.py`、`requirements.txt` 都收在 repo 的
`edge-worker/` 資料夾裡。`bootstrap.py` 讀 `edge.conf` 集中管理設定，優先順序是
**CLI 參數 > 環境變數 > edge.conf > 預設值**，三種都可以混用；`load_conf()` 預設
會找「跟 `bootstrap.py` 同一個資料夾」下的 `edge.conf`，不管你是保留
`edge-worker/` 這層結構、還是只把這兩個檔案單獨複製到 Kaggle/Colab 的工作目錄，
都一樣找得到：

```bash
# 方式 A：把 edge.conf 內容改好後直接跑（長期在同一台機器上管理最方便）
# 從 repo 的 edge-worker/ 資料夾把 bootstrap.py、edge.conf 一起複製到工作目錄
# （兩個檔案要放在同一層，不用管理它是不是還在 edge-worker/ 底下），
# 編輯 edge.conf 填入：
#   ORACLE_URL、NODE_ID、NODE_API_KEY（必須跟 Step 2 登記的一致）、MODEL_NAME
pip install -r edge-worker/requirements.txt -q
python edge-worker/bootstrap.py
```

```python
# 方式 B：Kaggle/Colab Notebook 常用的一次性寫法，不需要另外上傳 edge.conf
import os
os.environ["ORACLE_URL"] = "https://<你的Oracle公開IP或網域>"
os.environ["NODE_ID"] = "kaggle-1"          # 必須跟 Step 2 登記的一致
os.environ["NODE_API_KEY"] = "<金鑰A>"        # 必須跟 Step 2 登記的一致
os.environ["MODEL_NAME"] = "qwen2.5-coder:14b"

!pip install requests -q
!python bootstrap.py
```

```bash
# 方式 C：CLI 參數臨時切換模型／節點身分（測試不同模型時最方便，不用改檔案）
python bootstrap.py --model qwen3:8b
python bootstrap.py --model deepseek-coder-v2:16b --node-id colab-1
```

## Step 5.5：（選用）g4f 虛擬節點——不需要 GPU 的額外運算節點

`g4f_worker.py`（在 `edge-worker/` 資料夾裡）是另一種「節點」：不呼叫本地 Ollama，
而是透過 `g4f`（gpt4free）套件轉打免費的第三方網頁模型端點，適合拿來當備援或測試用，
**不需要顯卡、不需要另外的機器**——通常直接跟 `server.py` 部署在同一台 Oracle 主機上，
把它當成一個「虛擬」邊緣節點。

```bash
cd /home/opc/finflow-queue   # 或任何你方便管理的資料夾
pip install -r edge-worker/requirements.txt   # 已含 g4f、requests

export G4F_NODE_API_KEY="<金鑰D，記得先照 Step 2 的方式登記進 NODE_API_KEYS_JSON>"
python edge-worker/g4f_worker.py
```

**已知限制**：`g4f` 套件呼叫的是免費第三方端點，穩定性、速度、可用模型都不受你控制，
逾時（預設 60 秒）或端點掛掉都是正常會發生的狀況，程式已經處理成「失敗就回報 error
給佇列，讓佇列走正常的重試/DLQ 流程」，不需要額外介入。長期穩定用途還是建議以
Ollama 邊緣節點（Step 5）為主，這個當備援。

## Step 6：驗證

```bash
curl -k https://<Oracle公開IP>/healthz 2>/dev/null || echo "（若無 /healthz 端點請改用下方 /jobs 測試）"

curl -k -X POST https://<Oracle公開IP>/v1/chat/completions \
  -H "x-api-key: <CLIENT_API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"請說一句話確認你收到了"}]}'
```

---

## 變更紀錄摘要

### v10（本次）

| 檔案 | 變更 |
|------|------|
| `bot-gateway/`（新增資料夾） | 把 `bot_gateway.py`、`bot-gateway.service`、`register_discord_commands.py`、bot 專用 `requirements.txt` 四個檔案整理進同一個資料夾，跟根目錄的 Oracle 核心端程式（`server.py`）分開，也跟 `edge-worker/` 的邊緣節點程式分開。四個檔案原本散落在根目錄，現在統一收進 `bot-gateway/` 底下（`requirements.txt` 直接放在這一層，不需要再多一層子資料夾） |
| `bot-gateway.service` | 路徑改為 `/home/opc/bot-gateway`（`WorkingDirectory`／`GATEWAY_DB_PATH`／`ExecStart`）。systemd 服務名稱維持 `bot-gateway.service` 不變（`systemctl status bot-gateway` 等指令不受影響），只有部署目錄跟著 repo 結構調整 |
| `Caddyfile` | 註解裡的 `bot-gateway/` 路徑說明更新為 `bot-gateway/`（純文字說明，`reverse_proxy 127.0.0.1:8001` 這個實際轉發目標不受資料夾搬家影響） |
| `finflow-queue.env` | 補上 `TELEGRAM_WEBHOOK_SECRET`、`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`DISCORD_PUBLIC_KEY`，讓 `bot-gateway.service` 的機敏設定也能從這份共用檔案讀取 |
| `bot-gateway.service` | 改用 `EnvironmentFile=/home/opc/finflow-queue/finflow-queue.env` 讀取機敏設定，不再把金鑰明碼寫在 unit file 裡；非機敏的部署參數（`ORACLE_INTERNAL_URL`／`GATEWAY_DB_PATH`／`JOB_WAIT_TIMEOUT_SEC`／`HISTORY_MAX_MESSAGES`）維持用 `Environment=` |
| `register_discord_commands.py` | 修正文件說明中「一次性腳本」的誤導表述，澄清 Global Command 註冊後會自動套用到 Bot 之後加入的任何新伺服器；新增 `--list`／`--delete` 子指令方便查詢與維護既有指令 |
| `DEPLOY.md`（本檔） | Step 4.5 部署路徑同步改為 `bot-gateway/`；「設定為常駐服務」段落改成直接複製 `bot-gateway.service`（不再用 heredoc 內嵌一份跟實際檔案不同步的舊版內容）；補上 `bot-gateway/venv/` 的 SELinux relabel 步驟；修正檔頭已過時的「`bot-gateway.service` 仍用 inline `Environment=`」備註 |

### v9（本次）

這次上傳的репо快照裡，`server.py`／`requirements.txt`／`bot-gateway.service` 這三個檔案
其實是接在比較舊的基礎上（沒有 v6 的修正），所以先重新補上這幾個修正，另外處理你提出的
兩個新問題（邊緣運算相關檔案盤點、搬進 `edge-worker/`）。

| 檔案 | 變更 |
|------|------|
| `server.py` | 補回 `GET /nodes` 監控端點（這次上傳的版本又缺了這個，v6 修過的東西被沖掉了，重新補上） |
| `requirements.txt` | 重新拆分：根目錄改回 `fastapi`/`uvicorn`/`pydantic`/`requests`（`server.py` 用），原本誤植的 bot-gateway 依賴移到 `bot-gateway/requirements.txt` |
| `bot-gateway/requirements.txt`（新增） | 承接上一項，內容為 `fastapi`/`uvicorn`/`httpx`/`pynacl` |
| `bot-gateway.service` | 重新修正：移除又出現的真實 `CLIENT_API_KEY` 明碼（改回佔位字串）；`Description` 補上 Discord 說明 |
| （移除）`finflow-bot-gateway-updates.zip` | 又混進 repo 的暫存檔，再次移除 |
| `edge-worker/bootstrap.py`（搬移） | 從根目錄搬進 `edge-worker/`；`load_conf()` 預設路徑改成「跟 `bootstrap.py` 同一資料夾」而非「目前工作目錄」，這樣不管保留 `edge-worker/` 結構或單獨複製到 Kaggle/Colab 工作目錄都找得到 `edge.conf` |
| `edge-worker/edge.conf`（搬移） | 從根目錄搬進 `edge-worker/`，內容不變 |
| `edge-worker/g4f_worker.py`（搬移） | 從根目錄搬進 `edge-worker/`；沒有讀外部設定檔，搬移不影響行為 |
| `edge-worker/requirements.txt` | 位置不變（本來就在這個資料夾），內容已包含 `requests`、`g4f` |
| `DEPLOY.md`（本檔） | 升級至 v9；Step 5／5.5 的路徑改為 `edge-worker/bootstrap.py`、`edge-worker/g4f_worker.py`、`edge-worker/requirements.txt` |

**問題1 回答**——除了 `bootstrap.py`、`edge.conf`，屬於邊緣運算（跑在 Kaggle/Colab/Lightning
或作為虛擬節點）的檔案還有：`g4f_worker.py`（不需要 GPU 的虛擬節點，跟 `bootstrap.py` 一樣會呼叫
`/nodes/heartbeat`、`/jobs/next`、`/jobs/{id}/result`）、`edge-worker/requirements.txt`
（這兩支程式共用的依賴清單）。`server.py`、`bot_gateway.py`、`register_discord_commands.py`、
`Caddyfile`、`setup-https.sh`、`finflow-queue.service`、`finflow-queue.env`、
`bot-gateway.service` 都是核心端／閘道端，不屬於邊緣運算範疇，沒有搬動。

**問題2 回答**——只有 `bootstrap.py` 需要改路徑相依性：它原本用相對路徑 `"edge.conf"` 找設定檔，
相對的是「執行時的目前工作目錄」，不是「檔案自己的位置」。搬進 `edge-worker/` 後，如果你在
repo 根目錄執行 `python edge-worker/bootstrap.py`，目前工作目錄是根目錄，相對路徑
`"edge.conf"` 會去根目錄找，那裡已經沒有這個檔案了，會找不到、悄悄用預設值跑（不會報錯，
但設定形同沒生效，是個容易忽略的地雷）。已修正為用 `os.path.dirname(os.path.abspath(__file__))`
算出 `bootstrap.py` 自己的所在目錄，不管從哪裡呼叫都找得到同目錄下的 `edge.conf`。
`g4f_worker.py`、`edge-worker/requirements.txt` 沒有路徑相依問題，搬移不影響行為。

### v8（本次）

延續 v7 的思路（秘密值跟設定檔分離），這次把 `Caddyfile`／`setup-https.sh` 裡寫死的
Oracle 公開 IP 也搬進 `finflow-queue.env`，理由：這兩個檔案先前都直接把真實公開 IP
明碼寫進版本控制，公開 repo 等於公告了你的伺服器位置。

| 檔案 | 變更 |
|------|------|
| `finflow-queue.env` | 新增 `ORACLE_PUBLIC_IP=`（空值佔位），並附註說明用途 |
| `Caddyfile` | 方案 A 的 domain 清單從寫死的 `158.101.16.137` 改成 Caddy 原生支援的環境變數佔位符 `{$ORACLE_PUBLIC_IP}`；`10.0.0.152`（VCN 內部私有 IP，非機敏資訊）維持寫死 |
| `setup-https.sh` | 新增「步驟 0」：從 `/home/opc/finflow-queue/finflow-queue.env` 讀取 `ORACLE_PUBLIC_IP`，沒填就直接報錯退出，不會用空值繼續跑；新增「步驟 4」：幫套件安裝的 `caddy.service` 建立 systemd drop-in（`/etc/systemd/system/caddy.service.d/override.conf`，`EnvironmentFile=` 指向同一份 env 檔），讓 Caddy 進程啟動時能讀到 `ORACLE_PUBLIC_IP` 並代入 Caddyfile 裡的 `{$ORACLE_PUBLIC_IP}`；結尾驗證用的 curl 指令改用讀到的變數，不再寫死 IP |
| `DEPLOY.md`（本檔） | Step 2 的 `finflow-queue.env` 範例補上 `ORACLE_PUBLIC_IP`；Step 4 改寫，說明新的讀取流程與執行前置條件 |

**運作原理小記**：Caddyfile 原生就支援 `{$ENV_VAR}` 這種語法在載入設定檔時代入環境變數，
不需要額外套件；唯一麻煩的地方是 Oracle Linux 的 `caddy` 套件安裝的 `caddy.service` 是
套件維護者提供的，我們不會、也不應該直接改動套件檔案本身，所以用 systemd 的 drop-in
override 機制（`/etc/systemd/system/caddy.service.d/`）疊加一條 `EnvironmentFile=`，
這是 systemd 官方支援、不會在套件更新時被覆蓋掉的做法。

### v7（本次，合併 upload_files.zip 有價值的部分）

這次是把兩份壓縮檔的內容合併：`ai_edge_computing-main.zip`（較新，含 Discord/GET-nodes/
requirements.txt 修正）保留為基礎，再挑 `upload_files.zip` 裡確實比較新、比較好的部分併入。

| 檔案 | 變更 |
|------|------|
| `bootstrap.py` | **改用新版**：從 `edge.conf` 讀取設定（`load_conf()`），優先順序 CLI 參數 > 環境變數 > edge.conf > 預設值；同時修掉了先前 v5 版本遺留的已知問題（推論例外現在會立即透過 `submit_result(error=...)` 回報，不再是整圈靜默重試）。**合併時修正**：預設 `ORACLE_URL` 原本寫死真實公開 IP，已改為佔位字串 |
| `edge.conf`（新增） | 邊緣節點設定範本。**合併時修正**：原始版本裡的 `NODE_API_KEY`、`ORACLE_URL` 是真實外洩值，已換成佔位字串 |
| `finflow-queue.env`（新增） | 佇列伺服器設定範本，內容本來就是空值佔位，直接沿用 |
| `finflow-queue.service` | 改用 `EnvironmentFile=/home/opc/finflow-queue/finflow-queue.env`，取代原本內嵌明碼的 `Environment=` 寫法，真實金鑰不會再出現在會被 commit 的 unit file 裡（也是先前 `bot-gateway.service` 金鑰外洩問題的同類修法） |
| `g4f_worker.py`（新增） | 不需要 GPU 的 g4f（gpt4free）虛擬節點。**合併時修正兩個會導致完全無法運作的 bug**：① `register_node()` 原本送巢狀 `{"capability": {...}}`，跟 `server.py` 的 `HeartbeatRequest` 扁平 schema（`platform`/`current_model`/`vram_gb`）對不起來，會被 422 拒絕，已改成送正確的扁平欄位；② 原本用 `res.json().get("job")` 解析任務，但 `GET /jobs/next` 實際回傳的是扁平的 `{"job_id":...,"payload":...}`，沒有 `"job"` 這一層，永遠拿不到任務，已修正為直接讀 `job_id`/`payload` |
| `g4f-worker-requirements.txt`（新增） | `g4f_worker.py` 需要的 `g4f`、`requests` 套件，原本沒有對應的 requirements 檔 |
| `DEPLOY.md`（本檔） | Step 1-3 改為搭配 `finflow-queue.env` 的部署方式；新增 Step 5 的 `edge.conf`／CLI 參數用法說明；新增 Step 5.5 說明 `g4f_worker.py` 部署方式與已知限制 |

**這次沒有採用的部分**（因為比 `ai_edge_computing-main.zip` 舊，會造成功能倒退）：
`upload_files.zip` 裡的 `server.py`、`bot_gateway.py`、`requirements.txt`、`Caddyfile`、
`bot-gateway.service` 都維持用 `ai_edge_computing-main.zip` 的版本，沒有覆蓋。

### v6（本次）

| 檔案 | 變更 |
|------|------|
| `server.py` | 補回 `GET /nodes` 監控端點（`main.py` 原本有，`server.py` 重構時漏掉）；回傳格式對應新版扁平化 `nodes` schema，組成巢狀 `capability` 物件維持對舊呼叫端相容 |
| `requirements.txt` | **修正誤植**：這份原本其實是 bot-gateway 專用依賴（`fastapi`/`uvicorn`/`httpx`/`pynacl`），長期放在根目錄，導致 `server.py` 實際需要的 `requests`（Telegram 通知用）、`pydantic` 版本鎖定沒有被追蹤。改回 `fastapi`/`uvicorn`/`pydantic`/`requests` |
| `bot-gateway/requirements.txt`（新增） | 原本誤放在根目錄的 bot-gateway 依賴，移到專屬子資料夾 |
| `bot-gateway.service` | **安全性修正**：移除誤上傳進版本庫的真實 `CLIENT_API_KEY` 明碼，改回 `change-me-to-your-own-client-secret` 佔位字串（該金鑰已外洩，實機上務必重新產生新金鑰並更新 `finflow-queue.service`／`bot-gateway.service`，兩邊都要同步改）；`Description` 補上 Discord 說明 |
| `Caddyfile` | 方案 B（自訂網域）範例區塊補上遺漏的 `/discord/*` 路由——原本只有 `/telegram/*`、`/line/*`，但 Discord 恰好是唯一「必須用方案 B 或 C」的平台，範例卻沒示範，屬邏輯疏漏 |
| `DEPLOY.md`（本檔） | Step 1／`finflow-queue.service` 範例路徑統一改為 `/home/opc`（原本殘留 `/home/ubuntu`，跟 bot-gateway 段落、跟 repo 裡實際的 `finflow-queue.service`／`bot-gateway.service` 不一致）；移除 heredoc 內重複的 `WorkingDirectory=` 行；移除 Ubuntu `apt` 安裝指令（環境已確認是 Oracle Linux，一律用 venv 即可） |
| （移除）`finflow-bot-gateway-updates.zip` | 先前對話產生的暫存壓縮檔被誤含在 repo 裡，予以移除，避免內容跟實際檔案不同步造成混淆 |

### v5

| 檔案 | 變更 |
|------|------|
| `bot_gateway.py` | 新增 Discord 支援：`POST /discord/interactions` 端點，Ed25519 簽章驗證（`DISCORD_PUBLIC_KEY`）、Slash Command `/ask` 的 deferred 回應 + 背景任務 + interaction token followup 編輯訊息 |
| `register_discord_commands.py`（新增） | 一次性腳本，呼叫 Discord API 註冊 `/ask` 這個 Slash Command（Global 或指定 Guild） |
| `requirements.txt` | 新增 `pynacl`（Discord Ed25519 簽章驗證需要）——**此條目在 v6 已修正為誤植，詳見上方 v6 說明** |
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
