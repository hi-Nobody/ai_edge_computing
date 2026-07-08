"""
FinFlow 邊緣節點啟動腳本 v2 —— 六階段完整版 + Per-node 金鑰 + HTTPS 支援

在 Kaggle / Colab / Lightning AI 的 Notebook Cell 中執行，或在 Lightning AI 的
持久 Studio 中以 `python bootstrap.py` 常駐執行。

相對 v1 新增：
1. VRAM 自動偵測（nvidia-smi），回報給 Oracle 做能力比對分配（對應 main.py v3 的 capability_satisfies）
2. 改用 Per-node 專屬金鑰（NODE_API_KEY），而非所有節點共用一把金鑰
3. 支援 HTTPS，並可選擇是否驗證 Caddy 自簽憑證（VERIFY_TLS）

涵蓋：環境偵測 -> 依賴安裝 -> 模型快取檢查 -> Ollama 啟動與健康檢查 ->
      節點註冊（含 VRAM 偵測）-> 主迴圈監督（含自動重啟與本機日誌持久化）

使用前設定（環境變數，或直接修改下方 CONFIG）：
    ORACLE_URL          例如 https://150.230.x.x（注意：v3 起預設應為 https）
    NODE_ID              這個節點的識別碼，必須先在 Oracle 端的 NODE_API_KEYS_JSON 登記
    NODE_API_KEY          這個節點專屬的金鑰，必須與 Oracle 端登記的一致
    MODEL_NAME            Ollama 模型標籤
    VERIFY_TLS            "true"/"false"，若 Oracle 用自簽憑證且未做憑證固定，設為 false
"""

import os
import sys
import time
import uuid
import json
import shutil
import subprocess
import logging
import requests

# ---------- 設定 ----------
CONFIG = {
    "ORACLE_URL": os.environ.get("ORACLE_URL", "https://<你的Oracle公開IP或網域>"),
    "NODE_ID": os.environ.get("NODE_ID", f"node-{uuid.uuid4().hex[:8]}"),
    "NODE_API_KEY": os.environ.get("NODE_API_KEY", "change-me-must-match-oracle-side"),
    "MODEL_NAME": os.environ.get("MODEL_NAME", "qwen2.5-coder:32b"),
    "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
    "PERSIST_DIR": os.environ.get("PERSIST_DIR", ""),
    "VERIFY_TLS": os.environ.get("VERIFY_TLS", "false").lower() == "true",
    # 預設 false 是因為零成本方案用 Caddy 自簽憑證；若改用 Cloudflare Tunnel 或
    # 自己的網域 + Let's Encrypt（見 DEPLOY.md），應改成 true 以確保連線真的安全
}

NODE_ID = CONFIG["NODE_ID"]
HEADERS = {"x-api-key": CONFIG["NODE_API_KEY"]}

if not CONFIG["VERIFY_TLS"]:
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


# ---------- 階段 1：環境偵測 ----------

def detect_platform() -> str:
    if os.path.exists("/teamspace"):
        return "lightning"
    if os.path.exists("/kaggle"):
        return "kaggle"
    if os.path.exists("/content"):
        return "colab"
    return "unknown"


def resolve_persist_dir(platform: str) -> str:
    if CONFIG["PERSIST_DIR"]:
        return CONFIG["PERSIST_DIR"]
    candidates = {
        "lightning": "/teamspace/studios/this_studio/finflow",
        "kaggle": "/kaggle/working/finflow",
        "colab": "/content/finflow",
    }
    path = candidates.get(platform, "/tmp/finflow")
    os.makedirs(path, exist_ok=True)
    return path


def detect_vram_gb():
    """best-effort 偵測，失敗回傳 None（main.py v3 對 None 採寬鬆放行，見規劃文件 4c）"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            total_mb = sum(int(x.strip()) for x in result.stdout.strip().splitlines() if x.strip())
            return round(total_mb / 1024, 1)
    except Exception:
        pass
    return None


PLATFORM = detect_platform()
PERSIST_DIR = resolve_persist_dir(PLATFORM)
VRAM_GB = detect_vram_gb()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(PERSIST_DIR, "worker.log")),
    ],
)
log = logging.getLogger("finflow-bootstrap")


# ---------- 階段 2：依賴安裝 ----------

def ensure_dependencies():
    if shutil.which("ollama") is None:
        log.info("偵測不到 Ollama，開始安裝...")
        subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True, check=True)
    else:
        log.info("Ollama 已安裝，略過安裝步驟")


# ---------- 階段 3：模型快取檢查 ----------

def ensure_model_cached():
    result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if CONFIG["MODEL_NAME"] in result.stdout:
        log.info(f"模型 {CONFIG['MODEL_NAME']} 已存在本機，略過下載")
        return
    log.info(f"模型 {CONFIG['MODEL_NAME']} 不存在，開始下載...")
    subprocess.run(["ollama", "pull", CONFIG["MODEL_NAME"]], check=True)


# ---------- 階段 4：啟動 Ollama 並等待就緒 ----------

def start_ollama_and_wait(timeout_sec=60):
    log.info("啟動 Ollama 服務...")
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get(f"{CONFIG['OLLAMA_URL']}/api/tags", timeout=3)
            if r.status_code == 200:
                log.info("Ollama 已就緒")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Ollama 在時限內未能就緒，請檢查安裝是否成功")


# ---------- 階段 5：節點註冊（含 VRAM 偵測結果）----------

def register_node():
    capability = {
        "platform": PLATFORM,
        "model": CONFIG["MODEL_NAME"],
        "node_id": NODE_ID,
        "vram_gb": VRAM_GB,
    }
    try:
        requests.post(
            f"{CONFIG['ORACLE_URL']}/nodes/heartbeat",
            json={"node_id": NODE_ID, "capability": capability},
            headers=HEADERS,
            timeout=10,
            verify=CONFIG["VERIFY_TLS"],
        )
        log.info(f"節點註冊成功：{NODE_ID}（平台：{PLATFORM}，模型：{CONFIG['MODEL_NAME']}，VRAM：{VRAM_GB}GB）")
    except Exception as e:
        log.warning(f"節點註冊失敗（將在主迴圈中持續重試）：{e}")


# ---------- 階段 6：主迴圈監督 ----------

def fetch_next_job():
    resp = requests.get(
        f"{CONFIG['ORACLE_URL']}/jobs/next",
        params={"node_id": NODE_ID},
        headers=HEADERS,
        timeout=10,
        verify=CONFIG["VERIFY_TLS"],
    )
    resp.raise_for_status()
    return resp.json().get("job")


def run_inference(messages):
    resp = requests.post(
        f"{CONFIG['OLLAMA_URL']}/v1/chat/completions",
        json={"model": CONFIG["MODEL_NAME"], "messages": messages, "stream": False},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def submit_result(job_id, result=None, error=None):
    requests.post(
        f"{CONFIG['ORACLE_URL']}/jobs/{job_id}/result",
        params={"node_id": NODE_ID},
        json={"result": result, "error": error},
        headers=HEADERS,
        timeout=10,
        verify=CONFIG["VERIFY_TLS"],
    )


def worker_loop_once():
    loop_count = 0
    while True:
        loop_count += 1
        if loop_count % 10 == 0:
            register_node()

        job = fetch_next_job()
        if job is None:
            time.sleep(2)
            continue

        job_id = job["id"]
        messages = job["payload"]["messages"]
        log.info(f"領到任務 {job_id}，開始推論")
        try:
            result = run_inference(messages)
            submit_result(job_id, result=result)
            log.info(f"任務 {job_id} 完成")
        except Exception as e:
            log.error(f"任務 {job_id} 推論失敗：{e}")
            submit_result(job_id, error=str(e))


def supervised_worker_loop():
    while True:
        try:
            worker_loop_once()
        except Exception as e:
            log.error(f"主迴圈發生未預期錯誤，10 秒後自動重啟：{e}")
            time.sleep(10)


def main():
    log.info(f"=== FinFlow 邊緣節點啟動程序開始（平台：{PLATFORM}，節點：{NODE_ID}）===")
    if NODE_ID not in CONFIG["ORACLE_URL"] and CONFIG["NODE_API_KEY"] == "change-me-must-match-oracle-side":
        log.warning("⚠️ NODE_API_KEY 仍是預設值，請務必設定環境變數，否則 Oracle 端會拒絕此節點")
    ensure_dependencies()
    ensure_model_cached()
    start_ollama_and_wait()
    register_node()
    log.info("=== 啟動完成，進入主迴圈 ===")
    supervised_worker_loop()


if __name__ == "__main__":
    main()
