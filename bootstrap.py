"""
FinFlow 邊緣節點啟動腳本 v3 —— 以使用者提供的 Gemini 修改版為基礎，修正三點：
  1. VRAM 偵測失敗回傳 None（而非 0.0），避免 CPU-only 節點被誤判為「確定沒有 GPU」
     而永遠領不到任務（接回 server.py v4 的寬鬆放行設計才有意義）
  2. Ollama 啟動後改為主動健康檢查等待就緒，而非固定 sleep(5)（不同平台冷啟動時間差異大）
  3. Kaggle / Colab 的日誌路徑改回持久路徑，而非 /tmp（/tmp 在 Session 結束後必定消失）

其餘維持原貌：扁平心跳欄位、單層 try/except 自我修復迴圈、Ollama 原生 /api/chat 呼叫方式。
"""

import os
import sys
import time
import json
import uuid
import subprocess
import logging
import requests

CONFIG = {
    "ORACLE_URL": os.environ.get("ORACLE_URL", "https://finflow.yourdomain.com"),
    "NODE_ID": os.environ.get("NODE_ID", "kaggle-1"),
    "NODE_API_KEY": os.environ.get("NODE_API_KEY", "change-me"),
    "MODEL_NAME": os.environ.get("MODEL_NAME", "qwen2.5-coder:14b"),
    "VERIFY_TLS": os.environ.get("VERIFY_TLS", "false").lower() == "true",
}

HEADERS = {"x-api-key": CONFIG["NODE_API_KEY"], "Content-Type": "application/json"}
if not CONFIG["VERIFY_TLS"]:
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


def detect_platform():
    if os.path.exists("/teamspace"):
        return "lightning"
    if os.path.exists("/kaggle"):
        return "kaggle"
    if os.path.exists("/content"):
        return "colab"
    return "unknown"


PLATFORM = detect_platform()

# 修正點 3：Kaggle/Colab 也給持久一點的路徑（Kaggle 的 /kaggle/working 在 Session
# 結束後仍會保留成 Output；Colab 的 /content 若有掛載 Drive 則持久，沒掛載則跟 /tmp
# 效果相同但至少統一邏輯，不再預設直接丟 /tmp）
_PERSIST_CANDIDATES = {
    "lightning": "/teamspace/studios/this_studio",
    "kaggle": "/kaggle/working",
    "colab": "/content",
}
PERSIST_DIR = _PERSIST_CANDIDATES.get(PLATFORM, "/tmp")
os.makedirs(PERSIST_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(os.path.join(PERSIST_DIR, "worker.log"))])
log = logging.getLogger("worker")


def detect_vram():
    """修正點 1：偵測失敗回傳 None，不是 0.0——
    0.0 是「確定的數字」，會讓 server.py 端的能力比對誤判成『這個節點明確沒有 GPU』，
    None 才能維持『未知，寬鬆放行』的設計"""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return sum(float(x) / 1024 for x in res.stdout.strip().split("\n") if x.strip())
    except Exception:
        return None


def wait_ollama_ready(timeout_sec=60):
    """修正點 2：主動健康檢查，取代固定 sleep(5)——
    Kaggle/Colab/Lightning 的冷啟動時間不一致，固定等待時間在較慢的環境會讓
    後續的 ollama pull 在伺服器還沒準備好時就執行而失敗"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get("http://localhost:11434/api/tags", timeout=3)
            if r.status_code == 200:
                log.info("Ollama 已就緒")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Ollama 在時限內未能就緒，請檢查安裝是否成功")


def start_ollama():
    log.info("檢查並啟動 Ollama 引擎...")
    if not subprocess.run("which ollama", shell=True, capture_output=True).stdout:
        subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True, check=True)
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_ollama_ready()
    log.info(f"拉取模型 {CONFIG['MODEL_NAME']}...")
    subprocess.run(["ollama", "pull", CONFIG["MODEL_NAME"]], check=True)


def worker_loop():
    vram_gb = detect_vram()
    log.info(f"進入主迴圈 (Node: {CONFIG['NODE_ID']}, 平台: {PLATFORM}, 模型: {CONFIG['MODEL_NAME']}, VRAM: {vram_gb})...")

    last_heartbeat = 0
    while True:
        try:
            if time.time() - last_heartbeat > 30:
                requests.post(f"{CONFIG['ORACLE_URL']}/nodes/heartbeat", headers=HEADERS, verify=CONFIG["VERIFY_TLS"], json={
                    "node_id": CONFIG["NODE_ID"], "platform": PLATFORM, "current_model": CONFIG["MODEL_NAME"], "vram_gb": vram_gb
                })
                last_heartbeat = time.time()

            res = requests.get(f"{CONFIG['ORACLE_URL']}/jobs/next?node_id={CONFIG['NODE_ID']}", headers=HEADERS, verify=CONFIG["VERIFY_TLS"])
            if res.status_code == 200 and "job_id" in res.json():
                job_id = res.json()["job_id"]
                messages = res.json()["payload"]["messages"]
                log.info(f"執行任務: {job_id}")

                ollama_res = requests.post("http://localhost:11434/api/chat", json={"model": CONFIG["MODEL_NAME"], "messages": messages, "stream": False})

                result_data = {}
                if ollama_res.status_code == 200:
                    result_data["result"] = ollama_res.json()["message"]["content"]
                else:
                    result_data["error"] = f"Ollama Error: {ollama_res.text}"

                requests.post(f"{CONFIG['ORACLE_URL']}/jobs/{job_id}/result?node_id={CONFIG['NODE_ID']}", headers=HEADERS, verify=CONFIG["VERIFY_TLS"], json=result_data)
                log.info(f"任務 {job_id} 完成並回傳")
            time.sleep(2)
        except Exception as e:
            log.error(f"連線異常，5秒後重試: {e}")
            time.sleep(5)


if __name__ == "__main__":
    start_ollama()
    worker_loop()
