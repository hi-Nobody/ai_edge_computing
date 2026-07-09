import os
import time
import requests
import logging
import concurrent.futures
from g4f.client import Client

ORACLE_URL = "http://127.0.0.1:8000"
NODE_ID = "oracle-g4f-virtual"
NODE_API_KEY = os.environ.get("G4F_NODE_API_KEY", "change-me-g4f")  # 請至 server.py 的金鑰 JSON 中註冊
MODEL_NAME = "gpt-4o"
JOB_TIMEOUT_SECONDS = 60  # 若 g4f 呼叫卡住超過此秒數，放棄並回報逾時 (避免心跳被卡住)
PLATFORM = "g4f-virtual"  # server.py 的 HeartbeatRequest 需要扁平的 platform 欄位
VIRTUAL_VRAM_GB = 99.0  # 逆向 API 沒有實體 VRAM 限制，回報一個寬鬆的假值即可

HEADERS = {"x-api-key": NODE_API_KEY, "Content-Type": "application/json"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s [g4f-virtual-worker] %(message)s")
log = logging.getLogger("g4f")

g4f_client = Client()
# 單執行緒池：只用來對 g4f 呼叫套用 timeout，本身仍是一次處理一個任務
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


def register_node():
    try:
        # 修正：server.py 的 HeartbeatRequest 要求扁平欄位（node_id/platform/
        # current_model/vram_gb），原本送巢狀 "capability" 物件會被 422 拒絕
        res = requests.post(
            f"{ORACLE_URL}/nodes/heartbeat",
            headers=HEADERS,
            json={
                "node_id": NODE_ID,
                "platform": PLATFORM,
                "current_model": MODEL_NAME,
                "vram_gb": VIRTUAL_VRAM_GB,
            },
            timeout=5,
        )
        if res.status_code != 200:
            log.warning(f"心跳回應異常 status={res.status_code} body={res.text[:200]}")
    except Exception as e:
        log.warning(f"虛擬節點發送心跳失敗: {e}")


def run_g4f(messages):
    """實際呼叫 g4f（會被丟到背景執行緒以套用 timeout）。"""
    response = g4f_client.chat.completions.create(model=MODEL_NAME, messages=messages)
    return response.choices[0].message.content


def post_result(job_id, payload):
    try:
        res = requests.post(
            f"{ORACLE_URL}/jobs/{job_id}/result?node_id={NODE_ID}",
            headers=HEADERS,
            json=payload,
            timeout=10,
        )
        if res.status_code != 200:
            log.warning(f"結果回傳異常 job={job_id} status={res.status_code} body={res.text[:200]}")
    except Exception as e:
        log.error(f"結果回傳失敗 job={job_id}: {e}")


def worker_loop():
    log.info(f"G4F 逆向 API 虛擬常駐節點已就緒 (監聽模型: {MODEL_NAME})")
    loop_count = 0
    while True:
        try:
            if loop_count % 15 == 0:
                register_node()

            res = requests.get(f"{ORACLE_URL}/jobs/next?node_id={NODE_ID}", headers=HEADERS, timeout=10)
            if res.status_code == 200:
                # 修正：server.py 的 GET /jobs/next 回傳的是扁平的
                # {"job_id":..., "payload":...}，沒有巢狀的 "job" 這一層，
                # 原本的 res.json().get("job") 永遠會是 None
                job_data = res.json()
                job_id = job_data.get("job_id")
                messages = (job_data.get("payload") or {}).get("messages")

                if not job_id or not messages:
                    log.debug("目前沒有待處理任務或格式不含 messages，略過")
                else:
                    log.info(f"核心指派逆向代理任務: {job_id}，開始向免費網頁端點穿透...")
                    try:
                        future = executor.submit(run_g4f, messages)
                        result_text = future.result(timeout=JOB_TIMEOUT_SECONDS)
                        post_result(job_id, {"result": result_text})
                        log.info(f"逆向任務 {job_id} 完成並成功交回結果。")
                    except concurrent.futures.TimeoutError:
                        log.error(f"逆向穿透逾時 (>{JOB_TIMEOUT_SECONDS}s): job={job_id}")
                        post_result(job_id, {"error": f"G4F timeout after {JOB_TIMEOUT_SECONDS}s"})
                    except Exception as g4f_err:
                        log.error(f"逆向穿透失敗: {g4f_err}")
                        post_result(job_id, {"error": f"G4F Web Error: {str(g4f_err)}"})
            else:
                log.warning(f"取得任務失敗 status={res.status_code} body={res.text[:200]}")

            loop_count += 1
            time.sleep(2)
        except Exception as e:
            log.error(f"主迴圈發生未預期錯誤: {e}")
            time.sleep(5)


if __name__ == "__main__":
    worker_loop()
