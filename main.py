"""
FinFlow 任務佇列伺服器 v3 —— 在 Oracle Cloud 上運行

相對 v2 新增三項（對應規劃文件第②③④項）：
1. Per-node API Key —— 每個邊緣節點有獨立金鑰，可單獨撤權，且金鑰比對改用
   secrets.compare_digest 常數時間比較，修補 timing attack 缺口
2. DAG 依賴（depends_on）—— 任務可宣告依賴其他任務，由確定性規則執行，
   不交給 AI 即時判斷（詳見規劃文件 4b 的評估理由）
3. 模型一致性路由（required_capability.model_name）—— 可指定任務必須由
   載入特定模型的節點處理，緩解第4c項提到的「模型異質性品質飄移」問題

v1 沿用：能力比對分配、任務狀態機、逾時重新分配
v2 沿用：彙整端點、Session/Context 管理與自動壓縮、資源枯竭偵測與 Telegram 通知

啟動方式：
    pip install -r requirements.txt
    export CLIENT_API_KEY="給你的開發工具用的金鑰"
    export NODE_API_KEYS_JSON='{"kaggle-1":"金鑰A","lightning-1":"金鑰B"}'
    export TELEGRAM_BOT_TOKEN="（可選）"
    export TELEGRAM_CHAT_ID="（可選）"
    uvicorn main:app --host 127.0.0.1 --port 8000
    # 注意：改成只 bind 127.0.0.1，對外的 443 由 Caddy 反向代理負責（見 Caddyfile）
"""

import os
import sqlite3
import time
import uuid
import json
import secrets
import threading
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ---------- 設定 ----------
DB_PATH = os.environ.get("QUEUE_DB_PATH", "queue.db")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "change-me-client-key")
NODE_API_KEYS: Dict[str, str] = json.loads(os.environ.get("NODE_API_KEYS_JSON", "{}"))
JOB_CLAIM_TIMEOUT_SEC = int(os.environ.get("JOB_CLAIM_TIMEOUT_SEC", "300"))
JOB_MAX_RETRY = int(os.environ.get("JOB_MAX_RETRY", "3"))
NODE_DEAD_AFTER_SEC = int(os.environ.get("NODE_DEAD_AFTER_SEC", "60"))
LONG_POLL_TIMEOUT_SEC = int(os.environ.get("LONG_POLL_TIMEOUT_SEC", "90"))
LONG_POLL_INTERVAL_SEC = 1.5
SESSION_COMPACT_THRESHOLD = int(os.environ.get("SESSION_COMPACT_THRESHOLD", "20"))
SESSION_COMPACT_KEEP_RECENT = int(os.environ.get("SESSION_COMPACT_KEEP_RECENT", "6"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFY_COOLDOWN_SEC = int(os.environ.get("NOTIFY_COOLDOWN_SEC", "1800"))
PENDING_TOO_LONG_SEC = int(os.environ.get("PENDING_TOO_LONG_SEC", "180"))

app = FastAPI(title="FinFlow Edge Queue v3")


# ---------- 資料庫 ----------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                job_type TEXT NOT NULL DEFAULT 'single',
                payload TEXT NOT NULL,
                required_capability TEXT,
                depends_on TEXT,
                result TEXT,
                error TEXT,
                claimed_by TEXT,
                created_at REAL NOT NULL,
                claimed_at REAL,
                completed_at REAL,
                retry_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                last_heartbeat REAL NOT NULL,
                capability TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                history TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notify_state (
                key TEXT PRIMARY KEY,
                last_notified_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.commit()


# ---------- 第②項：Per-node API Key（常數時間比較，修補 timing attack）----------

def verify_client_key(x_api_key: Optional[str]):
    if not secrets.compare_digest(x_api_key or "", CLIENT_API_KEY):
        raise HTTPException(status_code=401, detail="invalid client api key")


def verify_node_key(node_id: str, x_api_key: Optional[str]):
    """每個節點必須用自己專屬的金鑰，且該 node_id 必須已預先登記在 NODE_API_KEYS_JSON。
    未登記的 node_id 直接拒絕——這代表「新節點上線」需要你先手動分配一把金鑰給它，
    這是刻意的設計：換取「可以單獨撤掉某一個節點的存取權」這個能力。"""
    expected = NODE_API_KEYS.get(node_id)
    if expected is None or not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="invalid or unregistered node api key")


# ---------- 第①③④項共用：能力比對（含 VRAM 與模型名稱比對）----------

def capability_satisfies(node_capability: dict, required: Optional[dict]) -> bool:
    if not required:
        return True
    min_vram = required.get("min_vram_gb")
    if min_vram is not None:
        node_vram = node_capability.get("vram_gb")
        if node_vram is not None and node_vram < min_vram:
            return False
        # node_vram 為 None（偵測失敗）時維持寬鬆放行，理由見上一輪規劃文件 4c 段落

    model_name = required.get("model_name")
    if model_name is not None:
        if node_capability.get("model") != model_name:
            return False  # 模型名稱屬於明確指定的需求，不寬鬆放行——這是有意的不對稱設計：
            # VRAM 偵測失敗時「猜測可能夠用」造成的最差後果是變慢，但模型指定錯誤
            # 會直接影響輸出品質與風格一致性，因此這裡選擇嚴格比對而非寬鬆放行

    return True


def get_node_capability(node_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT capability FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    return json.loads(row["capability"]) if row and row["capability"] else {}


# ---------- 第③項：DAG 依賴檢查 ----------

def dependencies_completed(depends_on: Optional[list]) -> bool:
    if not depends_on:
        return True
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in depends_on)
        rows = conn.execute(
            f"SELECT status FROM jobs WHERE id IN ({placeholders})", depends_on
        ).fetchall()
    if len(rows) != len(depends_on):
        return False  # 引用了不存在的 job_id，視為尚未滿足
    return all(r["status"] == "completed" for r in rows)


def cascade_fail_blocked_jobs():
    """若某任務依賴的其他任務已經失敗，這個任務永遠等不到依賴完成，
    與其讓它無限期卡在 pending，主動串聯標記為失敗並說明原因。"""
    with get_conn() as conn:
        pending_with_deps = conn.execute(
            "SELECT id, depends_on FROM jobs WHERE status='pending' AND depends_on IS NOT NULL"
        ).fetchall()
        for row in pending_with_deps:
            dep_ids = json.loads(row["depends_on"])
            if not dep_ids:
                continue
            placeholders = ",".join("?" for _ in dep_ids)
            dep_rows = conn.execute(
                f"SELECT status FROM jobs WHERE id IN ({placeholders})", dep_ids
            ).fetchall()
            if any(r["status"] == "failed" for r in dep_rows):
                conn.execute(
                    "UPDATE jobs SET status='failed', error='依賴的任務已失敗，串聯標記為失敗' WHERE id=?",
                    (row["id"],),
                )
        conn.commit()


def claim_next_job(node_id: str) -> Optional[dict]:
    node_capability = get_node_capability(node_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, payload, required_capability, depends_on FROM jobs "
            "WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        for row in rows:
            depends_on = json.loads(row["depends_on"]) if row["depends_on"] else None
            if not dependencies_completed(depends_on):
                continue  # 依賴未完成，跳過此任務，不可被領取
            required = json.loads(row["required_capability"]) if row["required_capability"] else None
            if capability_satisfies(node_capability, required):
                job_id = row["id"]
                conn.execute(
                    "UPDATE jobs SET status='claimed', claimed_by=?, claimed_at=? WHERE id=? AND status='pending'",
                    (node_id, time.time(), job_id),
                )
                conn.commit()
                return {"id": job_id, "payload": json.loads(row["payload"])}
        conn.rollback()
        return None


def requeue_or_fail_stale_jobs():
    cutoff = time.time() - JOB_CLAIM_TIMEOUT_SEC
    with get_conn() as conn:
        stale = conn.execute(
            "SELECT id, retry_count FROM jobs WHERE status='claimed' AND claimed_at < ?", (cutoff,)
        ).fetchall()
        for row in stale:
            if row["retry_count"] + 1 >= JOB_MAX_RETRY:
                conn.execute(
                    "UPDATE jobs SET status='failed', error='超過重試上限，可能所有節點都已離線' WHERE id=?",
                    (row["id"],),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='pending', claimed_by=NULL, retry_count=retry_count+1 WHERE id=?",
                    (row["id"],),
                )
        conn.commit()


# ---------- 資源枯竭偵測 + Telegram 通知（v2 沿用）----------

def notify(message: str, key: str = "default"):
    print(f"[ALERT] {message}")
    with get_conn() as conn:
        row = conn.execute("SELECT last_notified_at FROM notify_state WHERE key=?", (key,)).fetchone()
        if row and time.time() - row["last_notified_at"] < NOTIFY_COOLDOWN_SEC:
            return
        conn.execute(
            "INSERT INTO notify_state (key, last_notified_at) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_notified_at=excluded.last_notified_at",
            (key, time.time()),
        )
        conn.commit()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("（未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，僅記錄日誌，未發送即時通知）")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram 通知發送失敗：{e}")


def check_resource_exhaustion():
    cutoff = time.time() - NODE_DEAD_AFTER_SEC
    with get_conn() as conn:
        alive_count = conn.execute(
            "SELECT COUNT(*) AS c FROM nodes WHERE last_heartbeat > ?", (cutoff,)
        ).fetchone()["c"]
        oldest_pending_row = conn.execute(
            "SELECT MIN(created_at) AS t FROM jobs WHERE status='pending'"
        ).fetchone()
        oldest_pending = oldest_pending_row["t"]

    if alive_count == 0:
        notify(
            "FinFlow 警示：目前沒有任何邊緣節點在線上，所有新任務都會等待逾時。"
            "請檢查 Kaggle / Colab / Lightning AI 是否需要重新啟動 Session 並重跑 bootstrap.py。",
            key="no_alive_nodes",
        )
        return

    if oldest_pending and (time.time() - oldest_pending) > PENDING_TOO_LONG_SEC:
        notify(
            f"FinFlow 警示：有任務已等待超過 {PENDING_TOO_LONG_SEC} 秒仍未被任何節點領取。"
            f"目前線上節點數：{alive_count}，可能是配額用盡、能力不匹配，或依賴鏈卡住。",
            key="pending_too_long",
        )


def background_loop():
    while True:
        try:
            requeue_or_fail_stale_jobs()
            cascade_fail_blocked_jobs()
            check_resource_exhaustion()
        except Exception as e:
            print("background loop error:", e)
        time.sleep(15)


# ---------- Session / Context 管理（v2 沿用）----------

def get_session_history(session_id: str) -> list:
    with get_conn() as conn:
        row = conn.execute("SELECT history FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return json.loads(row["history"]) if row else []


def save_session_history(session_id: str, history: list):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, history, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
            (session_id, json.dumps(history, ensure_ascii=False), time.time()),
        )
        conn.commit()


def submit_job_and_wait(
    payload: dict,
    job_type: str = "single",
    required_capability: Optional[dict] = None,
    max_wait_seconds: Optional[int] = None,
) -> str:
    job_id = create_job(payload, job_type=job_type, required_capability=required_capability)
    deadline = time.time() + (max_wait_seconds or LONG_POLL_TIMEOUT_SEC)
    while time.time() < deadline:
        with get_conn() as conn:
            row = conn.execute("SELECT status, result, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row["status"] == "completed":
            return row["result"]
        if row["status"] == "failed":
            raise HTTPException(status_code=502, detail=row["error"] or "edge node failed")
        time.sleep(LONG_POLL_INTERVAL_SEC)
    raise HTTPException(status_code=503, detail="目前沒有邊緣節點在線上領取任務，請確認 bootstrap.py 是否正在運行")


def create_job(
    payload: dict,
    job_type: str = "single",
    required_capability: Optional[dict] = None,
    depends_on: Optional[list] = None,
) -> str:
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, job_type, payload, required_capability, depends_on, created_at) "
            "VALUES (?, 'pending', ?, ?, ?, ?, ?)",
            (
                job_id,
                job_type,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(required_capability) if required_capability else None,
                json.dumps(depends_on) if depends_on else None,
                time.time(),
            ),
        )
        conn.commit()
    return job_id


def maybe_compact_session(history: list) -> list:
    if len(history) <= SESSION_COMPACT_THRESHOLD:
        return history
    to_compact = history[:-SESSION_COMPACT_KEEP_RECENT]
    recent = history[-SESSION_COMPACT_KEEP_RECENT:]
    compact_messages = [
        {"role": "system", "content": "請將以下對話歷史濃縮成一段簡短摘要，務必保留關鍵決策、檔案/變數名稱、尚未解決的問題，去除無關的寒暄與重複內容。"},
        {"role": "user", "content": json.dumps(to_compact, ensure_ascii=False)},
    ]
    summary = submit_job_and_wait({"model": "compactor", "messages": compact_messages}, job_type="compact")
    return [{"role": "system", "content": f"[先前對話摘要] {summary}"}] + recent


# ---------- Pydantic models ----------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "edge-default"
    messages: List[ChatMessage]
    session_id: Optional[str] = None
    required_capability: Optional[Dict[str, Any]] = None
    max_wait_seconds: Optional[int] = None


class AggregateRequest(BaseModel):
    source_job_ids: List[str]
    instruction: str
    max_wait_seconds: Optional[int] = None


class JobSubmitRequest(BaseModel):
    """第③項新增：給進階多步驟編排腳本使用的低層級端點，
    支援 depends_on，不像 /v1/chat/completions 那樣立即 long-poll 等結果。"""
    model: str = "edge-default"
    messages: List[ChatMessage]
    depends_on: Optional[List[str]] = None
    required_capability: Optional[Dict[str, Any]] = None
    job_type: str = "single"


class JobResult(BaseModel):
    result: Optional[str] = None
    error: Optional[str] = None


class Heartbeat(BaseModel):
    node_id: str
    capability: Optional[Dict[str, Any]] = None


# ---------- 給開發工具用的 OpenAI 相容端點（client key）----------

@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest, x_api_key: Optional[str] = Header(None)):
    verify_client_key(x_api_key)

    if req.session_id:
        history = get_session_history(req.session_id)
        history = maybe_compact_session(history)
        history.extend([m.dict() for m in req.messages])
    else:
        history = [m.dict() for m in req.messages]

    payload = {"model": req.model, "messages": history}
    result = submit_job_and_wait(
        payload,
        job_type="single",
        required_capability=req.required_capability,
        max_wait_seconds=req.max_wait_seconds,
    )

    if req.session_id:
        history.append({"role": "assistant", "content": result})
        save_session_history(req.session_id, history)

    return {
        "id": str(uuid.uuid4()),
        "object": "chat.completion",
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result},
            "finish_reason": "stop",
        }],
    }


@app.post("/jobs/aggregate")
def aggregate_jobs(req: AggregateRequest, x_api_key: Optional[str] = Header(None)):
    verify_client_key(x_api_key)
    if not req.source_job_ids:
        raise HTTPException(status_code=400, detail="source_job_ids 不可為空")

    placeholders = ",".join("?" for _ in req.source_job_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, result FROM jobs WHERE id IN ({placeholders}) AND status='completed'",
            req.source_job_ids,
        ).fetchall()

    if len(rows) != len(req.source_job_ids):
        raise HTTPException(status_code=400, detail="部分來源任務尚未完成或不存在，無法彙整")

    combined = "\n\n".join(f"【結果 {r['id']}】\n{r['result']}" for r in rows)
    messages = [
        {"role": "system", "content": req.instruction},
        {"role": "user", "content": combined},
    ]
    result = submit_job_and_wait(
        {"model": "aggregator", "messages": messages},
        job_type="aggregate",
        max_wait_seconds=req.max_wait_seconds,
    )
    return {"result": result}


# ---------- 第③項：DAG 任務提交與查詢端點（client key，非阻塞）----------

@app.post("/jobs")
def submit_job(req: JobSubmitRequest, x_api_key: Optional[str] = Header(None)):
    verify_client_key(x_api_key)
    job_id = create_job(
        payload={"model": req.model, "messages": [m.dict() for m in req.messages]},
        job_type=req.job_type,
        required_capability=req.required_capability,
        depends_on=req.depends_on,
    )
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str, x_api_key: Optional[str] = Header(None)):
    verify_client_key(x_api_key)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, status, result, error, depends_on FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": row["id"],
        "status": row["status"],
        "result": row["result"],
        "error": row["error"],
        "depends_on": json.loads(row["depends_on"]) if row["depends_on"] else None,
    }


# ---------- 給邊緣節點 bootstrap.py 用的端點（per-node key）----------

@app.get("/jobs/next")
def get_next_job(node_id: str, x_api_key: Optional[str] = Header(None)):
    verify_node_key(node_id, x_api_key)
    job = claim_next_job(node_id)
    return {"job": job}


@app.post("/jobs/{job_id}/result")
def submit_result(job_id: str, body: JobResult, node_id: str, x_api_key: Optional[str] = Header(None)):
    verify_node_key(node_id, x_api_key)
    with get_conn() as conn:
        if body.error:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, completed_at=? WHERE id=? AND claimed_by=?",
                (body.error, time.time(), job_id, node_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='completed', result=?, completed_at=? WHERE id=? AND claimed_by=?",
                (body.result, time.time(), job_id, node_id),
            )
        conn.commit()
    return {"ok": True}


@app.post("/nodes/heartbeat")
def heartbeat(body: Heartbeat, x_api_key: Optional[str] = Header(None)):
    verify_node_key(body.node_id, x_api_key)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (node_id, last_heartbeat, capability) VALUES (?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat, capability=excluded.capability",
            (body.node_id, time.time(), json.dumps(body.capability or {})),
        )
        conn.commit()
    return {"ok": True}


@app.get("/nodes")
def list_nodes(x_api_key: Optional[str] = Header(None)):
    verify_client_key(x_api_key)
    cutoff = time.time() - NODE_DEAD_AFTER_SEC
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
    return [
        {
            "node_id": r["node_id"],
            "alive": r["last_heartbeat"] > cutoff,
            "capability": json.loads(r["capability"] or "{}"),
        }
        for r in rows
    ]


@app.get("/healthz")
def healthz():
    return {"ok": True}


init_db()
threading.Thread(target=background_loop, daemon=True).start()
