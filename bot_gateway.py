"""
FinFlow Bot Gateway —— Telegram / LINE Webhook Middleware（監聽 127.0.0.1:8001）

用途：補上 Caddyfile 裡 `/telegram/*`、`/line/*` proxy 到 127.0.0.1:8001 這個洞。
之前這兩個路由指向的服務不存在，現在這支程式就是那個服務。

架構原則（跟 server.py / DEPLOY.md 一致，Oracle 端仍然「零 AI」）：
    Telegram/LINE 使用者傳訊息
        -> Caddy (443, TLS) -> 本程式 (8001)
        -> 驗證簽章 -> 立刻回 200 給 Telegram/LINE（避免對方判定逾時而重送）
        -> 背景工作：組裝訊息 -> 呼叫內部 FinFlow 佇列 (127.0.0.1:8000) 的
           POST /jobs（非阻塞提交，不是 /v1/chat/completions，
           因為 /v1/chat/completions 內建 90 秒 long-poll 上限，
           若邊緣節點還在啟動 Ollama、下載模型，90 秒常常不夠，
           用 /jobs + 輪詢 GET /jobs/{id} 可以自訂等待更久，且不會重複建立任務）
        -> 任務完成 -> 用 Telegram sendMessage / LINE push 把結果推回使用者

已知的刻意簡化（不是遺漏，是本輪的取捨）：
    - 對話歷史（session）用本程式自己的 SQLite 管理，只做「保留最近 N 則」的
      簡單截斷，沒有沿用 server.py 裡 /v1/chat/completions 那套「AI 摘要壓縮」
      機制（因為那套機制掛在 /v1/chat/completions 底下，而本程式為了避免
      90 秒逾時限制改走 /jobs，兩者無法直接共用）。長對話還是會被截斷，
      只是用「捨棄最舊訊息」而非「AI 摘要」的方式，效果較陽春但邏輯簡單可靠。
    - Webhook 重送去重（dedup）用行程內的記憶體集合，重啟後會清空。
      Telegram/LINE 只有在「沒收到 200」時才會重送，本程式一律先回 200 才處理，
      重送機率極低，這裡用記憶體而非資料庫是刻意的簡化。
    - LINE 每月只有 200 則免費訊息額度（Messaging API），詳見 DEPLOY.md 的說明；
      若流量超過，需自行評估付費或改以 Telegram 為主。
"""

import os
import time
import json
import hmac
import base64
import hashlib
import sqlite3
import logging
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, Request, Header, BackgroundTasks, HTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot-gateway")

# ---------- 環境變數 ----------
ORACLE_INTERNAL_URL = os.environ.get("ORACLE_INTERNAL_URL", "http://127.0.0.1:8000")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "")  # 必須跟 finflow-queue.service 裡的值一致

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # 對應 setWebhook 的 secret_token

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

GATEWAY_DB_PATH = os.environ.get("GATEWAY_DB_PATH", "bot_gateway.db")
JOB_WAIT_TIMEOUT_SEC = int(os.environ.get("JOB_WAIT_TIMEOUT_SEC", "900"))  # 背景等待任務完成的總預算，預設 15 分鐘
JOB_POLL_INTERVAL_SEC = 2.0
HISTORY_MAX_MESSAGES = int(os.environ.get("HISTORY_MAX_MESSAGES", "20"))  # 簡單截斷用，非 AI 摘要
TELEGRAM_MAX_CHARS = 4000
LINE_MAX_CHARS = 4900

app = FastAPI(title="FinFlow Bot Gateway")

# 記憶體去重集合：(platform, update_id) -> 處理時間。重啟即清空，見檔頭說明。
_seen_updates: Dict[str, float] = {}
_SEEN_TTL_SEC = 600


def _dedup_check_and_mark(key: str) -> bool:
    """回傳 True 代表這是新事件（應處理），False 代表已處理過（忽略）"""
    now = time.time()
    # 順手清掉過期紀錄，避免這個 dict 無限增長
    for k in [k for k, ts in _seen_updates.items() if now - ts > _SEEN_TTL_SEC]:
        _seen_updates.pop(k, None)
    if key in _seen_updates:
        return False
    _seen_updates[key] = now
    return True


# ---------- 對話歷史（簡單 SQLite KV，session_key -> messages JSON） ----------

def _db():
    conn = sqlite3.connect(GATEWAY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_key TEXT PRIMARY KEY,
            history TEXT,
            updated_at REAL
        )
    """)
    return conn


def get_history(session_key: str) -> List[Dict[str, str]]:
    conn = _db()
    row = conn.execute("SELECT history FROM conversations WHERE session_key=?", (session_key,)).fetchone()
    conn.close()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return []


def save_history(session_key: str, history: List[Dict[str, str]]):
    # 簡單截斷（非 AI 摘要，見檔頭說明），只保留最近 HISTORY_MAX_MESSAGES 則
    trimmed = history[-HISTORY_MAX_MESSAGES:]
    conn = _db()
    conn.execute(
        "INSERT INTO conversations (session_key, history, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(session_key) DO UPDATE SET history=excluded.history, updated_at=excluded.updated_at",
        (session_key, json.dumps(trimmed, ensure_ascii=False), time.time()),
    )
    conn.commit()
    conn.close()


# ---------- 呼叫內部 FinFlow 佇列 ----------

async def submit_and_wait_via_queue(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """透過 /jobs（非阻塞提交）+ 輪詢 GET /jobs/{id}，取代 /v1/chat/completions 的 90 秒硬性上限。
    回傳 {"ok": True, "result": "..."} 或 {"ok": False, "error": "..."}"""
    headers = {"x-api-key": CLIENT_API_KEY, "Content-Type": "application/json"}
    body = {"messages": messages, "model": "auto", "type": "chat", "priority": 5}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{ORACLE_INTERNAL_URL}/jobs", headers=headers, json=body)
            resp.raise_for_status()
            job_id = resp.json()["job_id"]
        except Exception as e:
            log.exception("送出任務到佇列失敗")
            return {"ok": False, "error": f"無法送出任務：{e}"}

        deadline = time.time() + JOB_WAIT_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                r = await client.get(f"{ORACLE_INTERNAL_URL}/jobs/{job_id}", headers=headers)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning("輪詢任務狀態失敗，稍後重試：%s", e)
                await _sleep(JOB_POLL_INTERVAL_SEC)
                continue

            if data["status"] == "completed":
                return {"ok": True, "result": data["result"]}
            if data["status"] == "failed":
                return {"ok": False, "error": data.get("error") or "邊緣節點處理失敗"}
            await _sleep(JOB_POLL_INTERVAL_SEC)

    return {"ok": False, "error": "等待邊緣節點逾時（可能所有節點都離線或忙碌中）"}


async def _sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n…（內容過長，已截斷）"


# ---------- Telegram ----------

async def telegram_send_message(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        log.error("未設定 TELEGRAM_BOT_TOKEN，無法回覆訊息")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await client.post(url, json={"chat_id": chat_id, "text": _truncate(text, TELEGRAM_MAX_CHARS)})
        except Exception:
            log.exception("Telegram sendMessage 失敗")


async def process_telegram_message(chat_id: int, user_text: str):
    session_key = f"telegram:{chat_id}"
    history = get_history(session_key)
    history.append({"role": "user", "content": user_text})

    result = await submit_and_wait_via_queue(history)

    if result["ok"]:
        history.append({"role": "assistant", "content": result["result"]})
        save_history(session_key, history)
        await telegram_send_message(chat_id, result["result"])
    else:
        await telegram_send_message(chat_id, f"⚠️ 處理失敗：{result['error']}")


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    # 驗證 Telegram 的 secret token（setWebhook 時要帶 secret_token 參數，見 DEPLOY.md）
    if not TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(
        x_telegram_bot_api_secret_token or "", TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    update = await request.json()
    update_id = update.get("update_id")
    if update_id is not None and not _dedup_check_and_mark(f"tg:{update_id}"):
        return {"ok": True}  # 重複的重送，忽略

    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return {"ok": True}  # 非文字訊息（貼圖、圖片等），本輪不處理

    chat_id = message["chat"]["id"]
    text = message["text"]
    background_tasks.add_task(process_telegram_message, chat_id, text)
    return {"ok": True}


# ---------- LINE ----------

def verify_line_signature(body: bytes, signature: Optional[str]) -> bool:
    if not signature or not LINE_CHANNEL_SECRET:
        return False
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def line_reply(reply_token: str, text: str):
    """快速 ACK 用：在 replyToken 有效期內回一句「處理中」，不佔用 push 額度"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": _truncate(text, LINE_MAX_CHARS)}]}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, headers=headers, json=body)
        except Exception:
            log.exception("LINE reply 失敗")


async def line_push(user_id: str, text: str):
    """真正的答案用 push（計入每月 200 則免費額度，見 DEPLOY.md）"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log.error("未設定 LINE_CHANNEL_ACCESS_TOKEN，無法推播訊息")
        return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"to": user_id, "messages": [{"type": "text", "text": _truncate(text, LINE_MAX_CHARS)}]}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await client.post(url, headers=headers, json=body)
        except Exception:
            log.exception("LINE push 失敗")


async def process_line_message(user_id: str, user_text: str):
    session_key = f"line:{user_id}"
    history = get_history(session_key)
    history.append({"role": "user", "content": user_text})

    result = await submit_and_wait_via_queue(history)

    if result["ok"]:
        history.append({"role": "assistant", "content": result["result"]})
        save_history(session_key, history)
        await line_push(user_id, result["result"])
    else:
        await line_push(user_id, f"⚠️ 處理失敗：{result['error']}")


@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: Optional[str] = Header(None),
):
    raw_body = await request.body()
    if not verify_line_signature(raw_body, x_line_signature):
        raise HTTPException(status_code=401, detail="Invalid LINE signature")

    payload = json.loads(raw_body)
    for event in payload.get("events", []):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            continue  # 本輪只處理文字訊息

        event_id = event.get("webhookEventId") or event.get("message", {}).get("id")
        if event_id and not _dedup_check_and_mark(f"line:{event_id}"):
            continue  # 重複重送，忽略

        user_id = event["source"].get("userId")
        text = event["message"]["text"]
        reply_token = event.get("replyToken")

        if not user_id:
            # 群組/聊天室裡沒有開放 userId 取得權限時會發生；本輪僅支援一對一聊天
            if reply_token:
                background_tasks.add_task(line_reply, reply_token, "目前僅支援一對一聊天，尚未支援群組。")
            continue

        # 立刻用 replyToken 回覆「處理中」，避免使用者以為沒反應；真正答案之後用 push 送
        if reply_token:
            background_tasks.add_task(line_reply, reply_token, "已收到，處理中，請稍候…")
        background_tasks.add_task(process_line_message, user_id, text)

    return {"ok": True}


# ---------- 健康檢查 ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok", "ts": time.time()}
