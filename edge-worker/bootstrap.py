"""
FinFlow 邊緣節點啟動腳本 v6

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
  # 方式 A：修改 edge.conf 後直接跑（長期管理用）
  python bootstrap.py

  # 方式 B：CLI 臨時指定模型（測試不同模型用）
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

# ─── 1. 讀取 edge.conf（若存在）───────────────────────────────────────────

# repo 內這兩個檔案現在放在 edge-worker/ 資料夾裡；但實際部署時常常是把
# bootstrap.py、edge.conf 單獨複製到 Kaggle/Colab/Lightning 的工作目錄，
# 不見得會保留 edge-worker/ 這層結構，所以預設路徑改成「跟 bootstrap.py
# 同一個資料夾」而不是「目前工作目錄」，不管從哪裡呼叫、資料夾結構有沒有
# 保留，都找得到同目錄下的 edge.conf
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONF_PATH = os.path.join(_SCRIPT_DIR, "edge.conf")

def load_conf(path=_DEFAULT_CONF_PATH) -> dict:
    """解析 key=value 格式的設定檔，忽略空行與 # 開頭的註解"""
    conf = {}
    if not os.path.exists(path):
        return conf
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                conf[key.strip()] = val.strip()
    return conf

CONF = load_conf()

# ─── 2. CLI 參數 ────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="FinFlow 邊緣節點啟動腳本")
parser.add_argument("--model",      help="覆蓋 edge.conf 的 MODEL_NAME（例：qwen3:8b）")
parser.add_argument("--node-id",    help="覆蓋 edge.conf 的 NODE_ID")
parser.add_argument("--oracle-url", help="覆蓋 edge.conf 的 ORACLE_URL")
ARGS = parser.parse_args()

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
    "NODE_ID":       resolve(ARGS.node_id,    "NODE_ID",       "NODE_ID",       f"node-{uuid.uuid4().hex[:8]}"),
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

# ─── 5. VRAM 偵測（失敗回傳 None，維持伺服器端的寬鬆放行邏輯）────────────

def detect_vram():
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return round(sum(float(x) / 1024 for x in res.stdout.strip().splitlines() if x.strip()), 1)
    except Exception:
        return None

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

def register_node(vram_gb):
    try:
        requests.post(
            f"{CONFIG['ORACLE_URL']}/nodes/heartbeat",
            headers=HEADERS,
            json={
                "node_id":       NODE_ID,
                "platform":      PLATFORM,
                "current_model": CONFIG["MODEL_NAME"],
                "vram_gb":       vram_gb,
            },
            timeout=10,
            verify=CONFIG["VERIFY_TLS"],
        )
        log.info(f"節點已向 Oracle 報到：{NODE_ID}｜平台：{PLATFORM}｜模型：{CONFIG['MODEL_NAME']}｜VRAM：{vram_gb}GB")
    except Exception as e:
        log.warning(f"心跳發送失敗（主迴圈會繼續重試）：{e}")

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

def worker_loop(vram_gb):
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
                register_node(vram_gb)

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


def supervised_loop(vram_gb):
    """外層監督：worker_loop 若崩潰，自動重啟，不讓整個 Session 停擺"""
    while True:
        try:
            worker_loop(vram_gb)
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

    ensure_ollama()
    start_ollama_and_wait()
    ensure_model(CONFIG["MODEL_NAME"])

    vram_gb = detect_vram()
    register_node(vram_gb)

    log.info("模型暖機中...")
    try:
        run_inference([{"role": "user", "content": "hi"}])
        log.info("模型暖機完成")
    except Exception as e:
        log.warning(f"暖機失敗（不影響主流程）：{e}")

    supervised_loop(vram_gb)


if __name__ == "__main__":
    main()