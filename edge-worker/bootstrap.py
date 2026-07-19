"""
FinFlow 邊緣節點啟動腳本 v8

新增功能（相對 v7）：
  - edge.conf 支援用 [node_id] 分區塊，一份檔案同時裝下多個節點的設定
    （NODE_ID／NODE_API_KEY／MODEL_NAME 等），不用每加一個節點就多開一個
    檔案。區塊開始前（也就是檔案最上面）的 KEY=VALUE 視為所有節點共用的
    預設值（例如大家都指向同一個 ORACLE_URL 通常只需要寫一次），個別
    區塊裡的同名 KEY 會覆蓋預設值。
  - 完全向後相容：如果 edge.conf 裡完全沒有 [區塊]（舊格式、單一節點
    平鋪 KEY=VALUE），行為跟 v7 一模一樣，不需要修改既有的單節點部署
  - 手動執行時，如果 edge.conf 裡有多個 [區塊]，必須用 --node-id 或
    環境變數 NODE_ID 明確指定要跑哪一個，或是在區塊開始前放一行
    NODE_ID=xxx 當作「沒指定時的預設節點」

新增功能（相對 v6）：
  - detect_vram() 改名/擴充為 detect_gpu_info()，除了 VRAM 總量，也回報
    GPU 型號名稱與張數（gpu_name／gpu_count），讓 /list-nodes 能直接顯示
    「這次分配到幾張卡」，不用自己拿 VRAM 數字回推
  - 每次心跳一併帶上這次 process 的啟動時間（started_at），給
    /list-nodes 顯示「已運作多久」用
  - 這兩項都是配合 Kaggle 的兩階段啟動流程新增的：GPU 是隨機分配的，
    先開機回報硬體規格、確認滿意再真正下載模型，見 DEPLOY.md「Step 5.7」

新增功能（相對 v5）：
  - ensure_ollama() 安裝前先確保 zstd 存在（Kaggle 等平台的基礎映像檔沒有
    預裝，ollama 官方安裝腳本會直接失敗；Lightning 等平台如果已經有 zstd，
    這步會直接跳過），從此不需要在啟動前手動另外執行 apt-get install zstd
  - 主輪詢迴圈新增遠端停止信號檢查：GET /jobs/next 回應裡的 stop_requested
    欄位為 true 時，呼叫既有的 shutdown() 自行結束，搭配 server.py 新增的
    POST /nodes/{id}/stop 與 bot-gateway 的節點群控指令使用，見 DEPLOY.md

新增功能（相對 v4）：
  - 單次推論逾時從寫死的 180 秒，改成可設定的 INFERENCE_TIMEOUT_SEC（預設 300 秒），
    跟 MODEL_NAME 一樣可透過 edge.conf／環境變數／CLI 覆蓋，換更大更慢的模型時
    不需要改程式碼

新增功能（相對 v3）：
  - 支援 edge.conf 設定檔，模型名稱與連線資訊集中管理
  - 支援 CLI 參數 --model / --node-id / --oracle-url，臨時切換優先於設定檔
  - 設定優先順序：CLI 參數 > 環境變數 > edge.conf > 預設值

使用方式：
  # 方式 A：edge.conf 只有一個節點（沒有 [區塊]），修改後直接跑
  python bootstrap.py

  # 方式 B：edge.conf 裝了多個節點的 [區塊]，用 --node-id 選其中一個
  python bootstrap.py --node-id kaggle-1
  python bootstrap.py --node-id lightning-1 --model qwen3:8b   # 同時臨時換模型

  # 方式 C：CLI 臨時指定模型（測試不同模型用，node_id 沿用 edge.conf 的預設值）
  python bootstrap.py --model qwen3:8b
  python bootstrap.py --model deepseek-coder-v2:16b --node-id colab-1
"""

import os
import sys
import time
import uuid
import argparse
import subprocess
import logging
import requests

# 這支程式「這次執行」的啟動時間，跟著每次心跳一起送出（見 register_node()），
# 給 /list-nodes 顯示「運作多久了」用。故意放在最上面、盡量接近真正的 process
# 起始時刻，不是等到 main() 執行到一半才記錄。
_PROCESS_START_TIME = time.time()

# ─── 1. 讀取 edge.conf（若存在），支援 [node_id] 分區塊 ───────────────────

# repo 內這兩個檔案現在放在 edge-worker/ 資料夾裡；但實際部署時常常是把
# bootstrap.py、edge.conf 單獨複製到 Kaggle/Colab/Lightning 的工作目錄，
# 不見得會保留 edge-worker/ 這層結構，所以預設路徑改成「跟 bootstrap.py
# 同一個資料夾」而不是「目前工作目錄」，不管從哪裡呼叫、資料夾結構有沒有
# 保留，都找得到同目錄下的 edge.conf
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONF_PATH = os.path.join(_SCRIPT_DIR, "edge.conf")


def load_conf_sections(path=_DEFAULT_CONF_PATH):
    """解析 edge.conf，回傳 (top_level, sections)：
      - top_level：第一個 [區塊] 出現之前的 KEY=VALUE，當作所有節點共用
        的預設值
      - sections：{node_id: {key: value, ...}}，每個區塊已經繼承了「這個
        區塊開始當下」累積到的 top_level 值，區塊內同名 KEY 會覆蓋掉繼承
        來的預設值

    這支函式在 bot-gateway/node_controllers/base.py 裡有一份幾乎一模一樣
    的拷貝（parse_conf_sections()）——那邊是 Oracle 端讀同一份 edge.conf
    的邏輯，這邊是節點自己讀的邏輯，兩邊執行環境完全獨立（不同的 repo
    checkout、不同的機器），沒辦法共用同一份程式碼，格式規則故意保持
    完全一致，改動時記得兩邊一起改。"""
    top_level, sections = {}, {}
    if not os.path.exists(path):
        return top_level, sections
    current = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1].strip()
                sections[current] = dict(top_level)  # 繼承目前為止的預設值
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if not key:
                continue
            if current is None:
                top_level[key] = val
            else:
                sections[current][key] = val
    return top_level, sections


_TOP_LEVEL, _SECTIONS = load_conf_sections()

# ─── 2. CLI 參數 ────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="FinFlow 邊緣節點啟動腳本")
parser.add_argument("--model",      help="覆蓋 edge.conf 的 MODEL_NAME（例：qwen3:8b）")
parser.add_argument("--node-id",    help="edge.conf 裝了多個節點時，指定要用哪一個 [區塊]；"
                                          "也可以用來覆蓋單節點 edge.conf 裡的 NODE_ID")
parser.add_argument("--oracle-url", help="覆蓋 edge.conf 的 ORACLE_URL")
ARGS = parser.parse_args()

# NODE_ID 要先決定，才知道該用 edge.conf 裡哪個區塊的設定——決定順序是
# CLI > 環境變數 > edge.conf 最上面（區塊之前）的預設 NODE_ID。如果
# edge.conf 完全沒有 [區塊]（舊格式單一節點），_SECTIONS 是空的，下面
# CONF 會直接退回整份 _TOP_LEVEL，行為跟 v7 完全一樣。
_NODE_ID = ARGS.node_id or os.environ.get("NODE_ID") or _TOP_LEVEL.get("NODE_ID")
if _SECTIONS:
    if not _NODE_ID:
        raise SystemExit(
            f"edge.conf 裡有多個節點區塊（{', '.join(_SECTIONS.keys())}），"
            f"必須用 --node-id 或環境變數 NODE_ID 指定要啟動哪一個，"
            f"或是在 edge.conf 最上面（第一個 [區塊] 之前）加一行 NODE_ID=xxx 當預設值"
        )
    if _NODE_ID not in _SECTIONS:
        raise SystemExit(
            f"edge.conf 裡找不到 [{_NODE_ID}] 這個區塊，"
            f"目前有的區塊：{', '.join(_SECTIONS.keys())}"
        )

CONF = _SECTIONS.get(_NODE_ID, _TOP_LEVEL) if _SECTIONS else _TOP_LEVEL

# ─── 3. 設定優先順序：CLI > 環境變數 > edge.conf > 預設值 ─────────────────

def resolve(cli_val, env_key, conf_key, default):
    if cli_val:
        return cli_val
    if os.environ.get(env_key):
        return os.environ[env_key]
    if conf_key in CONF:
        return CONF[conf_key]
    return default

CONFIG = {
    "ORACLE_URL":    resolve(ARGS.oracle_url, "ORACLE_URL",    "ORACLE_URL",    "https://your-oracle-public-ip-or-domain"),
    # NODE_ID 不透過 resolve()：上面已經用同樣的 CLI > 環境變數 > edge.conf
    # 優先序算出 _NODE_ID 了（還額外處理了多節點區塊比對），這裡直接沿用，
    # 避免重算一次卻少了區塊比對的邏輯，兩處各算各的容易產生不一致
    "NODE_ID":       _NODE_ID or f"node-{uuid.uuid4().hex[:8]}",
    "NODE_API_KEY":  resolve(None,            "NODE_API_KEY",  "NODE_API_KEY",  "change-me"),
    "MODEL_NAME":    resolve(ARGS.model,      "MODEL_NAME",    "MODEL_NAME",    "qwen2.5-coder:14b"),
    "OLLAMA_URL":    resolve(None,            "OLLAMA_URL",    "OLLAMA_URL",    "http://localhost:11434"),
    "VERIFY_TLS":    resolve(None,            "VERIFY_TLS",    "VERIFY_TLS",    "false").lower() == "true",
    # 單次推論請求的逾時秒數。套用在「每一個」推論呼叫上，不只是開機暖機那次——
    # 換成更大、更慢的模型（例如跨 GPU 的 MoE 架構）時，連同真實任務的長 prompt／
    # 長輸出一起考慮進去再調整，不要只看暖機時間。預設 300 秒（5 分鐘），遠低於
    # Oracle 端 JOB_WAIT_TIMEOUT_SEC 的 900 秒，兩者不會互相衝突。
    "INFERENCE_TIMEOUT_SEC": int(resolve(None, "INFERENCE_TIMEOUT_SEC", "INFERENCE_TIMEOUT_SEC", "300")),
    # 閒置超過這麼多秒沒有領到任何任務，就自動結束 process（釋放 Kaggle/Colab GPU 配額）。
    # 設為 0 或負數 = 停用自動停止，永遠跑下去（維持舊行為）。
    "IDLE_STOP_SEC": int(resolve(None,        "IDLE_STOP_SEC", "IDLE_STOP_SEC", "1800")),
}

NODE_ID = CONFIG["NODE_ID"]
HEADERS = {"x-api-key": CONFIG["NODE_API_KEY"], "Content-Type": "application/json"}

if not CONFIG["VERIFY_TLS"]:
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

# ─── 4. 平台偵測與日誌設定 ──────────────────────────────────────────────────

def detect_platform() -> str:
    if os.path.exists("/teamspace"): return "lightning"
    if os.path.exists("/kaggle"):    return "kaggle"
    if os.path.exists("/content"):   return "colab"
    return "unknown"

PLATFORM = detect_platform()

_PERSIST_DIR_MAP = {
    "lightning": "/teamspace/studios/this_studio",
    "kaggle":    "/kaggle/working",
    "colab":     "/content",
}
PERSIST_DIR = _PERSIST_DIR_MAP.get(PLATFORM, "/tmp")
os.makedirs(PERSIST_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(PERSIST_DIR, "worker.log")),
    ],
)
log = logging.getLogger("finflow-worker")

# ─── 5. GPU 資訊偵測（失敗回傳 None/0，維持伺服器端的寬鬆放行邏輯）───────

def detect_gpu_info():
    """回傳 (vram_gb, gpu_name, gpu_count)，偵測失敗回傳 (None, None, 0)。
    vram_gb 是所有 GPU 加總（沿用 v5 以前的行為，node_capability_satisfies()
    的 min_vram_gb 比對邏輯不用跟著改）；gpu_name／gpu_count 是這次新增的，
    給 /list-nodes 顯示實際分配到的硬體用，尤其是 Kaggle 這種數量隨機分配的
    平台，開機後可以馬上看到這次到底拿到幾張卡。"""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None, None, 0
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        names, total_mb = [], 0.0
        for line in lines:
            name, mem = line.rsplit(",", 1)
            names.append(name.strip())
            total_mb += float(mem.strip())
        # 保留原本出現順序去重：同型號 GPU 只顯示一次名稱，不同型號混用時
        # 用 "+" 串起來，讓使用者一眼看出是不是混合配置
        unique_names = list(dict.fromkeys(names))
        gpu_name = " + ".join(unique_names)
        return round(total_mb / 1024, 1), gpu_name, len(lines)
    except Exception:
        return None, None, 0

# ─── 6. Ollama 安裝、模型下載、健康檢查 ────────────────────────────────────

def ensure_ollama():
    import shutil
    if shutil.which("ollama") is None:
        log.info("偵測不到 Ollama，開始安裝...")
        # 有些平台的基礎映像檔沒有預裝 zstd（實測 Kaggle 會直接讓 ollama 官方
        # 安裝腳本失敗：ERROR: This version requires zstd for extraction），
        # 這裡先確保它存在。用 shell=True 執行且不 check=True：這行只是儘量
        # 幫忙裝，裝不成功（例如平台沒有 apt-get、或本來就有 zstd 不需要裝）
        # 都不應該讓整個 ensure_ollama() 中斷，交給下一行真正的安裝指令自己
        # 決定成敗
        if shutil.which("zstd") is None:
            log.info("偵測不到 zstd，嘗試安裝（Kaggle 等平台的 ollama 安裝腳本需要它）...")
            subprocess.run("apt-get update -qq && apt-get install -y zstd -qq", shell=True)
        subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True, check=True)
    else:
        log.info("Ollama 已安裝，略過安裝步驟")


def ensure_model(model_name: str):
    result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if model_name.split(":")[0] in result.stdout:
        log.info(f"模型 {model_name} 已存在，略過下載")
        return
    log.info(f"下載模型 {model_name}（視大小可能需要數分鐘至數十分鐘）...")
    subprocess.run(["ollama", "pull", model_name], check=True)


_OLLAMA_PROC = None

def start_ollama_and_wait(timeout_sec=60):
    global _OLLAMA_PROC
    log.info("啟動 Ollama 服務...")
    _OLLAMA_PROC = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get(f"{CONFIG['OLLAMA_URL']}/api/tags", timeout=3)
            if r.status_code == 200:
                log.info("Ollama 就緒")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Ollama 在時限內未能就緒，請檢查安裝是否成功")

# ─── 7. 節點註冊（心跳）───────────────────────────────────────────────────

def register_node(vram_gb, gpu_name=None, gpu_count=0, status="running"):
    try:
        requests.post(
            f"{CONFIG['ORACLE_URL']}/nodes/heartbeat",
            headers=HEADERS,
            json={
                "node_id":       NODE_ID,
                "platform":      PLATFORM,
                "current_model": CONFIG["MODEL_NAME"],
                "vram_gb":       vram_gb,
                "gpu_name":      gpu_name,
                "gpu_count":     gpu_count,
                "started_at":    _PROCESS_START_TIME,
                "status":        status,
            },
            timeout=10,
            verify=CONFIG["VERIFY_TLS"],
        )
        log.info(f"節點已向 Oracle 報到：{NODE_ID}｜狀態：{status}｜平台：{PLATFORM}｜模型：{CONFIG['MODEL_NAME']}｜"
                 f"GPU：{gpu_name or '未偵測到'} x{gpu_count}｜VRAM：{vram_gb}GB")
    except Exception as e:
        log.warning(f"心跳發送失敗（主迴圈會繼續重試）：{e}")


import threading

def _loading_heartbeat_loop(stop_event, vram_gb, gpu_name, gpu_count, interval_sec=25):
    """`ensure_ollama()`／`ensure_model()` 這段是同步阻塞呼叫，中間沒有任何
    機會送心跳——`ensure_model()` 拉一個 9B/14B 模型視情況要好幾分鐘，遠遠
    超過 Oracle 端 NODE_DEAD_AFTER_SEC（60 秒）的判定門檻，導致節點明明還
    活著（正在下載），`/list-nodes` 卻顯示離線，等下載完成才會忽然跳成
    運作中，順序完全不符合直覺，也讓人誤以為節點中途斷過線。這裡用背景
    執行緒每 25 秒補送一次心跳（status 設成 "loading"，不是 "running"，
    這樣 /list-nodes 才能正確顯示成「下載中」而不是誤報成「已經 ready」），
    在 main() 呼叫 register_node() 送出真正的 "running" 心跳之前，讓 Oracle
    持續知道節點還活著。用 threading.Event().wait() 而不是 time.sleep()：
    stop_event.set() 之後能立即結束，不用多等到下一次 interval_sec 才發現
    該停了。"""
    while not stop_event.wait(interval_sec):
        register_node(vram_gb, gpu_name, gpu_count, status="loading")

# ─── 8. 推論與任務回報 ──────────────────────────────────────────────────────

def run_inference(messages: list) -> str:
    resp = requests.post(
        f"{CONFIG['OLLAMA_URL']}/api/chat",
        json={"model": CONFIG["MODEL_NAME"], "messages": messages, "stream": False},
        timeout=CONFIG["INFERENCE_TIMEOUT_SEC"],
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def submit_result(job_id: str, result=None, error=None):
    requests.post(
        f"{CONFIG['ORACLE_URL']}/jobs/{job_id}/result",
        params={"node_id": NODE_ID},
        headers=HEADERS,
        json={"result": result, "error": error},
        timeout=10,
        verify=CONFIG["VERIFY_TLS"],
    )

# ─── 8.5 優雅關閉（自動閒置停止 / 手動停止共用）────────────────────────────

def shutdown(reason: str):
    log.info(f"節點準備停止：{reason}")
    if _OLLAMA_PROC is not None:
        try:
            _OLLAMA_PROC.terminate()
            _OLLAMA_PROC.wait(timeout=10)
        except Exception as e:
            log.warning(f"關閉 Ollama 行程時發生例外（忽略）：{e}")
    log.info("節點已停止，process 即將結束")
    sys.exit(0)


import signal

def _handle_signal(signum, frame):
    shutdown(f"收到系統訊號 {signum}（手動停止）")

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ─── 9. 主輪詢迴圈（含自動重啟監督）───────────────────────────────────────

def worker_loop(vram_gb, gpu_name=None, gpu_count=0):
    log.info(f"進入主輪詢迴圈（模型：{CONFIG['MODEL_NAME']}）")
    if CONFIG["IDLE_STOP_SEC"] > 0:
        log.info(f"閒置自動停止已啟用：連續 {CONFIG['IDLE_STOP_SEC']} 秒沒有任務就會自動結束節點")
    else:
        log.info("閒置自動停止已停用（IDLE_STOP_SEC <= 0），節點會持續運行")

    loop_count = 0
    last_activity_at = time.time()  # 一開始就算「活躍」，避免節點剛啟動就被判定閒置
    while True:
        try:
            if loop_count % 15 == 0:
                register_node(vram_gb, gpu_name, gpu_count)

            res = requests.get(
                f"{CONFIG['ORACLE_URL']}/jobs/next",
                params={"node_id": NODE_ID},
                headers=HEADERS,
                timeout=10,
                verify=CONFIG["VERIFY_TLS"],
            )
            res_data = res.json()

            if res_data.get("stop_requested"):
                shutdown("收到 Oracle 的遠端停止信號（Discord /stop-node 或 Lightning 閒置監控觸發）")

            if res.status_code == 200 and "job_id" in res_data:
                job_id   = res_data["job_id"]
                messages = res_data["payload"]["messages"]
                log.info(f"接到任務 {job_id}，開始推論...")
                last_activity_at = time.time()  # 領到任務＝有活動，重置閒置計時
                try:
                    result = run_inference(messages)
                    submit_result(job_id, result=result)
                    log.info(f"任務 {job_id} 完成並回傳")
                except Exception as e:
                    log.error(f"推論失敗：{e}")
                    submit_result(job_id, error=str(e))
                last_activity_at = time.time()  # 任務處理完成，再次更新，閒置計時從「完成時」重算

            elif CONFIG["IDLE_STOP_SEC"] > 0 and (time.time() - last_activity_at) > CONFIG["IDLE_STOP_SEC"]:
                idle_min = round((time.time() - last_activity_at) / 60, 1)
                shutdown(f"已閒置 {idle_min} 分鐘（超過 {CONFIG['IDLE_STOP_SEC']} 秒門檻），自動停止以節省 GPU 配額")

            loop_count += 1
            time.sleep(2)

        except Exception as e:
            log.error(f"連線異常，5 秒後重試：{e}")
            time.sleep(5)


def supervised_loop(vram_gb, gpu_name=None, gpu_count=0):
    """外層監督：worker_loop 若崩潰，自動重啟，不讓整個 Session 停擺"""
    while True:
        try:
            worker_loop(vram_gb, gpu_name, gpu_count)
        except Exception as e:
            log.error(f"主迴圈意外終止，10 秒後重啟：{e}")
            time.sleep(10)

# ─── 10. 進入點 ─────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"FinFlow 邊緣節點啟動 │ 平台：{PLATFORM} │ 節點：{NODE_ID}")
    log.info(f"模型：{CONFIG['MODEL_NAME']} │ Oracle：{CONFIG['ORACLE_URL']}")
    log.info("=" * 60)

    if CONFIG["NODE_API_KEY"] == "change-me":
        log.warning("⚠️  NODE_API_KEY 仍是預設值，Oracle 端會拒絕此節點的請求")

    # 提前到最前面偵測，讓下面的背景心跳執行緒從一開始就能帶著正確的 GPU
    # 資訊送出心跳，不用等 ensure_ollama／ensure_model 跑完才知道
    vram_gb, gpu_name, gpu_count = detect_gpu_info()

    stop_loading_heartbeat = threading.Event()
    loading_heartbeat_thread = threading.Thread(
        target=_loading_heartbeat_loop,
        args=(stop_loading_heartbeat, vram_gb, gpu_name, gpu_count),
        daemon=True,  # daemon=True：main process 结束時這條背景執行緒不會
                      # 卡著不放，不需要額外處理它的收尾
    )
    loading_heartbeat_thread.start()
    try:
        ensure_ollama()
        start_ollama_and_wait()
        ensure_model(CONFIG["MODEL_NAME"])

        # 暖機（跑一次真正的推論觸發模型載入進 VRAM）實測可能花到 100 秒
        # 以上，一樣遠超過 60 秒的離線判定門檻，所以背景心跳要蓋到這裡
        # 結束為止，不能在暖機開始前就提早停掉、送出 "running"——那樣
        # 暖機這段又會重演一次一模一樣的問題，只是空窗從「下載模型」
        # 搬到「暖機」而已。
        log.info("模型暖機中...")
        try:
            run_inference([{"role": "user", "content": "hi"}])
            log.info("模型暖機完成")
        except Exception as e:
            log.warning(f"暖機失敗（不影響主流程）：{e}")
    finally:
        # 不管上面成功或丟例外，都要停止背景心跳——如果中途失敗，讓
        # worker_loop／supervised_loop 外層的重試機制接手，不要讓一個
        # 「一直送 loading 心跳、卻再也不會變成 running」的殭屍狀態留著
        stop_loading_heartbeat.set()

    register_node(vram_gb, gpu_name, gpu_count, status="running")
    supervised_loop(vram_gb, gpu_name, gpu_count)


if __name__ == "__main__":
    main()