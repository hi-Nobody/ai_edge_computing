"""
FinFlow Bot Gateway —— Telegram / LINE / Discord Webhook Middleware（監聽 127.0.0.1:8001）

用途：補上 Caddyfile 裡 `/telegram/*`、`/line/*`、`/discord/*` proxy 到 127.0.0.1:8001 這個洞。
之前這幾個路由指向的服務不存在（或未實作），現在這支程式就是那個服務。

架構原則（跟 server.py / DEPLOY.md 一致，Oracle 端仍然「零 AI」）：
    Telegram/LINE 使用者傳訊息
        -> Caddy (443, TLS) -> 本程式 (8001)
        -> 驗證簽章 -> 立刻回 200 給 Telegram/LINE（避免對方判定逾時而重送）
        -> 背景工作：組裝訊息 -> 呼叫內部 FinFlow 佇列 (127.0.0.1:8000) 的
           POST /jobs（非阻塞提交，不是 /v1/chat/completions，
           因為 /v1/chat/completions 內建 90 秒 long-poll 上限，
           若邊緣節點還在啟動 Ollama、下載模型，90 秒常常不夠，
           用 /jobs + 輪詢 GET /jobs/{id} 可以自訂等待更久，且不會重複建立任務）
        -> 任務完成 -> 用 Telegram sendMessage / LINE push / Discord webhook 訊息編輯
           把結果推回使用者

Discord 的架構跟 Telegram/LINE 不同，特別說明：
    Discord 沒有「訊息事件 webhook」這種東西（那是 Gateway WebSocket 常駐連線的範疇，
    跟本程式「無狀態 HTTP 服務」的架構不合），本程式改用 Discord 的
    **Interactions Endpoint**（Slash Command）：
        使用者在 Discord 輸入 /ask prompt:<內容>
            -> Discord 官方伺服器打本程式的 /discord/interactions
            -> 驗證 Ed25519 簽章（用 DISCORD_PUBLIC_KEY，不是 HMAC）
            -> 立刻回傳 type=5（DEFERRED，Discord UI 顯示「思考中…」，
               避免 Discord 規定的 3 秒內必須回應的限制）
            -> 背景工作：呼叫佇列 -> 用 interaction token 呼叫
               PATCH .../webhooks/{application_id}/{token}/messages/@original
               把最終結果編輯回那則「思考中」的訊息
    interaction token 的 followup 編輯有效期是 15 分鐘，剛好對應
    JOB_WAIT_TIMEOUT_SEC 預設的 900 秒，這也是選擇 Slash Command 而非其他
    Discord 互動類型的原因之一。
    **已知限制**：Discord 驗證 Interactions Endpoint URL 時，會用官方信任的
    CA 憑證鏈檢查 TLS，**不接受自簽憑證**——這點跟 Telegram webhook 的限制一樣，
    所以 Discord 只能搭配方案 B（正式網域 + Let's Encrypt）或方案 C
    （Cloudflare Tunnel）使用，方案 A（自簽憑證）無法通過 Discord 的端點驗證。

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
    - Discord Slash Command 只支援單一 `prompt` 字串參數，沒有做多輪指令
      （如 `/status`、`/cancel`）；對話歷史仍是「同一個 Discord 使用者 ID」
      共用同一個 session，邏輯與 Telegram/LINE 一致。

節點群控（/start-node、/stop-node、/list-nodes）架構說明：
    這三個指令刻意掛在**另一個獨立的 Discord Application**（另一個 bot、
    另一把 DISCORD_ADMIN_PUBLIC_KEY、另一個端點 /discord/admin-interactions），
    不是掛在使用者問答用的 /ask 那個 bot 底下——理由：
        1. 權限分離：一般使用者只該看到 /ask，節點的開關本來就不該讓每個
           能用 /ask 的人都能操作，指令註冊時也會用 Discord 的
           default_member_permissions 限制成僅管理員可見/可用，雙重防護
        2. 職責分離：把「跟使用者聊天」與「操作雲端資源／花錢」這兩件事
           徹底分開，方便你把管理 bot 邀進獨立的私人伺服器/頻道管理
    兩個 Discord Application 共用同一個 bot_gateway.py process、同一個
    port（8001），只是網址路徑跟簽章驗證的公鑰不同，不需要另外多開一個
    systemd 服務。

    實際控制 Kaggle／Lightning 的邏輯完全不寫在這支檔案裡，委派給
    node_controllers/ 這個獨立套件（見該套件的 docstring）——本檔案只負責
    「這是不是合法的 Discord 請求」「該呼叫哪個 controller」「怎麼把結果
    回覆成 Discord 看得懂的格式」，不知道 Kaggle API、Lightning SDK 長怎樣。

    Kaggle／Lightning 的 SDK 都是同步（阻塞）的，直接在 async def 的
    FastAPI handler 裡呼叫會卡住整個事件迴圈，連 Telegram/LINE 的訊息
    處理都會被拖住。做法：跟 /ask 一樣先回 deferred（type=5），實際呼叫
    丟進 BackgroundTask，BackgroundTask 裡再用 run_in_executor 把真正
    會卡住的 SDK 呼叫丟到獨立執行緒，兩層都不卡事件迴圈。

    Lightning 的閒置自動關閉用獨立的背景執行緒（不是 asyncio 任務、也不是
    server.py 的背景巡檢）定期檢查 GET /nodes 的心跳時間，超過門檻就呼叫
    LightningController.stop()。放在這支檔案而不是 server.py，是刻意
    維持 server.py「零平台知識」的定位，見 DEPLOY.md 的完整說明。
"""

import os
import time
import json
import hmac
import base64
import hashlib
import sqlite3
import logging
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, Request, Header, BackgroundTasks, HTTPException
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

import node_controllers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot-gateway")

# 跑 Kaggle CLI／Lightning SDK 這類同步阻塞呼叫的獨立執行緒池，
# 避免卡住 FastAPI 的 asyncio 事件迴圈（見檔頭架構說明）
_BLOCKING_CALL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="node-ctl")

# ---------- 環境變數 ----------
ORACLE_INTERNAL_URL = os.environ.get("ORACLE_INTERNAL_URL", "http://127.0.0.1:8000")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "")  # 必須跟 finflow-queue.service 裡的值一致

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # 對應 setWebhook 的 secret_token

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")  # Developer Portal -> General Information（/ask 那個 bot）
DISCORD_ADMIN_PUBLIC_KEY = os.environ.get("DISCORD_ADMIN_PUBLIC_KEY", "")  # 節點群控用的獨立 bot
# 註：DISCORD_BOT_TOKEN／DISCORD_ADMIN_BOT_TOKEN 只有註冊 Slash Command 的
# 一次性腳本（register_discord_commands.py）需要，本服務執行期間不需要
# bot token，因為 followup 訊息編輯用的是 interaction token，不是 bot token。

GATEWAY_DB_PATH = os.environ.get("GATEWAY_DB_PATH", "bot_gateway.db")
JOB_WAIT_TIMEOUT_SEC = int(os.environ.get("JOB_WAIT_TIMEOUT_SEC", "900"))  # 背景等待任務完成的總預算，預設 15 分鐘
JOB_POLL_INTERVAL_SEC = 2.0
HISTORY_MAX_MESSAGES = int(os.environ.get("HISTORY_MAX_MESSAGES", "20"))  # 簡單截斷用，非 AI 摘要
TELEGRAM_MAX_CHARS = 4000
LINE_MAX_CHARS = 4900
DISCORD_MAX_CHARS = 1900  # Discord 訊息上限 2000 字元，留一點緩衝空間

# Lightning 閒置監控：多久沒收到心跳就視為閒置、主動呼叫 Studio.stop()
LIGHTNING_IDLE_TIMEOUT_SEC = int(os.environ.get("LIGHTNING_IDLE_TIMEOUT_SEC", "1800"))
LIGHTNING_IDLE_CHECK_INTERVAL_SEC = int(os.environ.get("LIGHTNING_IDLE_CHECK_INTERVAL_SEC", "60"))

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


# ---------- Discord ----------

def verify_discord_signature(body: bytes, signature: Optional[str], timestamp: Optional[str], public_key: str) -> bool:
    """Discord 用 Ed25519 簽章驗證（不是 HMAC）。public_key 由呼叫端指定
    是 DISCORD_PUBLIC_KEY 還是 DISCORD_ADMIN_PUBLIC_KEY——兩個 bot 各自的
    簽章只能用各自的公鑰驗證，不可以互相通用。"""
    if not signature or not timestamp or not public_key:
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


async def discord_edit_original(application_id: str, interaction_token: str, text: str):
    """把最終結果編輯回 deferred 的那則「思考中」訊息。interaction token
    的 followup 編輯有效期是 15 分鐘，對應 JOB_WAIT_TIMEOUT_SEC 預設值。"""
    url = f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.patch(url, json={"content": _truncate(text, DISCORD_MAX_CHARS)})
            if resp.status_code >= 400:
                log.error("Discord followup 編輯失敗：%s %s", resp.status_code, resp.text)
        except Exception:
            log.exception("Discord followup 編輯失敗")


async def process_discord_interaction(user_id: str, prompt: str, application_id: str, interaction_token: str):
    session_key = f"discord:{user_id}"
    history = get_history(session_key)
    history.append({"role": "user", "content": prompt})

    result = await submit_and_wait_via_queue(history)

    if result["ok"]:
        history.append({"role": "assistant", "content": result["result"]})
        save_history(session_key, history)
        await discord_edit_original(application_id, interaction_token, result["result"])
    else:
        await discord_edit_original(application_id, interaction_token, f"⚠️ 處理失敗：{result['error']}")


@app.post("/discord/interactions")
async def discord_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_signature_ed25519: Optional[str] = Header(None),
    x_signature_timestamp: Optional[str] = Header(None),
):
    raw_body = await request.body()
    if not verify_discord_signature(raw_body, x_signature_ed25519, x_signature_timestamp, DISCORD_PUBLIC_KEY):
        # Discord 要求驗證失敗一律回 401，這也是 Discord Developer Portal
        # 拿來檢查你的 Interactions Endpoint URL 是否正確設定的依據
        raise HTTPException(status_code=401, detail="Invalid Discord request signature")

    interaction = json.loads(raw_body)
    itype = interaction.get("type")

    # type=1：Discord 官方定期／設定時發送的健康檢查，必須原樣回 PONG（type=1）
    if itype == 1:
        return {"type": 1}

    # type=2：使用者實際觸發了 Slash Command
    if itype == 2:
        interaction_id = interaction.get("id")
        if interaction_id and not _dedup_check_and_mark(f"discord:{interaction_id}"):
            return {"type": 5}  # 重複重送，仍需回應合法格式，但不重新處理

        data = interaction.get("data", {})
        options = data.get("options", [])
        prompt = next((o.get("value") for o in options if o.get("name") == "prompt"), None)

        user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
        user_id = user.get("id")
        application_id = interaction.get("application_id")
        interaction_token = interaction.get("token")

        if not prompt or not user_id or not application_id or not interaction_token:
            return {
                "type": 4,
                "data": {"content": "⚠️ 缺少 prompt 參數或無法解析使用者資訊。", "flags": 64},
            }

        # 3 秒內必須回應，實際任務丟到背景處理，用 deferred 回應讓 Discord
        # 顯示「思考中…」，之後再用 PATCH 把真正答案編輯回去
        background_tasks.add_task(
            process_discord_interaction, user_id, prompt, application_id, interaction_token
        )
        return {"type": 5}

    # 其他 interaction 類型（按鈕、autocomplete、modal 等）本輪不支援
    return {"type": 4, "data": {"content": "尚未支援此類型的互動。", "flags": 64}}


# ---------- 管理端節點群控（獨立的 Discord Admin Bot） ----------

async def _set_oracle_stop_flag(node_id: str) -> bool:
    """呼叫 server.py 的 POST /nodes/{id}/stop，標記「請這個節點自己盡快
    停止」。這個呼叫對所有平台都會做（不只 Kaggle）：對 Kaggle 來說這是
    唯一能達成停止效果的方式；對 Lightning 來說是官方 Studio.stop() 之外
    的備援保險，即使 Lightning API 呼叫失敗，節點自己下一輪輪詢也還是
    會看到這個信號、自行結束 worker_loop（雖然不會真的關閉 Studio，
    但至少不會繼續耗費運算資源去接任務）。"""
    headers = {"x-api-key": CLIENT_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{ORACLE_INTERNAL_URL}/nodes/{node_id}/stop", headers=headers)
            return resp.status_code == 200
        except Exception:
            log.exception("呼叫 Oracle 停止信號端點失敗")
            return False


async def _set_oracle_load_flag(node_id: str) -> bool:
    """呼叫 server.py 的 POST /nodes/{id}/load，標記「可以開始佈署了」。
    這是 Kaggle 兩階段啟動專用的機制（見 kernel_script.py.template 的
    說明）：/start-node 只讓節點開機、回報 GPU 硬體資訊，停在原地等這個
    信號才會真正下載模型。對不支援兩階段啟動的平台（目前是 Lightning），
    呼叫這支端點不會造成任何影響——反正沒有東西在輪詢這個旗標，單純是
    server.py 資料庫裡多一筆沒人讀的紀錄，不影響 Lightning 節點本身。"""
    headers = {"x-api-key": CLIENT_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{ORACLE_INTERNAL_URL}/nodes/{node_id}/load", headers=headers)
            return resp.status_code == 200
        except Exception:
            log.exception("呼叫 Oracle 載入信號端點失敗")
            return False


async def _fetch_live_nodes() -> Optional[List[Dict[str, Any]]]:
    headers = {"x-api-key": CLIENT_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{ORACLE_INTERNAL_URL}/nodes", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            log.exception("查詢 /nodes 失敗")
            return None


def _run_blocking(fn, *args):
    """把同步（阻塞）的 controller 呼叫丟進獨立執行緒池，回傳一個
    asyncio 可以 await 的 Future，避免卡住事件迴圈。"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_BLOCKING_CALL_POOL, fn, *args)


async def process_start_node(node_id: str, application_id: str, interaction_token: str):
    config = node_controllers.get_node_config(node_id)
    if not config:
        await discord_edit_original(application_id, interaction_token,
                                     f"⚠️ 找不到節點 `{node_id}`，請確認 NODE_PLATFORM_MAP 裡有登記。")
        return

    platform = config.get("platform")
    try:
        controller = node_controllers.get_controller(platform)
    except KeyError as e:
        await discord_edit_original(application_id, interaction_token, f"⚠️ {e}")
        return

    # 把 Oracle 端本來就有的連線資訊、節點各自的模型設定一併傳給 controller，
    # 這樣 Kaggle/Lightning 那邊產生的 edge.conf 才會跟這個節點該用的設定一致。
    # NODE_API_KEY 直接沿用 server.py 也在用的同一份 NODE_API_KEYS_JSON，
    # 不要求使用者為了群控功能又重複登記一次金鑰。
    try:
        keys_map = json.loads(os.environ.get("NODE_API_KEYS_JSON", "{}"))
    except json.JSONDecodeError:
        keys_map = {}

    # DOMAIN_NAME 優先於 ORACLE_PUBLIC_IP：如果照 DEPLOY.md Step 4-A「⑤」的
    # 建議收緊了 OCI Security List（只允許 Cloudflare 的 IP 段連進
    # 443），Kaggle/Colab/Lightning 這些外部節點對 ORACLE_PUBLIC_IP 直連
    # 會被防火牆直接擋掉（連 TLS 握手都到不了），節點端的心跳／輪詢/停止
    # 信號全部靜默失敗，症狀是「看起來啟動成功，但永遠查不到硬體規格、
    # 也永遠關不掉」——因為這兩件事都要靠節點成功連回 Oracle 才做得到。
    # 有設定 DOMAIN_NAME 就一定要用它（會走 Cloudflare，不受那條防火牆
    # 規則影響），沒設定才退回直連 IP。
    domain_name = os.environ.get("DOMAIN_NAME", "").strip()
    oracle_public_ip = os.environ.get("ORACLE_PUBLIC_IP", "").strip()
    oracle_public_host = domain_name or oracle_public_ip
    full_config = {
        **config,
        "oracle_url": f"https://{oracle_public_host}" if oracle_public_host else "",
        "node_api_key": keys_map.get(node_id, ""),
    }

    if not full_config["oracle_url"]:
        await discord_edit_original(application_id, interaction_token,
                                     "⚠️ finflow-queue.env 裡的 DOMAIN_NAME、ORACLE_PUBLIC_IP "
                                     "兩個都是空的，無法組出邊緣節點要連線的網址，"
                                     "請先確認至少一個有填。")
        return
    if not full_config["node_api_key"]:
        await discord_edit_original(application_id, interaction_token,
                                     f"⚠️ 找不到節點 `{node_id}` 的 NODE_API_KEY，"
                                     f"請確認 NODE_API_KEYS_JSON 裡有登記這個節點。")
        return

    result = await _run_blocking(controller.start, node_id, full_config)
    prefix = "✅" if result.ok else "❌"
    msg = f"{prefix} {result.message}"
    if result.ok and platform == "kaggle":
        msg += ("\n\n這是 Kaggle 的兩階段啟動：節點現在只會開機、偵測這次分配到的 GPU 型號/"
                "張數，還**不會**下載模型。等一下用 `/list-nodes` 確認 GPU 規格，滿意的話"
                f"再用 `/load-node node_id:{node_id}` 觸發真正的部署；如果分配到的張數不夠，"
                f"直接 `/stop-node node_id:{node_id}` 後重新 `/start-node` 即可重試"
                "（提醒：每次重試本身也會消耗一些 Kaggle GPU 週配額，不是完全免費的操作）。")
    await discord_edit_original(application_id, interaction_token, msg)


async def process_load_node(node_id: str, application_id: str, interaction_token: str):
    config = node_controllers.get_node_config(node_id)
    if not config:
        await discord_edit_original(application_id, interaction_token,
                                     f"⚠️ 找不到節點 `{node_id}`，請確認 NODE_PLATFORM_MAP 裡有登記。")
        return

    platform = config.get("platform")
    if platform != "kaggle":
        await discord_edit_original(
            application_id, interaction_token,
            f"ℹ️ `{node_id}`（{platform}）不使用兩階段啟動，`/start-node` 已經完整部署好了，"
            f"不需要再下 `/load-node`。",
        )
        return

    ok = await _set_oracle_load_flag(node_id)
    if ok:
        await discord_edit_original(
            application_id, interaction_token,
            f"✅ 已送出載入信號給 `{node_id}`，節點下一次輪詢（通常 5 秒內）就會開始下載模型、"
            f"啟動 Ollama，過程可能需要幾分鐘，完成後用 `/list-nodes` 確認狀態轉為運作中。",
        )
    else:
        await discord_edit_original(
            application_id, interaction_token,
            f"❌ 呼叫 Oracle 載入信號端點失敗，`{node_id}` 會持續停在階段一等待，可以稍後重試這個指令。",
        )


async def process_stop_node(node_id: str, application_id: str, interaction_token: str):
    config = node_controllers.get_node_config(node_id)
    if not config:
        await discord_edit_original(application_id, interaction_token,
                                     f"⚠️ 找不到節點 `{node_id}`，請確認 NODE_PLATFORM_MAP 裡有登記。")
        return

    platform = config.get("platform")

    # 一律先設 Oracle 停止旗標——這是唯一對 Kaggle 有效的機制，對 Lightning
    # 則是官方 API 之外的備援，見上面 _set_oracle_stop_flag() 的說明
    flag_ok = await _set_oracle_stop_flag(node_id)

    try:
        controller = node_controllers.get_controller(platform)
        result = await _run_blocking(controller.stop, node_id, config)
        prefix = "✅" if result.ok else "❌"
        msg = f"{prefix} {result.message}"
    except KeyError as e:
        msg = f"⚠️ {e}（已送出 Oracle 停止信號，節點仍會在下次輪詢時自行結束）"

    if not flag_ok:
        msg += "\n⚠️ 附註：呼叫 Oracle 停止信號端點失敗，若平台本身也沒有即時停止能力，節點可能不會如預期停止。"

    await discord_edit_original(application_id, interaction_token, msg)


async def process_list_nodes(application_id: str, interaction_token: str):
    configured = node_controllers.list_configured_nodes()
    live = await _fetch_live_nodes()
    live_by_id = {n["node_id"]: n for n in live} if live is not None else {}

    if not configured and not live_by_id:
        await discord_edit_original(application_id, interaction_token, "目前沒有任何已設定或已上線的節點。")
        return

    lines = []
    all_ids = sorted(set(configured.keys()) | set(live_by_id.keys()))
    for node_id in all_ids:
        platform = configured.get(node_id, {}).get("platform", "?")
        live_info = live_by_id.get(node_id)

        if live_info is None:
            lines.append(f"`{node_id}`（{platform}）：從未上線")
            continue

        cap = live_info.get("capability", {})
        gpu_name = cap.get("gpu_name")
        gpu_count = cap.get("gpu_count")
        gpu_desc = f"{gpu_name} x{gpu_count}" if gpu_name else "GPU 資訊未知"
        model = cap.get("model")

        if not live_info["alive"]:
            # 離線也一併顯示最後一次心跳回報的 GPU 資訊（不是「現在還占用」，
            # 是「上次活著的時候拿到的規格」，方便判斷值不值得重開）；
            # gpu_name 為 None 時（例如從未成功偵測到 GPU）才顯示「GPU 資訊未知」
            last_known = f"，最後已知 {gpu_desc}" if gpu_name else ""
            status_line = f"🔴 離線（曾經上線過{last_known}）"
        elif live_info.get("status") == "booting":
            status_line = f"🟡 開機中，等待 /load-node（{gpu_desc}）"
        else:
            uptime = ""
            if live_info.get("started_at"):
                uptime_sec = time.time() - live_info["started_at"]
                uptime = f"，已運作 {round(uptime_sec / 60)} 分鐘"
            status_line = f"🟢 運作中（模型：{model or '?'}，{gpu_desc}{uptime}）"

        lines.append(f"`{node_id}`（{platform}）：{status_line}")

    if live is None:
        lines.append("\n⚠️ 查詢即時心跳狀態失敗，以上只顯示已設定的節點清單，實際狀態未知。")

    await discord_edit_original(application_id, interaction_token, "\n".join(lines))


@app.post("/discord/admin-interactions")
async def discord_admin_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_signature_ed25519: Optional[str] = Header(None),
    x_signature_timestamp: Optional[str] = Header(None),
):
    """節點群控專用的獨立端點，用另一把 DISCORD_ADMIN_PUBLIC_KEY 驗證，
    架構原因見檔頭 docstring「節點群控架構說明」。"""
    raw_body = await request.body()
    if not verify_discord_signature(raw_body, x_signature_ed25519, x_signature_timestamp, DISCORD_ADMIN_PUBLIC_KEY):
        raise HTTPException(status_code=401, detail="Invalid Discord request signature")

    interaction = json.loads(raw_body)
    itype = interaction.get("type")

    if itype == 1:
        return {"type": 1}

    if itype == 2:
        interaction_id = interaction.get("id")
        if interaction_id and not _dedup_check_and_mark(f"discord-admin:{interaction_id}"):
            return {"type": 5}

        data = interaction.get("data", {})
        command_name = data.get("name")
        options = {o["name"]: o.get("value") for o in data.get("options", [])}

        application_id = interaction.get("application_id")
        interaction_token = interaction.get("token")
        if not application_id or not interaction_token:
            return {"type": 4, "data": {"content": "⚠️ 無法解析請求。", "flags": 64}}

        if command_name == "start-node":
            node_id = options.get("node_id")
            if not node_id:
                return {"type": 4, "data": {"content": "⚠️ 缺少 node_id 參數。", "flags": 64}}
            background_tasks.add_task(process_start_node, node_id, application_id, interaction_token)
            return {"type": 5}

        if command_name == "stop-node":
            node_id = options.get("node_id")
            if not node_id:
                return {"type": 4, "data": {"content": "⚠️ 缺少 node_id 參數。", "flags": 64}}
            background_tasks.add_task(process_stop_node, node_id, application_id, interaction_token)
            return {"type": 5}

        if command_name == "load-node":
            node_id = options.get("node_id")
            if not node_id:
                return {"type": 4, "data": {"content": "⚠️ 缺少 node_id 參數。", "flags": 64}}
            background_tasks.add_task(process_load_node, node_id, application_id, interaction_token)
            return {"type": 5}

        if command_name == "list-nodes":
            background_tasks.add_task(process_list_nodes, application_id, interaction_token)
            return {"type": 5}

        return {"type": 4, "data": {"content": f"⚠️ 未知指令：{command_name}", "flags": 64}}

    return {"type": 4, "data": {"content": "尚未支援此類型的互動。", "flags": 64}}


# ---------- Lightning 閒置監控（獨立背景執行緒，非 asyncio） ----------

_lightning_stop_cooldown: Dict[str, float] = {}
_LIGHTNING_STOP_COOLDOWN_SEC = 600  # 避免同一個節點在短時間內被重複嘗試關閉


def _lightning_idle_checker_loop():
    """獨立 OS 執行緒（不是 asyncio task），跟 server.py 的
    _background_loop() 是同一種模式：即使這裡呼叫 Lightning API 卡住，
    也不會拖累 FastAPI 事件迴圈處理其他請求。"""
    import requests as _requests  # 用同步的 requests，不跟 httpx.AsyncClient 混用

    while True:
        try:
            time.sleep(LIGHTNING_IDLE_CHECK_INTERVAL_SEC)

            headers = {"x-api-key": CLIENT_API_KEY, "Content-Type": "application/json"}
            resp = _requests.get(f"{ORACLE_INTERNAL_URL}/nodes", headers=headers, timeout=10)
            resp.raise_for_status()
            nodes = resp.json()

            configured = node_controllers.list_configured_nodes()
            now = time.time()

            for n in nodes:
                node_id = n["node_id"]
                cfg = configured.get(node_id)
                if not cfg or cfg.get("platform") != "lightning":
                    continue
                if n["alive"]:
                    continue  # last_heartbeat 還在 NODE_DEAD_AFTER_SEC 門檻內，不算閒置

                last_attempt = _lightning_stop_cooldown.get(node_id, 0)
                if now - last_attempt < _LIGHTNING_STOP_COOLDOWN_SEC:
                    continue  # 冷卻中，避免對同一個節點重複狂打 Lightning API

                idle_sec = now - n["last_heartbeat"]
                if idle_sec < LIGHTNING_IDLE_TIMEOUT_SEC:
                    continue

                log.info(f"Lightning 節點 {node_id} 已閒置 {round(idle_sec)} 秒，嘗試主動關閉 Studio...")
                _lightning_stop_cooldown[node_id] = now
                try:
                    controller = node_controllers.get_controller("lightning")
                    result = controller.stop(node_id, cfg)
                    if result.ok:
                        log.info(f"Lightning 節點 {node_id} 已透過閒置監控關閉：{result.message}")
                    else:
                        log.warning(f"Lightning 節點 {node_id} 閒置監控嘗試關閉失敗：{result.message}")
                except Exception:
                    log.exception(f"Lightning 閒置監控處理節點 {node_id} 時發生例外")

        except Exception:
            log.exception("Lightning 閒置監控迴圈發生例外，60 秒後繼續")
            time.sleep(60)


threading.Thread(target=_lightning_idle_checker_loop, daemon=True).start()


# ---------- 健康檢查 ----------

@app.get("/healthz")
def healthz():
    return {"status": "ok", "ts": time.time()}
