"""
FinFlow 任務佇列伺服器 v4 —— 在 Oracle Cloud 上運行

本版以使用者提供的 Gemini 修改版（優先權、中斷偵測、DLQ、扁平化 nodes schema）
為基礎，保留其有價值的改進，修正以下問題（詳細原因見對話說明，不在此重複）：
  1. /system/cron 改回自動背景執行緒觸發，且補上驗證（原版完全公開無驗證）
  2. 派工邏輯重新接回 required_capability 比對（原版完全沒檢查，是死碼）
  3. 修正 DAG 依賴檢查的 vacuous-truth 漏洞（依賴不存在的 job 會被誤判為已完成）
  4. 補上依賴失敗時的串聯失敗（避免任務無限期卡在 pending）
  5. 補回 /jobs/aggregate 彙整端點
  6. 補回 Session 自動壓縮（避免長對話 context 無限增長）
  7. 補回 POST /jobs + GET /jobs/{id} 給進階 DAG 編排腳本使用
  8. 補回 GET /nodes 監控端點（原版重構時遺漏，導致沒有任何方式查詢節點存活狀態）

啟動方式：
    pip install fastapi uvicorn pydantic requests
    export CLIENT_API_KEY="..."
    export NODE_API_KEYS_JSON='{"kaggle-1":"...","lightning-1":"..."}'
    uvicorn server:app --host 127.0.0.1 --port 8000
"""

import os
import time
import json
import uuid
import sqlite3
import secrets
import asyncio
import threading
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request
from pydantic import BaseModel

# ---------- 系統常數與環境變數 ----------
DB_PATH = os.environ.get("QUEUE_DB_PATH", "finflow_queue.db")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "change-me-client-key")
NODE_API_KEYS: Dict[str, str] = json.loads(os.environ.get("NODE_API_KEYS_JSON", "{}"))

JOB_CLAIM_TIMEOUT_SEC = 300
JOB_MAX_RETRY = 3
NODE_DEAD_AFTER_SEC = 60
LONG_POLL_TIMEOUT_SEC = 90
NOTIFY_COOLDOWN_SEC = 1800
PENDING_TOO_LONG_SEC = 180
SESSION_COMPACT_THRESHOLD = int(os.environ.get("SESSION_COMPACT_THRESHOLD", "20"))
SESSION_COMPACT_KEEP_RECENT = int(os.environ.get("SESSION_COMPACT_KEEP_RECENT", "6"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="FinFlow Edge Queue (v4)")


@app.get("/healthz")
def healthz():
    # 刻意不驗證 API Key：這是給 Caddy / OCI Health Check / 監控腳本用的存活探針，
    # 只回報「行程還活著」，不洩漏任何任務內容或節點細節。
    return {"status": "ok", "ts": time.time()}


# ---------- 1. 資料庫初始化 ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT DEFAULT 'chat',
            priority INTEGER DEFAULT 5,
            status TEXT DEFAULT 'pending',
            payload TEXT,
            requested_model TEXT,
            required_capability TEXT,
            depends_on TEXT,
            claimed_by TEXT,
            claimed_at REAL,
            completed_at REAL,
            result TEXT,
            error TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dlq_jobs (
            id TEXT PRIMARY KEY,
            original_payload TEXT,
            last_node TEXT,
            error_reason TEXT,
            failed_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            platform TEXT,
            current_model TEXT,
            vram_gb REAL,
            last_heartbeat REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            history TEXT,
            updated_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_audit_logs (
            log_id TEXT PRIMARY KEY,
            job_id TEXT,
            session_id TEXT,
            assigned_node TEXT,
            executed_platform TEXT,
            executed_model TEXT,
            dispatched_at REAL,
            completed_at REAL,
            duration_ms REAL,
            status TEXT,
            full_prompt_json TEXT,
            response_text TEXT,
            error_message TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notify_state (
            key TEXT PRIMARY KEY,
            last_notified_at REAL
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------- 2. 安全驗證 ----------
def verify_client_key(x_api_key: str = Header(None)):
    if not x_api_key or not secrets.compare_digest(x_api_key, CLIENT_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid Client API Key")
    return x_api_key


def verify_node_key(node_id: str, x_api_key: str = Header(None)):
    expected = NODE_API_KEYS.get(node_id)
    if not expected or not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid or Unregistered Node API Key")
    return x_api_key


# ---------- 3. Pydantic Models ----------
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: str = "default-edge-model"
    session_id: Optional[str] = None
    priority: int = 5
    depends_on: Optional[List[str]] = None
    required_capability: Optional[Dict[str, Any]] = None


class JobSubmitRequest(BaseModel):
    """補回：給進階多步驟編排腳本用的非阻塞提交端點"""
    messages: List[ChatMessage]
    model: str = "default-edge-model"
    type: str = "single"
    priority: int = 5
    depends_on: Optional[List[str]] = None
    required_capability: Optional[Dict[str, Any]] = None


class AggregateRequest(BaseModel):
    """補回：彙整任務便利端點。Oracle 仍然沒有任何 AI 能力——
    這裡只是組裝一個新的 job，把多筆既有結果當作輸入，一樣交給邊緣節點處理"""
    source_job_ids: List[str]
    instruction: str
    priority: int = 5


class JobResult(BaseModel):
    result: Optional[str] = None
    error: Optional[str] = None


class HeartbeatRequest(BaseModel):
    node_id: str
    platform: str
    current_model: str
    vram_gb: Optional[float] = None


# ---------- 4. 能力比對（修正問題二：重新接回比對邏輯）----------

def node_capability_satisfies(node_row: Optional[sqlite3.Row], required: Optional[dict]) -> bool:
    """node_row 為 None，或 vram_gb 為 None/0 且無法確定時，採寬鬆放行——
    這是刻意設計：偵測不到能力時，最差後果是任務變慢，但嚴格擋下會讓
    CPU-only 節點（如 Lightning AI 免費 Studio）永遠領不到任何任務"""
    if not required:
        return True
    if node_row is None:
        return True

    min_vram = required.get("min_vram_gb")
    if min_vram is not None:
        node_vram = node_row["vram_gb"]
        if node_vram is not None and node_vram > 0 and node_vram < min_vram:
            return False

    model_name = required.get("model_name")
    if model_name is not None:
        if node_row["current_model"] != model_name:
            return False  # 模型名稱是明確指定的需求，不寬鬆放行

    return True


def get_node_row(conn, node_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()


# ---------- 5. DAG 依賴檢查（修正問題三、四）----------

def dependencies_status(conn, depends_on: Optional[list]) -> str:
    """回傳 'ready'（可執行）/ 'waiting'（還在等）/ 'blocked'（依賴失敗或指向不存在的任務，永遠無法完成）"""
    if not depends_on:
        return "ready"
    placeholders = ",".join("?" for _ in depends_on)
    rows = conn.execute(f"SELECT status FROM jobs WHERE id IN ({placeholders})", depends_on).fetchall()
    if len(rows) != len(depends_on):
        return "blocked"  # 修正問題三：引用不存在的任務，視為異常而非「已完成」
    if any(r["status"] in ("failed", "cancelled") for r in rows):
        return "blocked"  # 修正問題四：依賴失敗，這個任務也永遠無法正常完成
    if all(r["status"] == "completed" for r in rows):
        return "ready"
    return "waiting"


# ---------- 6. 背景稽核紀錄寫入 ----------
def write_audit_log(job_id: str, session_id: str, assigned_node: str, payload: str, response: str, error: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    job_info = conn.execute("SELECT claimed_at, completed_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    node_info = conn.execute("SELECT platform, current_model FROM nodes WHERE node_id=?", (assigned_node,)).fetchone()

    dispatched_at = job_info["claimed_at"] if job_info else time.time()
    completed_at = job_info["completed_at"] if job_info else time.time()
    duration_ms = (completed_at - dispatched_at) * 1000 if dispatched_at else 0

    executed_platform = node_info["platform"] if node_info else "unknown"
    executed_model = node_info["current_model"] if node_info else "unknown"

    conn.execute("""
        INSERT INTO task_audit_logs
        (log_id, job_id, session_id, assigned_node, executed_platform, executed_model, dispatched_at, completed_at, duration_ms, status, full_prompt_json, response_text, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(uuid.uuid4()), job_id, session_id, assigned_node, executed_platform, executed_model, dispatched_at, completed_at, duration_ms, status, payload, response, error))
    conn.commit()
    conn.close()


# ---------- 7. Session 管理與自動壓縮（補回功能缺口）----------

def get_session_history(conn, session_id: str) -> list:
    row = conn.execute("SELECT history FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return json.loads(row["history"]) if row and row["history"] else []


def save_session_history(conn, session_id: str, history: list):
    conn.execute(
        "INSERT INTO sessions (session_id, history, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
        (session_id, json.dumps(history, ensure_ascii=False), time.time()),
    )
    conn.commit()


def create_job(conn, messages: list, model: str, job_type: str = "single",
               priority: int = 5, required_capability: Optional[dict] = None,
               depends_on: Optional[list] = None) -> str:
    job_id = str(uuid.uuid4())
    payload_str = json.dumps({"messages": messages})
    conn.execute("""
        INSERT INTO jobs (id, type, priority, payload, requested_model, required_capability, depends_on, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, job_type, priority, payload_str, model,
          json.dumps(required_capability) if required_capability else None,
          json.dumps(depends_on) if depends_on else None, time.time()))
    conn.commit()
    return job_id


async def wait_for_job(job_id: str, deadline: float) -> sqlite3.Row:
    while time.time() < deadline:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, result, error, claimed_by FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        if row["status"] in ("completed", "failed"):
            return row
        await asyncio.sleep(1.5)
    raise HTTPException(status_code=504, detail="Timeout waiting for edge nodes")


async def submit_and_wait(messages: list, model: str, job_type: str = "single",
                           priority: int = 5, required_capability: Optional[dict] = None) -> str:
    conn = sqlite3.connect(DB_PATH)
    job_id = create_job(conn, messages, model, job_type, priority, required_capability)
    conn.close()
    row = await wait_for_job(job_id, time.time() + LONG_POLL_TIMEOUT_SEC)
    if row["status"] == "failed":
        raise HTTPException(status_code=502, detail=row["error"] or "edge node failed")
    return row["result"]


async def maybe_compact_session(history: list) -> list:
    if len(history) <= SESSION_COMPACT_THRESHOLD:
        return history
    to_compact = history[:-SESSION_COMPACT_KEEP_RECENT]
    recent = history[-SESSION_COMPACT_KEEP_RECENT:]
    compact_messages = [
        {"role": "system", "content": "請將以下對話歷史濃縮成一段簡短摘要，務必保留關鍵決策、檔案/變數名稱、尚未解決的問題，去除無關的寒暄與重複內容。"},
        {"role": "user", "content": json.dumps(to_compact, ensure_ascii=False)},
    ]
    summary = await submit_and_wait(compact_messages, model="compactor", job_type="compact")
    return [{"role": "system", "content": f"[先前對話摘要] {summary}"}] + recent


# ---------- 8. API 閘道端點 ----------

@app.post("/v1/chat/completions", dependencies=[Depends(verify_client_key)])
async def create_chat_completion(request: Request, body: ChatRequest, background_tasks: BackgroundTasks):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    history = get_session_history(conn, body.session_id) if body.session_id else []
    if body.session_id:
        history = await maybe_compact_session(history)
    full_messages = history + [m.dict() for m in body.messages]

    job_id = create_job(conn, full_messages, body.model, "chat", body.priority,
                         body.required_capability, body.depends_on)
    payload_str = json.dumps({"messages": full_messages})
    conn.close()

    deadline = time.time() + LONG_POLL_TIMEOUT_SEC
    while time.time() < deadline:
        if await request.is_disconnected():
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (job_id,))
            conn.commit()
            conn.close()
            return {}

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, result, error, claimed_by FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()

        if row["status"] == "completed":
            if body.session_id:
                full_messages.append({"role": "assistant", "content": row["result"]})
                conn = sqlite3.connect(DB_PATH)
                save_session_history(conn, body.session_id, full_messages)
                conn.close()

            background_tasks.add_task(write_audit_log, job_id, str(body.session_id), row["claimed_by"], payload_str, row["result"], "", "SUCCESS")

            return {
                "id": job_id,
                "object": "chat.completion",
                "model": body.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": row["result"]}, "finish_reason": "stop"}]
            }

        elif row["status"] == "failed":
            background_tasks.add_task(write_audit_log, job_id, str(body.session_id), row["claimed_by"], payload_str, "", row["error"], "FAILED")
            raise HTTPException(status_code=500, detail=row["error"])

        await asyncio.sleep(1.5)

    raise HTTPException(status_code=504, detail="Timeout waiting for edge nodes")


@app.post("/jobs/aggregate", dependencies=[Depends(verify_client_key)])
async def aggregate_jobs(req: AggregateRequest):
    if not req.source_job_ids:
        raise HTTPException(status_code=400, detail="source_job_ids 不可為空")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in req.source_job_ids)
    rows = conn.execute(
        f"SELECT id, result FROM jobs WHERE id IN ({placeholders}) AND status='completed'",
        req.source_job_ids,
    ).fetchall()
    conn.close()

    if len(rows) != len(req.source_job_ids):
        raise HTTPException(status_code=400, detail="部分來源任務尚未完成或不存在，無法彙整")

    combined = "\n\n".join(f"【結果 {r['id']}】\n{r['result']}" for r in rows)
    messages = [
        {"role": "system", "content": req.instruction},
        {"role": "user", "content": combined},
    ]
    result = await submit_and_wait(messages, model="aggregator", job_type="aggregate", priority=req.priority)
    return {"result": result}


@app.post("/jobs", dependencies=[Depends(verify_client_key)])
def submit_job(req: JobSubmitRequest):
    """補回：非阻塞提交，給需要自己組 DAG 的編排腳本用，不像 /v1/chat/completions 會 long-poll 等結果"""
    conn = sqlite3.connect(DB_PATH)
    job_id = create_job(conn, [m.dict() for m in req.messages], req.model, req.type,
                         req.priority, req.required_capability, req.depends_on)
    conn.close()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}", dependencies=[Depends(verify_client_key)])
def get_job_status(job_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, status, result, error, depends_on FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": row["id"], "status": row["status"], "result": row["result"], "error": row["error"],
        "depends_on": json.loads(row["depends_on"]) if row["depends_on"] else None,
    }


# ---------- 9. 邊緣節點通訊端點 ----------

@app.get("/jobs/next")
def get_next_job(node_id: str, x_api_key: str = Header(None)):
    verify_node_key(node_id, x_api_key)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    node_row = get_node_row(conn, node_id)

    rows = conn.execute("""
        SELECT id, payload, depends_on, required_capability FROM jobs
        WHERE status = 'pending'
        ORDER BY priority ASC, created_at ASC
    """).fetchall()

    target_job = None
    for row in rows:
        depends_on = json.loads(row["depends_on"]) if row["depends_on"] else None
        dep_state = dependencies_status(conn, depends_on)
        if dep_state != "ready":
            continue  # 還在等，或依賴已失敗/不存在（後者由背景巡檢標記失敗，這裡先跳過）

        required = json.loads(row["required_capability"]) if row["required_capability"] else None
        if not node_capability_satisfies(node_row, required):
            continue  # 修正問題二：節點能力不符，跳過讓其他節點接

        target_job = row
        break

    if target_job:
        job_id = target_job["id"]
        conn.execute("UPDATE jobs SET status = 'claimed', claimed_by = ?, claimed_at = ? WHERE id = ?", (node_id, time.time(), job_id))
        conn.commit()
        conn.close()
        return {"job_id": job_id, "payload": json.loads(target_job["payload"])}

    conn.close()
    return {"message": "No pending jobs"}


@app.post("/jobs/{job_id}/result")
def submit_job_result(job_id: str, result: JobResult, node_id: str, x_api_key: str = Header(None)):
    verify_node_key(node_id, x_api_key)
    conn = sqlite3.connect(DB_PATH)
    if result.error:
        conn.execute("UPDATE jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?", (result.error, time.time(), job_id))
    else:
        conn.execute("UPDATE jobs SET status = 'completed', result = ?, completed_at = ? WHERE id = ?", (result.result, time.time(), job_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/nodes/heartbeat")
def node_heartbeat(data: HeartbeatRequest, x_api_key: str = Header(None)):
    verify_node_key(data.node_id, x_api_key)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO nodes (node_id, platform, current_model, vram_gb, last_heartbeat)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat, platform=excluded.platform, current_model=excluded.current_model, vram_gb=excluded.vram_gb
    """, (data.node_id, data.platform, data.current_model, data.vram_gb, time.time()))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/nodes", dependencies=[Depends(verify_client_key)])
def list_nodes():
    """列出所有已註冊節點、是否存活、能力資訊——監控用端點。
    舊版（main.py）用單一 capability JSON 欄位；本版 nodes 表已改成扁平化欄位
    （platform/current_model/vram_gb，見 node_capability_satisfies() 的設計說明），
    這裡回傳時組成對稱的巢狀 capability 物件，維持 API 回應格式對舊呼叫端相容。"""
    cutoff = time.time() - NODE_DEAD_AFTER_SEC
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    conn.close()
    return [
        {
            "node_id": r["node_id"],
            "alive": r["last_heartbeat"] > cutoff,
            "last_heartbeat": r["last_heartbeat"],
            "capability": {
                "platform": r["platform"],
                "model": r["current_model"],
                "vram_gb": r["vram_gb"],
            },
        }
        for r in rows
    ]


# ---------- 10. 背景容錯巡檢（修正問題一：補回自動執行緒；修正問題四：補上串聯失敗）----------

def notify(message: str, key: str):
    print(f"[ALERT] {message}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("（未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，僅記錄日誌）")
        return
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5,
        )
    except Exception as e:
        print(f"Telegram 通知發送失敗：{e}")


def should_notify(conn, key: str) -> bool:
    row = conn.execute("SELECT last_notified_at FROM notify_state WHERE key=?", (key,)).fetchone()
    now = time.time()
    if row and now - row[0] < NOTIFY_COOLDOWN_SEC:
        return False
    conn.execute(
        "INSERT INTO notify_state (key, last_notified_at) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET last_notified_at=excluded.last_notified_at", (key, now),
    )
    conn.commit()
    return True


def run_maintenance():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = time.time()

    # (a) 逾時重新分配 / 超過重試上限進 DLQ
    stale_jobs = conn.execute(
        "SELECT id, retry_count, claimed_by, payload FROM jobs WHERE status = 'claimed' AND ? - claimed_at > ?",
        (now, JOB_CLAIM_TIMEOUT_SEC),
    ).fetchall()
    for job in stale_jobs:
        if job["retry_count"] >= JOB_MAX_RETRY:
            conn.execute(
                "INSERT INTO dlq_jobs (id, original_payload, last_node, error_reason, failed_at) VALUES (?, ?, ?, ?, ?)",
                (job["id"], job["payload"], job["claimed_by"], "Max retries exceeded", now),
            )
            conn.execute("UPDATE jobs SET status = 'failed', error = 'Max retries exceeded' WHERE id = ?", (job["id"],))
        else:
            conn.execute(
                "UPDATE jobs SET status = 'pending', retry_count = retry_count + 1, claimed_by = NULL WHERE id = ?",
                (job["id"],),
            )

    # (b) 修正問題四：依賴已失敗或指向不存在任務的 pending job，串聯標記失敗
    pending_with_deps = conn.execute(
        "SELECT id, depends_on FROM jobs WHERE status='pending' AND depends_on IS NOT NULL"
    ).fetchall()
    for row in pending_with_deps:
        depends_on = json.loads(row["depends_on"])
        if dependencies_status(conn, depends_on) == "blocked":
            conn.execute(
                "UPDATE jobs SET status='failed', error='依賴的任務已失敗或不存在，串聯標記為失敗' WHERE id=?",
                (row["id"],),
            )

    conn.commit()

    # (c) 資源枯竭偵測
    active_nodes = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE ? - last_heartbeat < ?", (now, NODE_DEAD_AFTER_SEC)
    ).fetchone()["c"]

    if active_nodes == 0:
        if should_notify(conn, "no_alive_nodes"):
            notify("FinFlow 警示：目前沒有任何邊緣節點在線上，所有新任務都會等待逾時。", "no_alive_nodes")
    else:
        oldest_pending = conn.execute(
            "SELECT MIN(created_at) as t FROM jobs WHERE status='pending'"
        ).fetchone()["t"]
        if oldest_pending and (now - oldest_pending) > PENDING_TOO_LONG_SEC:
            if should_notify(conn, "pending_too_long"):
                notify(
                    f"FinFlow 警示：有任務等待超過 {PENDING_TOO_LONG_SEC} 秒未被領取，"
                    f"線上節點數 {active_nodes}，可能是能力不匹配或依賴鏈卡住。",
                    "pending_too_long",
                )

    conn.close()


@app.get("/system/cron", dependencies=[Depends(verify_client_key)])
def background_maintenance():
    """保留此端點供手動觸發測試（已加上驗證），但正常運作不依賴它——
    見下方 _background_loop 會自動每 15 秒執行同一段邏輯"""
    run_maintenance()
    return {"status": "maintenance_run"}


def _background_loop():
    while True:
        try:
            run_maintenance()
        except Exception as e:
            print("background loop error:", e)
        time.sleep(15)


threading.Thread(target=_background_loop, daemon=True).start()
