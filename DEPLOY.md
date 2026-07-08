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
| `Caddyfile` / `setup-https.sh` | 未變更 |

## 已知仍刻意保留的限制（非遺漏，是現階段判斷不值得處理）

- 速率限制（rate limiting）尚未實作
- 跨節點模型輸出品質自動評分尚未實作
- `requested_model`／`type` 欄位目前主要供稽核記錄使用，實際派工仍以 `required_capability` 為準，兩者語意上有重疊但刻意不合併，避免一次改動過多既有欄位語意
