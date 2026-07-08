# FinFlow 邊緣運算系統 —— 完整部署指南（v3）

> 這份文件是第⑤項「最終彙整」的產出。**先說明一個重要的範疇澄清**：你原本要求「一份完整的程式碼」，但這個系統本質上是分散在多台機器上的多個獨立程式（Oracle 上的伺服器、N 個邊緣節點上的腳本），硬要塞成一個檔案會讓你誤以為這是單機程式，反而不利於理解與部署。所以「完整」在這裡的意思是：**一份涵蓋所有元件、依序執行就能跑起來的完整部署包**，不是字面上的一個 .py 檔案。

---

## 系統元件總覽

```
Oracle Cloud（持久核心）
├── main.py              任務佇列伺服器（FastAPI，只 bind 127.0.0.1）
├── Caddyfile             反向代理設定，對外提供 HTTPS:443
├── setup-https.sh        一次性安裝 Caddy
├── finflow-queue.service  systemd，確保 SSH 斷線後伺服器持續運行
└── requirements.txt

邊緣節點（Kaggle / Colab / Lightning AI，輪流或併行執行）
├── bootstrap.py          六階段啟動腳本（含 VRAM 偵測、Per-node 金鑰）
└── requirements.txt
```

---

## 部署步驟（依序執行）

### Step 1：Oracle 端 —— 安裝任務佇列伺服器

```bash
# 1. 上傳檔案到 Oracle VM（finflow-queue 整個資料夾）
scp -r finflow-queue/ ubuntu@<你的Oracle公開IP>:/home/ubuntu/

# 2. SSH 進去安裝依賴
ssh ubuntu@<你的Oracle公開IP>
cd /home/ubuntu/finflow-queue
pip3 install -r requirements.txt
```

### Step 2：Oracle 端 —— 規劃你的金鑰（這是 v3 跟之前版本最大的差異）

v3 起，金鑰分成兩種角色，你需要先想清楚要開幾個邊緣節點，每個都要先取一個 node_id 並配一把專屬金鑰：

```bash
# 範例：產生隨機金鑰的小技巧
python3 -c "import secrets; print(secrets.token_hex(16))"
```

決定好之後，編輯 `finflow-queue.service` 裡的這兩行：

```
Environment=CLIENT_API_KEY=<給你自己的開發工具用，例如 Cline/Qwen Code 連線時用這把>
Environment=NODE_API_KEYS_JSON={"kaggle-1":"<金鑰A>","lightning-1":"<金鑰B>","colab-1":"<金鑰C>"}
```

**這代表你「新增一個邊緣節點」的正式流程，從現在開始是：先在這裡登記 node_id + 金鑰，重啟服務生效，再去那個平台啟動 bootstrap.py**，不是反過來。這是 Per-node 金鑰設計換來的代價（多一步手動登記），用來交換「能單獨撤掉某一個節點存取權」的能力。

### Step 3：Oracle 端 —— 設定為常駐服務

```bash
sudo cp finflow-queue.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable finflow-queue
sudo systemctl start finflow-queue
sudo systemctl status finflow-queue   # 確認 active (running)
```

### Step 4：Oracle 端 —— 啟用 HTTPS

```bash
chmod +x setup-https.sh
sudo ./setup-https.sh
```

跑完後**別忘了腳本提醒你的事**：還要手動到 OCI 控制台的 Security List 開 443 port，這一步腳本做不到。

驗證：

```bash
curl -k https://<你的Oracle公開IP>/healthz
# 預期回應：{"ok":true}
```

### Step 5：邊緣節點端 —— 在 Kaggle / Colab / Lightning AI 啟動

以 Kaggle 為例，在 Notebook 第一個 Cell：

```python
import os
os.environ["ORACLE_URL"] = "https://<你的Oracle公開IP>"
os.environ["NODE_ID"] = "kaggle-1"                    # 必須跟 Step 2 登記的一致
os.environ["NODE_API_KEY"] = "<金鑰A>"                  # 必須跟 Step 2 登記的一致
os.environ["MODEL_NAME"] = "qwen2.5-coder:32b"
os.environ["VERIFY_TLS"] = "false"                     # 方案A自簽憑證預設值

!pip install -r requirements.txt -q
!python bootstrap.py
```

Colab、Lightning AI 同樣的腳本，只是 `NODE_ID`／`NODE_API_KEY` 換成對應登記的那一組（colab-1、lightning-1）。

### Step 6：驗證整條鏈路

```bash
# 在你自己的電腦（或任何能連到 Oracle 的地方）
curl -k https://<Oracle公開IP>/nodes -H "x-api-key: <CLIENT_API_KEY>"
# 應該能看到剛剛啟動的節點，alive: true

curl -k -X POST https://<Oracle公開IP>/v1/chat/completions \
  -H "x-api-key: <CLIENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"請說一句話確認你收到了"}]}'
```

### Step 7：把開發工具指向 Oracle

在 Cline / Continue.dev 等工具的設定裡，把 base_url 設成 `https://<Oracle公開IP>/v1`，API Key 填 `CLIENT_API_KEY`。若工具的 HTTPS 客戶端預設會驗證憑證（方案A自簽憑證會被拒絕），多數工具有「忽略憑證驗證」的選項；若沒有，建議改用方案 B（自己的網域）或方案 C（Cloudflare Tunnel）。

---

## 方案 C 補充說明：Cloudflare Tunnel（如果你想完全不開 Oracle 的對外 port）

這是 Caddyfile 裡提到、但需要額外安裝才能用的方案，適合你想要「連 Security List 都不想開」的情況：

```bash
# 在 Oracle VM 上
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo ./cloudflared service install <你的Tunnel Token，從 Cloudflare 後台取得>
```

設定完成後，Cloudflare 會給你一個公開網址，流量是 Oracle 主動連出去 Cloudflare、再由 Cloudflare 轉進來，**不需要開任何 Oracle 對外 port，也不需要自簽憑證**，TLS 由 Cloudflare 處理，憑證是真正受信任的。代價：你需要一個掛在 Cloudflare 的網域（網域本身通常一年數百元，不是完全免費，但 Tunnel 服務免費）。這個方案的詳細網域註冊與 Cloudflare 後台設定步驟，因為涉及你個人帳號操作介面，建議你實際要採用這條路時再個別詢問，這裡先讓你知道這條路存在且原理是什麼。

---

## 最終完成度確認

| # | 項目 | 狀態 |
|---|------|------|
| 1 | 任務管理系統（記錄/分配/收集/資源評估） | ✅ |
| 2 | API 閘道彙整端點評估與實作 | ✅ |
| 3 | 邊緣節點啟動腳本（六階段） | ✅ |
| 4a | 逾時重新分配 | ✅ |
| 4b | 多節點併行排程評估（結論：不交給AI即時判斷，改用確定性 DAG） | ✅ |
| 4c | 跨節點 Context 遺失（Session 管理 + 自動壓縮） | ✅ |
| 4d | 資源不足告警（Telegram，含 LINE Notify 停運澄清） | ✅ |
| 5 | 安全性（Per-node 金鑰、常數時間比較、HTTPS 三方案） | ✅ |
| 6 | 落地順序重新評估（推翻 Gemini 靜態分工，改用能力動態路由） | ✅ |
| 7 | 完整系統清單與部署包 | ✅（本文件） |

**仍標記為「尚未做」、刻意留白、不打算現在補的部分**（誠實列出，不是遺漏）：
- 速率限制（rate limiting）尚未實作——目前規模（你一人 + 少數節點）風險低，等真正有外部使用者再做
- 模型異質性目前只做到「可觀測」（`/nodes` 可查每個節點載入的模型），沒做到「跨節點輸出品質自動評分比較」，這需要額外的評估機制，現階段investment/value比偏低
- 真正處理金融機構客戶資料時，這整套免費架構都不適用，前面已多次強調，這裡再次提醒避免你之後忘記
