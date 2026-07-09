"""
FinFlow 邊緣節點啟動腳本 v4

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

def load_conf(path="edge.conf") -> dict:
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


def start_ollama_and_wait(timeout_sec=60):
    log.info("啟動 Ollama 服務...")
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        timeout=180,
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

# ─── 9. 主輪詢迴圈（含自動重啟監督）───────────────────────────────────────

def worker_loop(vram_gb):
    log.info(f"進入主輪詢迴圈（模型：{CONFIG['MODEL_NAME']}）")
    loop_count = 0
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
            if res.status_code == 200 and "job_id" in res.json():
                job_id   = res.json()["job_id"]
                messages = res.json()["payload"]["messages"]
                log.info(f"接到任務 {job_id}，開始推論...")
                try:
                    result = run_inference(messages)
                    submit_result(job_id, result=result)
                    log.info(f"任務 {job_id} 完成並回傳")
                except Exception as e:
                    log.error(f"推論失敗：{e}")
                    submit_result(job_id, error=str(e))

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

    supervised_loop(vram_gb)


if __name__ == "__main__":
    main()