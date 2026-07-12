# FinFlow 分散式邊緣運算系統部署指南（v18，Step 4 改寫為完整教學，新增 Cloudflare 方案 D）

> 本輪（v18）把原本過於簡略的「Step 4：啟用 HTTPS」整段改寫成手把手教學：從 OCI
> Cloud Shell 怎麼連進 Oracle VM 開始，`setup-https.sh` 內部每一步在做什麼都拆成表格
> 逐條解釋，OCI 主控台開放 443 的每個點擊位置也寫清楚。新增「Step 4-D：改用 Cloudflare
> 代管憑證」，把先前對話中討論過的 Cloudflare Proxy＋Full 加密模式接法正式寫進文件，
> 作為 Discord 需要受信任憑證時的解法（原本方案 A 的自簽憑證無法滿足 Discord
> Interactions Endpoint 的要求）。另外新增「Step 4 疑難排解」，把這幾輪對話裡真的
>踩過的坑（無 SNI 找不到憑證、Caddyfile 語法錯誤、環境變數替換成空字串、203/EXEC）
> 整理成對照表，方便之後重新部署或交接給別人時快速定位問題。

> 前一版（v17）差異：Step 1 統一改用 `pip install -r requirements.txt`（原本是直接
> `pip install fastapi uvicorn pydantic requests`，跟 `bot-gateway/` 的安裝方式不一致，
> 已確認並統一）；Step 4.5 補上 `bot-gateway/venv` 的 SELinux relabel 就地說明。

> 本輪（v17）處理上一版留下的待確認事項與兩個文件缺口：**Step 1 確認改為 `pip install -r
> requirements.txt`**（原本用 `pip install fastapi uvicorn pydantic requests` 純粹是舊版
> 遺留寫法，不是刻意設計），並在旁邊列出這份根目錄 `requirements.txt` 實際裝了什麼、為何
> 需要每一個套件；Step 4.5 的 `bot-gateway/venv` SELinux relabel 原本只有指令、要「見文件
> 開頭」才有完整原因，這次把完整原因（`user_home_t` vs `init_t`／`bin_t`／`lib_t` 的機制）
> 直接寫進 Step 4.5 本身，不用再往上翻；Step 3 補上一句預告，說明 Step 4.5 會重複同一套
> `cp` 進 `/etc/systemd/system/` 的流程；順手修正「建立 Discord Bot」小節裡跟
> `register_discord_commands.py` 實際行為不一致的「一次性動作」表述，並補上文件裡完全沒
> 提過的 `--list`／`--delete` 子指令用法。

> 本輪（v16）是把這份文件拿去跟一份獨立整理的「Step 1-13 部署規劃」逐項核對出來的結果：
> 補上 Step 1 缺少的 git clone 具體教學、新增 Step 0（既有安裝要重新部署時該不該清空重建）、
> Step 6 驗證補強分層檢查與 bot-gateway webhook 路由測試、新增 Step 7（SIT 系統整合測試
> 檢查清單）。另外發現 Step 4.5 的 `bot-gateway/venv` SELinux relabel 指令只寫在檔頭、
> Step 4.5 本身沒有就地提醒，容易被漏做，這次補上。

> 本輪（v15）是實際把 Kaggle 節點接上 Oracle 之後，從「節點心跳正常、但送出的任務永遠
> 卡在 `pending`／`Timeout waiting for edge nodes`」這個現象一路排查出來的，核心是
> `server.py` 裡一個路由宣告順序的 bug，**不是網路、金鑰、或是 timeout 數值設太短的問題**
> （雖然這次也順手把這幾項都優化了）。詳見「Step 3.5：已知過的重大 bug」與下方 v15 變更紀錄。
> 另外新增邊緣節點「閒置自動停止」機制，避免忘記手動關閉 Kaggle/Colab session 而浪費 GPU 配額，
> 詳見 Step 5。

> 本文件取代前一版 DEPLOY.md。本輪（v13）評估過「把 `caddy.service` 併入
> `finflow-queue.service`，減少要維護的環境變數設定檔案數量」這個提案，**決定不合併**：
> 全系統目前只有一份機敏設定檔（`finflow-queue.env`），`finflow-queue.service`／
> `bot-gateway.service` 兩個自己寫的 unit file 直接用 `EnvironmentFile=` 讀取，
> `caddy.service` 因為是 `dnf`/`copr` 套件安裝、不歸這個 repo 管（套件更新會覆蓋
> unit file 本體），只能透過 systemd 官方的 drop-in override 機制外掛一條
> `EnvironmentFile=` 進去——這已經是「只有一份 env 檔」的狀態，真正把三個服務
> 合併成一個 systemd unit 反而會製造新問題：Caddy 需要綁 443 特權 port、跑在
> 專用的 `caddy` 系統帳號下，跟 `finflow-queue.service` 用的 `opc` 帳號、8000
> 這種一般 port 的權限模型不同；systemd 一個 `Type=simple` 服務只能有一個主行程，
> 硬塞兩個長駐行程進同一個 unit 會讓 `systemctl restart`／`journalctl` 沒辦法
> 針對單一服務獨立操作與查log。維持三個服務分開、共用同一份 env 檔，是目前
> 最合理的做法。
>
> 本輪順手把原本埋在 `setup-https.sh` heredoc 裡的 caddy drop-in override 內容，
> 抽成獨立檔案 `caddy-override.conf`（見「Step 4」），可被 git 追蹤、單獨 review
> diff，`setup-https.sh` 改成直接 `cp` 這個檔案。
>
> 前一版（v12）差異：改用 venv 部署（迴避 Ubuntu 新版 pip 限制）、
> 檔名由 main.py 改為 server.py、**不需要額外設定 cron**（維護邏輯已改回自動背景執行緒）、
> **新增 Discord Slash Command 支援**（`bot_gateway.py` 補上 `/discord/interactions`
> 端點，可與 Telegram/LINE 並存或互相切換）、**`bootstrap.py` 改用 `edge.conf` 集中管理設定**
> （支援 CLI 參數臨時覆蓋，見「Step 5」）、**新增 `g4f_worker.py` 虛擬節點**（不需要 GPU，
> 用 g4f 逆向 API 當作額外一個運算節點，見「Step 5.5」）、**`Caddyfile`／`setup-https.sh`
> 的 Oracle 公開 IP 改從 `finflow-queue.env` 的 `ORACLE_PUBLIC_IP` 讀取**，不再寫死在會被
> commit 的檔案裡（見「Step 4」）、**邊緣運算相關檔案（`bootstrap.py`／`edge.conf`）整理進
> `edge-worker/` 資料夾**（`g4f_worker.py` 維持在根目錄，跟 `server.py` 共用同一個 venv，
> 見「Step 5.5」的說明），`server.py` 恢復 `GET /nodes` 監控端點、
> **`bot_gateway.py`／`bot-gateway.service`／`register_discord_commands.py`／
> bot 專用 `requirements.txt` 四個檔案整理進 `bot-gateway/` 資料夾**（見「Step 4.5」）、
> `bot-gateway.service` 改用 `EnvironmentFile` 讀取 `finflow-queue.env`，不再把金鑰明碼寫在
> unit file 裡（跟 `finflow-queue.service` 同一套修法，兩個服務現在共用同一份機敏設定檔）、
> **VM 上的實際部署路徑最終定為 `/home/opc/finflow-queue/bot-gateway`**（巢狀在
> `finflow-queue` 底下，取代先前試過的 `/home/opc/ui-bot`、`/home/opc/bot-gateway`
> 兩種平行擺放的路徑，見版本紀錄 v12 的說明）。
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
> sudo semanage fcontext -a -t bin_t '/home/opc/finflow-queue/bot-gateway/venv/bin(/.*)?'
> sudo restorecon -Rv /home/opc/finflow-queue/bot-gateway/venv/bin
> sudo semanage fcontext -a -t lib_t '/home/opc/finflow-queue/bot-gateway/venv/lib(/.*)?\.so(\.[0-9]+)*'
> sudo restorecon -Rv /home/opc/finflow-queue/bot-gateway/venv/lib
> ```
> 這組 relabel 規則是綁在「路徑」上的，不是綁在「服務」上——如果之後又把
> `bot-gateway/` 資料夾搬到別的地方（哪怕只是搬回本來的 `/home/opc/bot-gateway`），
> 新路徑要重新下一次 `semanage fcontext`／`restorecon`，不會自動沿用；同時 venv
> 內的 `venv/bin/uvicorn` 等進入點腳本的 shebang 也會寫死目前這個絕對路徑，資料夾
> 一旦搬家就得整個 `rm -rf venv` 重建，不能只搬資料夾了事（這是實際部署時繞了
> 兩三輪路徑才踩出來的兩個坑，見版本紀錄 v12）。

---

---

## Step 0：（僅適用於既有安裝要重新部署）要不要清空重建

**只有在你機器上已經有一份舊的安裝、而且這次改動涉及路徑/服務結構調整時才需要看這節**；
全新的機器、第一次安裝的話直接跳到 Step 1。

**建議清空重建的情況**：路徑結構有變（例如 `bot-gateway` 搬過家）、`venv` 疑似損毀或
版本混亂、想確認目前的異常是不是「設定漂移」（累積過多次手動修改、記不清目前實際
狀態）造成的。**不建議的情況**：只是想更新程式碼本身（`server.py`／`bot_gateway.py`
內容變了，但路徑、服務結構都沒變）——這種情況直接覆蓋檔案、`systemctl restart`
即可，不需要大動作。

```bash
sudo systemctl stop finflow-queue bot-gateway caddy 2>/dev/null
sudo systemctl disable finflow-queue bot-gateway 2>/dev/null
sudo rm -f /etc/systemd/system/finflow-queue.service /etc/systemd/system/bot-gateway.service
sudo rm -rf /etc/systemd/system/caddy.service.d
sudo rm -f /etc/caddy/Caddyfile
sudo systemctl daemon-reload

rm -rf /home/opc/finflow-queue
```

**SELinux 的 `semanage fcontext` 規則不用特別清**——它是綁在「路徑字串」上的規則，
不是綁在實際檔案上（見文件開頭版本說明裡 v12 的說明）；只要重建後的路徑跟原本
完全一樣（例如都是 `/home/opc/finflow-queue/...`），舊規則會繼續生效，`restorecon`
照跑即可，不需要重新 `semanage fcontext -a`。只有當**新路徑跟舊路徑不同**時，才需要
針對新路徑重新下一次。

---

## Step 1：Oracle 核心端安裝

本文件範例路徑統一使用 `/home/opc`（Oracle Linux 上 OCI 預設的使用者），並用虛擬環境安裝
（不論是 Ubuntu 新版 pip 的 PEP 668 限制，或是 Oracle Linux，用 venv 都是最省事的做法）：

```bash
sudo dnf install -y git
cd /home/opc
git clone https://github.com/<你的帳號>/ai_edge_computing.git finflow-queue
cd finflow-queue
```
（`clone` 時特別指定資料夾名稱 `finflow-queue`，是為了讓路徑直接對上本文件其餘所有
`/home/opc/finflow-queue/...` 的假設，省得之後每個路徑都要自己換算。如果 repo 是
private，`git clone` 會要求登入——GitHub 已不接受帳密登入，改用 Personal Access
Token 當密碼，或先 `ssh-keygen` 產生金鑰、把公鑰加進 GitHub 帳號的 SSH Keys，
改用 `git clone git@github.com:<帳號>/ai_edge_computing.git finflow-queue`。）

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

這裡的 `requirements.txt`（repo 根目錄那份，`git clone` 時已經一起拉下來了）內容是：

```
fastapi
uvicorn
pydantic
requests
```

`fastapi`／`uvicorn` 是 `server.py` 本身的 Web framework 跟 ASGI server；`pydantic` 是
FastAPI 的請求/回應資料驗證用的（`ChatRequest`、`JobSubmitRequest` 這些 model 都靠它）；
`requests` 則是 `server.py` 第 516 行左右資源枯竭時發 Telegram 告警通知用的——**這份
`requirements.txt` 專屬於 Oracle 核心端（`server.py`）**，不要跟 `bot-gateway/requirements.txt`
（`fastapi`／`uvicorn`／`httpx`／`pynacl`，bot-gateway 專用）搞混，兩者刻意分開維護，
詳見版本紀錄 v6／v9 的說明（根目錄曾經誤放過 bot-gateway 那份，一度讓 `server.py`
少了 `requests`／`pydantic` 的版本鎖定，裝完才在真正觸發時噴 `ImportError`，而不是
安裝當下就發現）。

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

**之後 Step 4.5 部署 `bot-gateway.service` 時，會重複「`cp` 進 `/etc/systemd/system/`、
`daemon-reload`、`enable`、`start`」同一套流程**——這是本專案兩個自建 systemd 服務共通的
標準模式，先在這裡打個預防針，屆時不用覺得奇怪。

## Step 3.5：已知過的重大 bug —— `/jobs/next` 被 `/jobs/{job_id}` 攔截

**症狀**：`GET /nodes` 顯示節點 `alive:true`、心跳正常送達，但透過 `/v1/chat/completions`
或 `POST /jobs` 建立的任務永遠卡在 `pending`，`/v1/chat/completions` 最終回
`{"detail":"Timeout waiting for edge nodes"}`；如果直接手動模擬節點去打
`GET /jobs/next?node_id=<你的節點>`，會發現回傳 `401 {"detail":"Invalid Client API Key"}`——
即使帶的是完全正確的 `NODE_API_KEY`。

**原因**：FastAPI 依「宣告順序」由上往下比對路由。`server.py` 裡如果
`GET /jobs/{job_id}`（萬用路徑參數，要求 `CLIENT_API_KEY`）宣告在
`GET /jobs/next`（節點輪詢專用，要求 `NODE_API_KEY`）**之前**，`/jobs/next` 這個請求
會被前者攔截、把字串 `"next"` 誤判成 `job_id`，並套用錯誤的驗證邏輯——節點端不管帶什麼
key 都會被拒絕，**心跳能過但永遠領不到任務**，是這個 bug 最容易讓人誤判方向的地方（因為
心跳是走另一支獨立的 `/nodes/heartbeat`，不受影響，看起來節點像是「活著但沒工作可做」，
容易誤以為是任務指派邏輯或 capability 比對的問題）。

**確認你手上的 `server.py` 是否已修正**：

```bash
grep -n "^@app.get(\"/jobs" server.py
```

正確順序應該是 `/jobs/next` 在前、`/jobs/{job_id}` 在後：

```
@app.get("/jobs/next")
@app.get("/jobs/{job_id}", dependencies=[Depends(verify_client_key)])
```

如果你的版本反過來，代表用的是 v14（含）以前的 `server.py`，請更新到本輪修正後的版本，
覆蓋後 `sudo systemctl restart finflow-queue` 即可生效，不需要動任何其他設定或金鑰。

## Step 4：啟用 HTTPS

這一節目標：讓 Oracle 上的 `server.py`／`bot_gateway.py`（分別跑在 127.0.0.1:8000／8001，
只聽本機、外部連不到）透過 Caddy 這個反向代理，統一用 443（HTTPS）對外提供服務。
下面從「怎麼連進 Oracle VM」開始，每一條指令都附註解，照抄執行即可。

### 4-1：用 OCI Cloud Shell 連進 Oracle VM

1. 瀏覽器打開 [cloud.oracle.com](https://cloud.oracle.com)，登入你的帳號
2. 畫面**右上角**有一排小圖示，找到一個像「終端機／`>_`」的圖示，點下去——這個就是
   **Cloud Shell**，OCI 直接在瀏覽器裡給你的一個小型 Linux 環境，不需要自己的電腦裝
   任何 SSH 工具
3. Cloud Shell 開起來後（第一次會花約 1 分鐘初始化），輸入以下指令連進你的 Oracle VM：

```bash
# ssh 連線到你的 Oracle Compute 執行個體
# opc 是 Oracle Linux 映像檔預設的管理帳號
# 後面接你的 VM 公開 IP（在 OCI Console → Compute → Instances 頁面可以查到）
ssh opc@158.101.16.137
```

第一次連線會問你要不要信任這台主機的指紋，輸入 `yes` 按 Enter。如果你是用自己的
SSH 金鑰而不是 Cloud Shell 內建的，指令會多一個 `-i` 參數指定金鑰檔案路徑，例如
`ssh -i ~/.ssh/oci_key opc@158.101.16.137`。

連進去之後，命令列提示字元會變成 `[opc@ai-computing-edge ~]$` 這種樣子（`ai-computing-edge`
換成你自己的主機名稱），代表你現在在 Oracle VM 裡面操作，不是在 Cloud Shell 本身了。

### 4-2：進到專案資料夾，確認需要的檔案都在

```bash
# 切換到 repo clone 下來的資料夾（Step 1 已經建立過）
cd /home/opc/finflow-queue

# 列出這一步需要用到的四個檔案，確認都存在
# Caddyfile：反向代理規則設定
# setup-https.sh：自動化安裝腳本
# caddy-override.conf：讓 caddy 服務讀取到公開 IP 的 systemd 外掛設定
# finflow-queue.env：機敏設定檔，ORACLE_PUBLIC_IP 要先填在這裡面
ls Caddyfile setup-https.sh caddy-override.conf finflow-queue.env
```
四個檔名都要被列出來、沒有「No such file or directory」才能繼續。

### 4-3：確認 `ORACLE_PUBLIC_IP` 已經填好

`setup-https.sh` 一啟動就會檢查這個值，沒填會直接報錯中止，先確認：

```bash
# grep 篩選出這一行，快速檢查有沒有填值
grep ORACLE_PUBLIC_IP finflow-queue.env
```
如果等號後面是空的，先編輯填上（把 IP 換成你自己的）：
```bash
# nano 是簡單的文字編輯器，Ctrl+O 存檔、Ctrl+X 離開
nano finflow-queue.env
# 找到這一行，改成：ORACLE_PUBLIC_IP=158.101.16.137
```

### 4-4：執行安裝腳本，逐段解釋它在做什麼

```bash
# 給腳本加上「可執行」權限，不然會出現 Permission denied
chmod +x setup-https.sh

# 用 sudo 執行（腳本內部要 dnf 安裝套件、寫系統設定檔，需要管理員權限）
sudo ./setup-https.sh
```

腳本會依序印出「步驟 0」到「步驟 6」，對應它實際在做的事，逐一說明：

| 腳本步驟 | 實際做的事 | 為什麼要這樣做 |
|---|---|---|
| 步驟 0 | 讀取 `finflow-queue.env` 裡的 `ORACLE_PUBLIC_IP` | 真實公開 IP 不寫死進會被 git commit 的檔案，避免外洩（跟 Step 2 金鑰分離同一套邏輯） |
| 步驟 1 | `sudo dnf makecache` 更新套件索引 | 確保等一下安裝 Caddy 時抓到的是最新版本資訊 |
| 步驟 2 | 加入 Caddy 官方的 COPR repo、`dnf install caddy` | Oracle Linux 官方倉庫沒有 Caddy，COPR 是社群維護的額外套件庫 |
| 步驟 3 | 把 `Caddyfile` 複製到 `/etc/caddy/Caddyfile` | `/etc/caddy/` 才是 Caddy 實際會讀取設定的路徑，repo 裡的只是原始檔案 |
| 步驟 4 | 把 `caddy-override.conf` 複製到 `/etc/systemd/system/caddy.service.d/override.conf`，`daemon-reload` | 讓 `caddy.service` 這個 systemd 服務啟動時也能讀到 `ORACLE_PUBLIC_IP`（詳見文件開頭「為何不合併 caddy.service」的說明） |
| 步驟 5 | `systemctl enable` + `restart caddy` | 開機自動啟動，並用剛剛套用的新設定重啟一次 |
| 步驟 6 | `firewall-cmd` 開放 443 port | Oracle Linux 內建的 OS 層防火牆，跟 OCI 網路層的 Security List 是兩層不同的防火牆，兩層都要開 |

如果任何一步印出 `ERROR` 就會直接停下來，把錯誤訊息貼給我即可。

### 4-5：OCI 主控台開放 443（腳本做不到這步，要手動點）

1. 瀏覽器回到 [cloud.oracle.com](https://cloud.oracle.com) 主控台（不是 Cloud Shell 那個分頁）
2. 左上角「☰」選單 → **Networking** → **Virtual Cloud Networks**
3. 點進你的 VCN（例如 `ai-computing-edge-vcn`）
4. 左側選單找 **Security Lists**，點進去 → 點 **Default Security List**
5. **Add Ingress Rules** 按鈕，填：
   - Source CIDR：`0.0.0.0/0`（代表允許任何來源）
   - IP Protocol：`TCP`
   - Destination Port Range：`443`
6. 存檔

### 4-6：驗證

回到 SSH 進去的那個終端機視窗：
```bash
curl -k https://127.0.0.1/healthz              # 本機測試，-k 表示不驗證憑證（自簽憑證本來就不受信任，這裡先跳過）
curl -k https://158.101.16.137/healthz          # 換成你自己的 IP，測試走公網也通
```
兩個都要回 `{"status":"ok",...}` 才算這一步完成。

---

## Step 4-D：（選用）改用 Cloudflare 代管憑證，取代自簽憑證

如果你已經有網域託管在 Cloudflare（例如 `myproj2.dpdns.org`），可以讓 Cloudflare 幫你
處理對外憑證，這是唯一能滿足 **Discord Interactions Endpoint** 要求受信任憑證的簡便做法
（`tls internal` 自簽憑證會被 Discord 直接拒絕，方案 A 過不了這關）。

**原理**：啟用 Cloudflare 的橘色雲朵代理後，連線變成兩段：
```
Discord / 使用者  ──HTTPS（Cloudflare 的受信任憑證）──▶  Cloudflare  ──HTTPS（可以是自簽）──▶  Oracle
```
外部看到的永遠是 Cloudflare 出示的憑證，Oracle 端可以繼續用現有的 `tls internal` 自簽憑證，
不需要额外去申請或安裝任何新憑證。

**① Cloudflare 加一筆 DNS 記錄，指向 Oracle 公開 IP**

Cloudflare Dashboard → 選你的網域 → **DNS** → **Add record**：
- Type：`A`
- Name：`@`（代表根網域本身）或自訂子網域，例如 `api`
- IPv4 address：你的 Oracle 公開 IP
- Proxy status：**打開橘色雲朵**（這步是關鍵，沒開的話流量不會經過 Cloudflare）

**② 設定加密模式為「完整」（不是「自動 SSL/TLS」）**

Cloudflare Dashboard → **SSL/TLS** → **Overview**，選 **完整（Full）**。
**不要選「自動 SSL/TLS」**——那個模式會定期重新掃描並可能自動升級成「完整（嚴格）」，
一旦升級就會開始驗證 Oracle 端憑證是不是受信任 CA 簽的，你的自簽憑證會驗證失敗，
所有服務會在你沒注意到的情況下突然斷線。「完整」模式明確寫著「不進行憑證驗證，
接受任何憑證，包括自簽憑證」，是固定、可預期的行為。

**③ `Caddyfile` 加上這個網域名稱**

回到 Oracle VM 的 SSH 視窗：
```bash
cd /home/opc/finflow-queue
nano Caddyfile
```
找到站台位址那一行，在後面加上你的網域（逗號分隔，其他 `handle` 區塊都不用動）：
```
127.0.0.1, 10.0.0.152, {$ORACLE_PUBLIC_IP}, myproj2.dpdns.org {
```
存檔後套用：
```bash
sudo caddy validate --config Caddyfile   # 先驗證語法
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

**④ 驗證**
```bash
curl https://myproj2.dpdns.org/healthz   # 注意這次不用加 -k，能正常回應才代表 Cloudflare 憑證真的生效了
```

**⑤（建議，可以晚點做）收緊 OCI Security List**，只允許 Cloudflare 的 IP 段連進 443，
擋掉繞過 Cloudflare、直接打 Oracle 公開 IP 的流量。Cloudflare 官方 IP 清單：
https://www.cloudflare.com/ips/ ——這步不急，先確認前面都跑通再處理。

**`skip_install_trust` 跟 `caddy-override.conf` 都不用因為改用 Cloudflare 而拿掉**：
前者處理的是 Oracle 端自己簽憑證時裝不進系統信任庫的問題，跟 Cloudflare 完全無關、
Oracle 端還是繼續用自簽憑證；後者是因為 `Caddyfile` 站台位址列表裡還留著
`{$ORACLE_PUBLIC_IP}`（保留直連 IP 除錯的能力），只要這個佔位符還在，就還是需要
`caddy-override.conf` 把環境變數餵給 Caddy。真的確定以後只走 Cloudflare 網域、
不需要直連 IP 除錯了，才需要把這個佔位符從位址列表移除。

---

## Step 4 疑難排解：這幾個坑都真實踩過

**`tlsv1 alert internal error`**：通常是位址列表裡少了某個實際會被連到的身分
（例如少了 VM 的私有 IP，或環境變數沒代入成功變成空字串），用
`sudo journalctl -u caddy -f -o cat` 搭配 `curl -kv` 即時看 debug log 裡的
`tls.handshake` 訊息，會明確告訴你 `identifier` 是什麼、有沒有找到對應憑證。

**`Job for caddy.service failed`／`unknown subdirective`**：`Caddyfile` 語法錯誤，
先跑 `sudo caddy validate --config Caddyfile` 會直接告訴你哪一行有問題，不要跳過
這步直接硬套用。

**`Expected another address but had '{'`**：`{$ORACLE_PUBLIC_IP}` 被替換成空字串，
通常是因為用 `sudo caddy validate` 這種不透過 systemd 啟動的方式手動測試，沒有
`caddy-override.conf` 幫忙注入環境變數；手動測試要自己先 `export`：
```bash
export $(grep ORACLE_PUBLIC_IP finflow-queue.env)
sudo -E caddy validate --config Caddyfile
```

**`code=exited, status=203/EXEC`**：跟 HTTPS 本身無關，是 SELinux 擋下執行檔，
或是 systemd unit 裡的路徑寫錯，見 Step 3、Step 4.5 的 SELinux 說明。

## Step 4.5：部署 Bot Gateway（Telegram / LINE middleware，補上 8001 的洞）


`Caddyfile` 裡的 `/telegram/*`、`/line/*` 會轉發到 `127.0.0.1:8001`，這一步就是把監聽在
8001 的服務建起來。**必須先完成 Step 1-4（Oracle 核心端 + HTTPS）**，因為這個服務會呼叫
內部的 `127.0.0.1:8000`。

```bash
mkdir -p /home/opc/finflow-queue/bot-gateway && cd /home/opc/finflow-queue/bot-gateway
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
2. 註冊 Slash Command（**不是嚴格意義上的一次性腳本**：往後想調整指令內容，例如幫
   `/ask` 加新參數，直接改 `register_discord_commands.py` 裡的 `COMMAND_PAYLOAD` 再重新
   執行即可，Discord 會用同名指令覆蓋舊定義，不會重複建立）：
   ```bash
   export DISCORD_BOT_TOKEN="<Bot Token>"
   export DISCORD_APPLICATION_ID="<Application ID>"
   # 若想先在自己的測試伺服器立即生效，設定這個（否則 Global Command 最多要等 1 小時）：
   # export DISCORD_GUILD_ID="<你的測試伺服器 ID>"
   python3 register_discord_commands.py              # 註冊／更新
   python3 register_discord_commands.py --list         # 查看目前已註冊的指令
   python3 register_discord_commands.py --delete ask   # 刪除指定名稱的指令
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

**Oracle Linux 上請先做 SELinux relabel**（實際部署時真的撞過一次，記錄完整原因，不用再往上翻）：

新建立的 `bot-gateway/venv/` 資料夾，SELinux 預設會把裡面所有檔案貼上
`user_home_t` 這個標籤（因為它物理上就在 `/home/opc` 底下）；但 `systemd` 執行
`ExecStart` 指定的程式時，是站在 `init_t` 這個 domain 底下動作，而 `init_t`
在 Oracle Linux 預設政策下**不被允許直接執行**貼著 `user_home_t` 標籤的檔案，
所以 `venv/bin/uvicorn` 這個執行檔本身就會被擋下來，出現 `203/EXEC`。

這跟 Step 3 那次 `EnvironmentFile` 被擋是**不同的坑**：那次是「systemd 讀設定檔」
被擋（`finflow-queue.env` 需要 `systemd_unit_file_t`），這次是「systemd 執行程式」
被擋（`venv/bin/`、`venv/lib/` 底下的執行檔／函式庫需要 `bin_t`／`lib_t`），兩個
relabel 目標不同，不能只做一次就以為兩邊都處理好了：

```bash
sudo semanage fcontext -a -t bin_t '/home/opc/finflow-queue/bot-gateway/venv/bin(/.*)?'
sudo restorecon -Rv /home/opc/finflow-queue/bot-gateway/venv/bin
sudo semanage fcontext -a -t lib_t '/home/opc/finflow-queue/bot-gateway/venv/lib(/.*)?\.so(\.[0-9]+)*'
sudo restorecon -Rv /home/opc/finflow-queue/bot-gateway/venv/lib
```

這組規則是綁在「路徑字串」上的，不是綁在服務或檔案本體上——如果之後又把
`bot-gateway/` 資料夾搬到別的地方，新路徑要重新下一次；同時 venv 內
`venv/bin/uvicorn` 等進入點腳本的 shebang 也會寫死目前這個絕對路徑，資料夾
一旦搬家就得整個 `rm -rf venv` 重建，不能只搬資料夾了事（完整的踩坑過程見
版本紀錄 v12）。

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
WorkingDirectory=/home/opc/finflow-queue/bot-gateway
EnvironmentFile=/home/opc/finflow-queue/finflow-queue.env
Environment=ORACLE_INTERNAL_URL=http://127.0.0.1:8000
Environment=GATEWAY_DB_PATH=/home/opc/finflow-queue/bot-gateway/bot_gateway.db
Environment=JOB_WAIT_TIMEOUT_SEC=900
Environment=HISTORY_MAX_MESSAGES=20
ExecStart=/home/opc/finflow-queue/bot-gateway/venv/bin/uvicorn bot_gateway:app --host 127.0.0.1 --port 8001
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

`bootstrap.py`、`edge.conf`、`requirements.txt` 都收在 repo 的
`edge-worker/` 資料夾裡（`g4f_worker.py` 不在這裡，見下方 Step 5.5 的說明）。
`bootstrap.py` 讀 `edge.conf` 集中管理設定，優先順序是
**CLI 參數 > 環境變數 > edge.conf > 預設值**，三種都可以混用；`load_conf()` 預設
會找「跟 `bootstrap.py` 同一個資料夾」下的 `edge.conf`，不管你是保留
`edge-worker/` 這層結構、還是只把這兩個檔案單獨複製到 Kaggle/Colab 的工作目錄，
都一樣找得到：

節點啟動流程現在會在「向 Oracle 報到」之後、「進入主輪詢迴圈」之前，多一段**模型暖機**
（送一次假的推論請求，讓 Ollama 把模型先載進 VRAM）。這是刻意的行為：避免第一個真正
任務因為模型冷啟動疊加推論時間，超過 Oracle 端的 long-poll timeout。代價是節點啟動到
真正能接任務之間會多花數十秒到一兩分鐘（視模型大小與硬體而定），屬正常現象，log 裡
看到 `模型暖機中...` 停留一陣子不用擔心，等到 `模型暖機完成` 出現就代表沒問題了。

### Kaggle／Colab Notebook 已知雷（跟本專案程式碼無關，但一定會踩到）

這幾個是實際在 Kaggle Notebook 上部署時踩到的環境限制，不是 `bootstrap.py` 的 bug，
但不知道的話會卡很久，記錄下來：

**1. `ollama` 官方安裝腳本在 Kaggle 上會因為缺 `zstd` 失敗**

```
ERROR: This version requires zstd for extraction. Please install zstd and try again
```

Kaggle/Colab 底層是 Ubuntu，裝好 `zstd` 再重跑安裝腳本即可：

```bash
!apt-get update -qq && apt-get install -y zstd -qq
!curl -fsSL https://ollama.com/install.sh | sh
```

安裝完成訊息裡會看到 `WARNING: systemd is not running` 跟
`WARNING: Unable to detect NVIDIA/AMD GPU`，這兩個在 Notebook 容器裡是**正常現象**，
不代表安裝失敗、也不代表真的沒有 GPU：容器沒有 systemd，所以 `ollama serve`
本來就要靠 `bootstrap.py` 自己用 subprocess 啟動（見下方第 3 點）；GPU 警告只是因為
安裝腳本當下環境還沒裝 `lspci`，跟 ollama 實際執行時抓不抓得到 CUDA 是兩回事。

**2. 確認真的有吃到 GPU，不要只看安裝訊息**

```bash
!nvidia-smi        # 確認 Notebook 有配置到 GPU（例如 Tesla T4）
!ollama ps          # 模型載入後，看 PROCESSOR 欄位是不是 "100% GPU"，不是 "100% CPU"
```

`100% CPU` 代表 ollama 沒吃到顯卡，14B/32B 這種模型純 CPU 跑會慢到接近不能用，要另外
排查 CUDA 驅動或 ollama 版本問題。

**3. `!command &`／`!nohup ... &` 在 Kaggle 上會直接報錯，不能拿來背景執行 `bootstrap.py`**

```
OSError: Background processes not supported.
```

Kaggle 的 kernel 對 `!` shell magic 帶 `&`（背景執行）明確擋掉了，這不是語法錯誤，是
平台限制。`bootstrap.py` 是無限迴圈（`worker_loop`），如果直接 `!python bootstrap.py`
前景執行，那個 cell 會永遠轉圈、把 kernel 卡住，沒辦法再跑其他 cell 做驗證。正確做法是
改用 Python 原生的 `subprocess.Popen`（不會被上面那個限制攔到）：

```python
import subprocess, sys

log_file = open("bootstrap.log", "w")
proc = subprocess.Popen(
    [sys.executable, "bootstrap.py"],
    stdout=log_file,
    stderr=subprocess.STDOUT,
)
print("bootstrap.py 已在背景啟動，PID:", proc.pid)
```

這樣這個 cell 會立刻執行完畢，`bootstrap.py` 在背景繼續跑，之後可以正常開其他 cell：

```bash
!tail -20 bootstrap.log          # 看日誌
!ps aux | grep bootstrap.py      # 確認進程還活著
```

**4. 驗證節點是否註冊成功，要用 `CLIENT_API_KEY`，不是 `NODE_API_KEY`**

```bash
!curl -s https://<Oracle公開IP>/nodes -H "x-api-key: <CLIENT_API_KEY>" -k
```

`GET /nodes` 是給你自己查看整體狀態用的監控端點，要求的是 `CLIENT_API_KEY`（你自己
開發工具用的那把）；`NODE_API_KEY`（節點自己拿去心跳/拉任務用的那把）沒有權限呼叫
這個端點，兩者是不同層級的金鑰，帶錯會被拒絕。回應裡看到
`"node_id":"kaggle-1","alive":true` 才代表這個節點真的註冊成功、心跳有送達。

**5. 用 `subprocess.Popen` 背景啟動的那個 cell，千萬不要重複執行**

每執行一次第 3 點的那段 `subprocess.Popen` 程式碼，就會多開一個 `bootstrap.py` process；
如果不小心重複點了那個 cell 好幾次（很常見，尤其是在除錯、重跑 cell 的時候），會變成
好幾個 process 用同一個 `NODE_ID` 同時搶著送心跳、搶著領任務，互相干擾，行為會變得很難
預測（心跳一下被這個蓋過、一下被那個蓋過）。定期檢查有沒有意外疊了多個：

```bash
!ps aux | grep -E "bootstrap.py|ollama serve"
```

如果同一支程式出現超過一個 PID，先全部關掉、確認清空後只重啟一個：

```bash
!kill -TERM <PID1> <PID2> ...     # 優雅關閉，會走程式內建的清理流程
# 等幾秒後再次確認，若還有殘留（少見）再用：
!pkill -9 -f bootstrap.py
!pkill -9 -f "ollama serve"
```

**6. 閒置自動停止（`IDLE_STOP_SEC`）與如何手動停止節點**

`bootstrap.py` 內建閒置偵測：連續 `IDLE_STOP_SEC` 秒（`edge.conf`／環境變數可調，預設
1800 秒＝30 分鐘）沒有任何任務可做，就會自動結束 process（同時關閉自己啟動的
`ollama serve`），避免忘記手動關閉而持續佔用 Kaggle/Colab 的 GPU 配額。設為 `0`
或負數則停用此機制，維持永久運行。

需要提早手動停止時（不管是不是背景執行），優先用 `SIGTERM`（`kill -TERM`，如上），
會走同一套優雅關閉流程，妥善收掉 Ollama 子行程，不會留下孤兒 process；只有在
`SIGTERM` 沒反應時才用 `kill -9`／`pkill -9` 強制清除。

```bash
# 方式 A：把 edge.conf 內容改好後直接跑（長期在同一台機器上管理最方便）
# 從 repo 的 edge-worker/ 資料夾把 bootstrap.py、edge.conf 一起複製到工作目錄
# （兩個檔案要放在同一層，不用管理它是不是還在 edge-worker/ 底下），
# 編輯 edge.conf 填入：
#   ORACLE_URL、NODE_ID、NODE_API_KEY（必須跟 Step 2 登記的一致）、MODEL_NAME、
#   IDLE_STOP_SEC（選填，預設 1800，設 0 或負數停用自動停止）
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
os.environ["IDLE_STOP_SEC"] = "1800"        # 選填，預設就是 1800；設 "0" 可停用自動停止

!pip install requests -q
# 在 Kaggle 上直接 !python bootstrap.py 前景執行的話，這個 cell 會因為
# worker_loop 是無限迴圈而永遠轉圈、卡住 kernel；正式跑建議改用上面
# 「Kaggle／Colab Notebook 已知雷」第 3 點的 subprocess.Popen 背景執行法
!python bootstrap.py
```

```bash
# 方式 C：CLI 參數臨時切換模型／節點身分（測試不同模型時最方便，不用改檔案）
python bootstrap.py --model qwen3:8b
python bootstrap.py --model deepseek-coder-v2:16b --node-id colab-1
```

## Step 5.5：（選用）g4f 虛擬節點——不需要 GPU 的額外運算節點

`g4f_worker.py`（在 repo **根目錄**，不在 `edge-worker/` 底下——因為它通常直接跟
`server.py` 部署在同一台 Oracle 主機上，用的是根目錄 `requirements.txt` 的環境，
不是邊緣節點那份）是另一種「節點」：不呼叫本地 Ollama，而是透過 `g4f`（gpt4free）
套件轉打免費的第三方網頁模型端點，適合拿來當備援或測試用，**不需要顯卡、不需要
另外的機器**。

```bash
cd /home/opc/finflow-queue   # 假設你把 g4f_worker.py 放在跟 server.py 同一個資料夾
# requirements.txt 這裡指的是根目錄那份（已含 g4f、requests），
# 如果你是接著 Step 1 建好的同一個 venv 繼續用，通常已經裝過了；
# 是新開的 venv 才需要重新 pip install -r requirements.txt
source venv/bin/activate

export G4F_NODE_API_KEY="<金鑰D，記得先照 Step 2 的方式登記進 NODE_API_KEYS_JSON>"
python g4f_worker.py
```

**已知限制**：`g4f` 套件呼叫的是免費第三方端點，穩定性、速度、可用模型都不受你控制，
逾時（預設 60 秒）或端點掛掉都是正常會發生的狀況，程式已經處理成「失敗就回報 error
給佇列，讓佇列走正常的重試/DLQ 流程」，不需要額外介入。長期穩定用途還是建議以
Ollama 邊緣節點（Step 5）為主，這個當備援。

## Step 6：驗證

**分層檢查**（愈底層先過，愈容易定位問題出在哪一層）：

```bash
curl http://127.0.0.1:8000/healthz       # finflow-queue 本身，不經 Caddy
curl http://127.0.0.1:8001/healthz       # bot-gateway 本身，不經 Caddy（若有部署）
curl -k https://127.0.0.1/healthz        # 經過 Caddy，本機
curl -k https://<Oracle公開IP>/healthz    # 經過 Caddy，走公網
```
四層都要回 `{"status":"ok",...}`；如果只有走公網那個失敗，通常是 OCI Security List
沒開 443；如果連 127.0.0.1 直連都失敗，問題在 `finflow-queue`／`bot-gateway`
服務本身沒起來，回頭查 `systemctl status`。

**bot-gateway 的 webhook 路由**（若有部署）——這幾個預期會是 401/404，不是 502；
如果是 502，代表 Caddy 轉發本身有問題（`bot-gateway` 沒有在監聽）：
```bash
curl -k https://127.0.0.1/telegram/webhook    # 預期 401（沒帶 secret token，正常）
curl -k https://127.0.0.1/line/webhook        # 預期 401（沒帶簽章，正常）
```

**實際任務測試**：

```bash
curl -k -X POST https://<Oracle公開IP>/v1/chat/completions \
  -H "x-api-key: <CLIENT_API_KEY>" -H "Content-Type: application/json" \
  -d '{"model":"test","messages":[{"role":"user","content":"請說一句話確認你收到了"}]}'
```

---

## Step 7：SIT（系統整合測試）

建議至少跑過這些情境再視為部署完成，涵蓋的都是「單元都沒問題，但整合起來才會暴露」
的問題類型：

- [ ] 重啟 Oracle VM（`sudo reboot`），確認 `finflow-queue`／`bot-gateway`／`caddy`
      都是 `enabled`，開機後自動起來、不需要手動介入
- [ ] Kaggle/Colab Notebook 斷線重連，重新註冊後 `GET /nodes` 能正常看到節點復活
      （`alive:true`），且沒有殘留多個重複的 `subprocess.Popen` process（見 Step 5
      已知雷第 5 點）
- [ ] 同時開兩個以上邊緣節點，測試任務有沒有正確分派（不會全部塞給同一個節點）；
      刻意讓其中一個節點斷線，確認任務會被重新分配給還活著的節點，不會卡死
- [ ] 刻意把所有節點都停掉，送一個任務進去，確認會依 `LONG_POLL_TIMEOUT_SEC` 逾時、
      回傳明確的錯誤訊息，而不是無限等待；若有設定 Telegram，確認會收到
      「沒有任何節點在線」的告警
- [ ] 提交一組帶 `depends_on` 的任務鏈，確認依賴的任務沒完成前，後續任務不會被派工；
      刻意讓被依賴的任務失敗，確認後續任務會被正確標記為失敗（cascade-fail），
      不會卡在 `pending` 假裝沒事
- [ ] Telegram／LINE／Discord（視你部署了哪些）分別發一則訊息，確認能收到正確回覆；
      Discord 額外確認「思考中…」的 deferred 回應會在任務完成後被正確編輯成答案，
      而不是卡在思考中不動
- [ ] Discord 連續發兩個 `/ask`，確認第二個不會被第一個卡住（背景任務並行處理）
- [ ] 用錯誤的 `x-api-key` 打 `/v1/chat/completions`，確認回 401 而不是洩漏內部
      錯誤細節；用 `NODE_API_KEY` 打 `GET /nodes`（權限層級不對），同樣要被拒絕
- [ ] 長對話（10 輪以上）測試 session 自動壓縮有沒有正常觸發，沒有把 context 撐爆
- [ ] （若有部署 `g4f_worker.py`）確認它在 Ollama 邊緣節點都離線時，能正常接手任務
      當備援，且逾時/失敗時任務會走正常的重試/DLQ 流程，不會讓整條佇列卡住

---

## 變更紀錄摘要

### v18（本次）

原本「Step 4：啟用 HTTPS」只有短短幾行、直接叫你跑 `setup-https.sh`，對第一次操作
OCI 的人來說太跳躍。這輪整段重寫成手把手教學，並把 Cloudflare 接法正式寫進文件。

| 檔案 | 變更 |
|------|------|
| `DEPLOY.md`（本檔） | Step 4 新增「4-1 用 Cloud Shell 連進 VM」到「4-6 驗證」六個小節，每條指令都附註解；`setup-https.sh` 內部六個步驟拆成表格逐條解釋在做什麼、為什麼要這樣做；OCI 主控台開放 443 的操作路徑寫成逐點擊步驟 |
| `DEPLOY.md`（本檔） | 新增「Step 4-D：改用 Cloudflare 代管憑證」，說明兩段式加密原理、DNS 記錄設定、為什麼選「完整」而非「自動 SSL/TLS」、`Caddyfile` 加網域名稱的做法，以及 `skip_install_trust`／`caddy-override.conf` 為何在改用 Cloudflare 後仍要保留 |
| `DEPLOY.md`（本檔） | 新增「Step 4 疑難排解」，整理這幾輪對話裡實際踩過的四個坑（無 SNI 找不到憑證、Caddyfile 語法錯誤、環境變數替換成空字串、203/EXEC）跟對應排查指令 |

### v17（本次）

延續 v16 留下的待確認事項，加上你自己實際部署時記得撞過的另一次 SELinux 坑，這輪
一併處理：

| 檔案 | 變更 |
|------|------|
| `DEPLOY.md`（本檔） | Step 1 的 `pip install fastapi uvicorn pydantic requests` 改為 `pip install -r requirements.txt`，並在旁邊列出根目錄 `requirements.txt` 的四個套件個別用途；Step 3 補上一句預告，說明 Step 4.5 會重複同一套「`cp` 進 `/etc/systemd/system/`」流程；Step 4.5「設定為常駐服務」的 SELinux relabel 說明從「見文件開頭」改成就地寫出完整原因（`user_home_t` 標籤 vs `init_t` domain 不允許執行、跟 Step 3 那次「讀設定檔」被擋是不同機制）；「建立 Discord Bot」小節修正「一次性動作」這個跟 `register_discord_commands.py` 實際行為（支援重複執行覆蓋、`--list`、`--delete`）不一致的表述，並補上這兩個子指令完全沒被文件提過的用法 |

**v16 遺留的待確認事項已確認並處理**：Step 1 確定改用 `pip install -r requirements.txt`，
不是刻意設計的差異，是舊版遺留寫法，見上表。

### v16（本次）

這輪起因是把這份文件拿去跟一份獨立整理的「Step 1-13 部署規劃」逐項核對，確認涵蓋度、
補上真正缺漏的部分，並記錄兩個尚待確認的疑點（不是本輪直接修正，等使用者確認後再處理）。

| 檔案 | 變更 |
|------|------|
| `DEPLOY.md`（本檔） | 新增「Step 0：清空重建」（既有安裝要重新部署時的判斷與操作，含 SELinux fcontext 規則綁路徑不綁檔案的說明）；Step 1 補上具體 `git clone` 教學（取代原本模糊的「上傳至此資料夾」）；Step 4.5「設定為常駐服務」補上 `bot-gateway/venv` 的 SELinux relabel 就地提醒（原本只在檔頭，容易被漏做）；Step 6 補強分層驗證（8000/8001 直連、本機/公網 HTTPS、bot-gateway webhook 路由 401 檢查）；新增「Step 7：SIT」系統整合測試檢查清單（11 項情境，涵蓋重啟自復原、多節點分派、依賴鏈失敗處理、三平台 webhook、金鑰權限層級等） |

**待確認事項（本輪未動，先記錄；已在 v17 確認並處理，見上方）**：
1. Step 1 目前直接 `pip install fastapi uvicorn pydantic requests`，沒有透過根目錄
   `requirements.txt` 安裝，跟 `bot-gateway/` 那邊會用 `pip install -r requirements.txt`
   的做法不一致。是否要統一改成用 `requirements.txt`，待確認。

### v15（本次）

這輪起因是把 Kaggle 節點實際接上 Oracle 之後，`GET /nodes` 顯示節點心跳正常，但送出的
任務永遠卡在 `pending`、最終 timeout。一路排查後發現是 `server.py` 一個路由宣告順序的
bug，跟金鑰、網路、timeout 數值都無關（雖然後兩者這輪也一併優化了）：

| 內容 | 說明 |
|------|------|
| `server.py` 路由順序 bug | `GET /jobs/{job_id}`（萬用路徑）宣告在 `GET /jobs/next`（節點輪詢專用）之前，導致 `/jobs/next` 被前者攔截、誤判 `job_id="next"`，套用錯誤的 `verify_client_key` 驗證，節點端不管帶什麼 `NODE_API_KEY` 都會 401。心跳走的是另一支獨立端點，不受影響，所以現象是「節點活著、但永遠領不到任務」，很容易被誤判成別的問題方向 |
| 逾時設定與冷啟動 | 找 bug 過程中一併確認：`LONG_POLL_TIMEOUT_SEC`（Oracle 端同步等待上限）需要 ≥ 節點端 Ollama 推論的 `timeout`，否則模型冷啟動（首次載入 VRAM）疊加推論時間很容易超過而誤判逾時 |
| 節點端新增「模型暖機」 | `bootstrap.py` 在向 Oracle 報到後、進入主輪詢迴圈前，先送一次假推論請求把模型載進 VRAM，避免第一個真正任務才在冷啟動 |
| 節點端新增「閒置自動停止」 | 新增 `IDLE_STOP_SEC` 設定（預設 1800 秒），連續閒置超過此門檻自動結束 process（含關閉 Ollama 子行程），避免忘記手動關閉而持續佔用 Kaggle/Colab GPU 配額；同時掛上 `SIGINT`/`SIGTERM` handler，手動停止也走同一套優雅關閉流程 |
| Kaggle 背景執行踩過的新雷 | 同一個 `subprocess.Popen` 啟動 cell 若重複執行會疊加多個 process，用同一個 `NODE_ID` 互搶心跳/任務，行為變得難預測；需要定期用 `ps aux` 檢查、`kill -TERM` 清理 |

| 檔案 | 變更 |
|------|------|
| `server.py` | `GET /jobs/next` 移到 `GET /jobs/{job_id}` 之前宣告，修正路由被萬用路徑攔截的 bug；`LONG_POLL_TIMEOUT_SEC` 建議調整為與節點端推論 timeout 相當或更長 |
| `edge-worker/bootstrap.py` | 新增啟動時模型暖機邏輯；新增 `IDLE_STOP_SEC` 閒置自動停止機制與對應的 `shutdown()` 清理函式；新增 `SIGINT`/`SIGTERM` handler |
| `edge-worker/edge.conf` | 新增 `IDLE_STOP_SEC` 設定項與說明註解 |
| `DEPLOY.md`（本檔） | 升級至 v15；新增「Step 3.5：已知過的重大 bug」說明路由順序問題與自我檢查方式；Step 5 新增模型暖機說明、閒置自動停止與手動停止（含背景多重 process 排查）的操作指引 |

### v14（本次）

這輪是 Step 5（邊緣節點端）在 Kaggle Notebook 上實際跑過一遍才發現的環境限制，
都跟本專案程式碼本身無關，是 Kaggle 平台的限制，記錄進文件避免下次重踩：

| 內容 | 說明 |
|------|------|
| `ollama` 安裝腳本缺 `zstd` | Kaggle 底層 Ubuntu 沒預裝 `zstd`，官方安裝腳本會直接報錯退出；先 `apt-get install zstd` 再重跑即可 |
| 安裝訊息的兩則 `WARNING` 是正常現象 | `systemd is not running`、`Unable to detect NVIDIA/AMD GPU` 這兩則警告在 Notebook 容器裡本來就會出現，不代表安裝失敗或真的沒有 GPU |
| GPU 是否真的被吃到，要用 `ollama ps` 確認 | 光看 `nvidia-smi` 只能確認 Notebook 有配置到 GPU，不能確認 ollama 實際推論時有沒有用到；`ollama ps` 的 `PROCESSOR` 欄位才是準的 |
| `!command &`／`!nohup ... &` 在 Kaggle 上直接報 `OSError: Background processes not supported` | Kaggle kernel 明確擋掉 shell magic 的背景執行語法；`bootstrap.py` 是無限迴圈，前景執行會卡住 kernel。改用 Python 原生 `subprocess.Popen` 背景啟動，不受此限制 |
| `GET /nodes` 驗證要帶 `CLIENT_API_KEY`，不是 `NODE_API_KEY` | 兩者是不同層級的金鑰，帶節點自己的 `NODE_API_KEY` 會被拒絕，容易搞混 |

| 檔案 | 變更 |
|------|------|
| `DEPLOY.md`（本檔） | Step 5 開頭新增「Kaggle／Colab Notebook 已知雷」小節，涵蓋上述五點；方式 B 的程式碼區塊補上指向該小節的提醒註解，避免直接前景執行卡住 kernel |

### v13（本次）

| 檔案 | 變更 |
|------|------|
| 評估：`caddy.service` 併入 `finflow-queue.service` | 決定不合併，理由詳見本文件最上方版本說明（權限模型衝突、systemd 單一主行程限制、套件更新會覆蓋合併後的 unit file）。目前架構已經是「全系統一份 `finflow-queue.env`，三個服務共用」，符合「減少設定散落」的原始訴求，不需要靠合併服務達成 |
| `caddy-override.conf`（新增檔案） | 把原本埋在 `setup-https.sh` heredoc 裡的 caddy systemd drop-in override 內容抽成獨立檔案，可被 git 追蹤、單獨 review diff |
| `setup-https.sh` | 步驟 4 改成檢查並 `cp caddy-override.conf`，不再用 heredoc 內嵌內容 |
| `DEPLOY.md`（本檔） | 版本號改為 v13；新增上述評估說明；Step 4 的流程描述同步更新 |

### v12（本次）

`bot-gateway` 的實際部署路徑這輪定案了：**巢狀在 `/home/opc/finflow-queue/bot-gateway`**，
取代 v11 當時採用的 `/home/opc/bot-gateway`（跟 `finflow-queue` 平行擺放）。這個決定是
在反覆搬動資料夾、實際排查兩個部署地雷之後定下來的，順便把過程中發現的兩個「非本專案
程式碼問題、但很容易忽略」的坑記錄下來：

- **venv 不能直接搬家**：`venv/bin/uvicorn` 這類進入點腳本的 shebang 會在建立當下寫死
  絕對路徑指向自己的 Python 直譯器；資料夾搬動後 shebang 沒有跟著變，會導致
  `systemctl start` 出現 `203/EXEC`。這跟 SELinux 無關，是 venv 本身的限制，唯一解法是
  搬完資料夾後 `rm -rf venv` 重建，不能只搬資料夾了事。
- **SELinux relabel 是綁在路徑上的**：`bin_t`/`lib_t` 的 `semanage fcontext` 規則認的是
  絕對路徑字串，資料夾換位置後要對新路徑重新下一次，不會自動沿用舊路徑的規則。

| 檔案 | 變更 |
|------|------|
| `bot-gateway/bot-gateway.service` | `WorkingDirectory`／`GATEWAY_DB_PATH`／`ExecStart` 三處路徑從 `/home/opc/bot-gateway` 改為 `/home/opc/finflow-queue/bot-gateway` |
| `DEPLOY.md`（本檔） | Step 4.5 的 `mkdir`／`cd` 指令、內嵌的 `bot-gateway.service` 範例、檔頭 intro 的 SELinux relabel 範例指令，三處路徑同步更新；新增本次 changelog，並附上 venv 搬家、SELinux relabel 這兩個實際踩過的地雷說明，方便日後再調整路徑時提醒自己 |

（下面 v11 的紀錄裡「搬到 `/home/opc/bot-gateway` 是對的」這段描述，是**當時那個決定**的
正確性判斷，不代表現在的最終路徑——現在的最終路徑以本次 v12 為準。歷史紀錄予以保留，
不回頭修改，避免搞混「這是哪個時間點的狀態」。）

### v11（本次）

你把 `/home/opc/finflow-queue/bot-gateway` 搬到 `/home/opc/bot-gateway` 這個操作本身是對的、
不會有其他路徑問題——`bot-gateway.service` 裡的 `WorkingDirectory`／`GATEWAY_DB_PATH`／
`ExecStart` 本來就是寫 `/home/opc/bot-gateway`（跟 repo 裡 `bot-gateway/` 資料夾同名，容易
混淆，但兩者是不同層級的東西：一個是 repo 裡收納原始碼的資料夾，一個是實機上的部署路徑），
搬完之後兩邊就對上了。順便做了一次全檔案覆核，發現一個文件跟實際檔案結構不一致的問題：

| 檔案 | 問題 | 修正 |
|------|------|------|
| `DEPLOY.md`（本檔） | Step 5／5.5 說 `g4f_worker.py` 收在 `edge-worker/` 資料夾、要用 `edge-worker/requirements.txt`（並宣稱裡面含 `g4f`）；但實際上 `g4f_worker.py` 是在**根目錄**，`g4f` 套件也是加在**根目錄的 `requirements.txt`**，`edge-worker/requirements.txt` 只有 `requests`。照原本的文件操作會裝錯 `requirements.txt`、也會找不到檔案 | Step 5 的資料夾清單移除 `g4f_worker.py`；Step 5.5 改成引用根目錄的 `g4f_worker.py`、根目錄 `requirements.txt`，並補充說明「這支程式通常跟 `server.py` 共用同一個 venv」的設計理由；檔頭 intro 的敘述同步修正 |

（`bootstrap.py`、`edge.conf`、`edge-worker/requirements.txt` 的路徑說明本來就是對的，沒有動；
`bot-gateway/` 資料夾本身、`bot-gateway.service` 的路徑設定這輪檢查下來也都正確，不需要修改。）

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
