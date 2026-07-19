"""
向 Discord 官方 API 註冊（或更新）本專案使用的 Slash Command。

跟 bot_gateway.py 是分開的——bot_gateway.py 執行期間只需要驗證/回覆 interaction，
不需要 bot token；但「有哪些指令、指令長怎樣」這件事本身要透過 Discord API
主動註冊，註冊完 Discord 才會在使用者的輸入框顯示這個指令。

不是只能執行一次：往後想調整指令內容，直接改下面對應的 COMMAND_SETS 再重新
執行即可，Discord 會用同名指令覆蓋舊定義，不會重複建立。

**這支腳本現在管兩組指令，分屬兩個不同的 Discord Application（兩個 bot）**：
    --set ask    /ask（一般使用者問答用，見 bot_gateway.py 的 /discord/interactions）
    --set admin  /start-node、/stop-node、/list-nodes（節點群控，見
                 /discord/admin-interactions，管理指令額外設定
                 default_member_permissions 限制只有 Manage Server 權限的人
                 看得到，避免一般成員誤觸）
兩組指令要註冊到**不同的 Discord Application**（各自的 Developer Portal 頁面
建立獨立的 bot），所以這支腳本需要對每一組各執行一次，各自的憑證用不同的
環境變數名稱區分，不會互相覆蓋：
    --set ask   讀 DISCORD_BOT_TOKEN       / DISCORD_APPLICATION_ID
    --set admin 讀 DISCORD_ADMIN_BOT_TOKEN / DISCORD_ADMIN_APPLICATION_ID

使用方式（兩種擇一，效果相同）：
    方式一：手動 export 環境變數
        export DISCORD_BOT_TOKEN="<ask bot 的 Bot Token>"
        export DISCORD_APPLICATION_ID="<ask bot 的 Application ID>"
        export DISCORD_ADMIN_BOT_TOKEN="<admin bot 的 Bot Token>"
        export DISCORD_ADMIN_APPLICATION_ID="<admin bot 的 Application ID>"
        python3 register_discord_commands.py --set ask
        python3 register_discord_commands.py --set admin

    方式二（推薦，不用擔心忘記 export）：把值寫進 discord-admin.env，跟本檔案放在
    同一個資料夾，腳本會自動讀取（若同名環境變數已經存在，環境變數優先，
    不會被檔案內容覆蓋）：
        # discord-admin.env 內容：
        DISCORD_BOT_TOKEN=<ask bot 的 Bot Token>
        DISCORD_APPLICATION_ID=<ask bot 的 Application ID>
        DISCORD_ADMIN_BOT_TOKEN=<admin bot 的 Bot Token>
        DISCORD_ADMIN_APPLICATION_ID=<admin bot 的 Application ID>

        python3 register_discord_commands.py --set ask
        python3 register_discord_commands.py --set admin

    也可以用 --env-file 指定其他路徑：
        python3 register_discord_commands.py --set admin --env-file /path/to/other.env

Global 與 Guild 的差異（決定要不要填 DISCORD_GUILD_ID）：
    - Global Command（GUILD_ID 留空，預設）：註冊一次之後，Bot 之後被邀請加入
      任何新的 Discord 伺服器，這個指令會自動出現，不需要每個新伺服器都重新
      註冊一次。缺點是變更後最多可能要等 1 小時才會在客戶端生效。
    - Guild Command（設定 DISCORD_GUILD_ID）：只在指定的單一伺服器生效，但
      改動立即生效，適合開發階段在自己的測試伺服器快速迭代。管理指令建議
      至少在測試階段用 Guild Command，確定沒問題再考慮要不要轉 Global。

「只想讓特定頻道看得到這個指令」不是這支腳本管的範圍——那是 Discord 伺服器本身
「整合權限」設定裡的頻道層級限制（伺服器設定 → 整合 → 你的 App），這支腳本
只負責「這個指令存不存在、誰有權限用」，不負責「哪個頻道看得到」。

node_id 參數的下拉選單（選填，僅 --set admin 有效）：
    如果執行這支腳本時，環境變數裡也讀得到 NODE_PLATFORM_MAP，/start-node、
    /stop-node 的 node_id 參數會自動變成下拉選單而不是要你手動打字，手機上
    操作更方便。除了 discord-admin.env 本身，這支腳本也會嘗試從同一個 repo
    根目錄的 finflow-queue.env 讀取 NODE_PLATFORM_MAP（該值本來就要跟 Oracle
    端維持一致，不需要在 discord-admin.env 裡重複填一次；discord-admin.env
    裡若也有寫，則以 discord-admin.env 優先）。沒有讀到也沒關係，只是退回
    自由輸入文字。Discord 的下拉選項上限是 25 個，超過就自動退回自由輸入
    （並印出警告）。

使用方式：
    python3 register_discord_commands.py --set ask         # 註冊/更新一般問答指令
    python3 register_discord_commands.py --set admin       # 註冊/更新節點群控指令
    python3 register_discord_commands.py --set ask --list  # 查看目前已註冊的指令
    python3 register_discord_commands.py --set ask --delete ask  # 刪除指定名稱的指令
"""

import os
import sys
import stat
import json
import argparse
import httpx


def load_env_file(path: str):
    """讀取簡單的 KEY=VALUE 格式檔案，設定進 os.environ。
    只在該 KEY 尚未存在於環境變數時才設定（os.environ.setdefault），
    所以手動 export 過的值永遠優先，這個檔案只是「沒手動設定時的備援」。
    也接受每行前面有 export 前綴（跟直接 source 這個檔案的寫法相容）。"""
    if not os.path.isfile(path):
        return
    try:
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"警告：{path} 的權限對群組/其他使用者開放，裡面存放 Bot Token，"
                  f"建議執行 chmod 600 {path}", file=sys.stderr)
    except OSError:
        pass

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def _default_env_file() -> str:
    """預設去 script 所在的資料夾找 discord-admin.env（跟 bot-gateway/ 底下
    其他程式放在一起，不用額外指定路徑）"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord-admin.env")


def _fallback_platform_map_file() -> str:
    """NODE_PLATFORM_MAP 本來就要跟 Oracle 端一致，順手從 repo 根目錄的
    finflow-queue.env 讀，避免在 discord-admin.env 裡重複維護一份。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "finflow-queue.env")


# 提早解析 --env-file（在讀取 DISCORD_BOT_TOKEN 等全域變數之前），
# 這樣 load_env_file() 補進 os.environ 的值才來得及被下面的 os.environ.get() 讀到。
# 用 parse_known_args 避免跟 main() 裡其他參數的完整 parser 定義衝突。
_pre_parser = argparse.ArgumentParser(add_help=False)
_pre_parser.add_argument("--env-file", default=_default_env_file())
_pre_args, _ = _pre_parser.parse_known_args()
load_env_file(_pre_args.env_file)
load_env_file(_fallback_platform_map_file())  # 只會補上 discord-admin.env 沒設定的 key

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")
DISCORD_ADMIN_BOT_TOKEN = os.environ.get("DISCORD_ADMIN_BOT_TOKEN", "")
DISCORD_ADMIN_APPLICATION_ID = os.environ.get("DISCORD_ADMIN_APPLICATION_ID", "")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

# 每組指令各自對應的憑證，--set 決定要用哪一對
_CREDENTIALS = {
    "ask":   {"token": DISCORD_BOT_TOKEN,       "app_id": DISCORD_APPLICATION_ID},
    "admin": {"token": DISCORD_ADMIN_BOT_TOKEN, "app_id": DISCORD_ADMIN_APPLICATION_ID},
}

# MANAGE_GUILD 權限的 bitfield（Discord 官方定義），管理指令預設只有伺服器
# 管理員看得到、用得到，一般成員即使在同一個頻道也看不到這些指令
_MANAGE_GUILD_PERMISSION = "32"


def _node_id_choices():
    """從 NODE_PLATFORM_MAP 動態產生 node_id 的下拉選項，見檔頭說明。"""
    raw = os.environ.get("NODE_PLATFORM_MAP", "")
    if not raw:
        return None
    try:
        node_map = json.loads(raw)
    except json.JSONDecodeError:
        print("警告：NODE_PLATFORM_MAP 不是合法 JSON，node_id 改用自由輸入", file=sys.stderr)
        return None
    if not node_map:
        return None
    if len(node_map) > 25:
        print("警告：NODE_PLATFORM_MAP 節點數超過 Discord 下拉選單上限（25），改用自由輸入", file=sys.stderr)
        return None
    return [{"name": f"{node_id}（{cfg.get('platform', '?')}）", "value": node_id} for node_id, cfg in node_map.items()]


def _node_id_option():
    choices = _node_id_choices()
    option = {
        "name": "node_id",
        "description": "節點 ID（例：kaggle-1、lightning-1）",
        "type": 3,  # STRING
        "required": True,
    }
    if choices:
        option["choices"] = choices
    return option


COMMAND_SETS = {
    "ask": [
        {
            "name": "ask",
            "description": "向 FinFlow 邊緣運算叢集提問，結果會用同一則訊息更新回覆",
            "type": 1,  # CHATINPUT
            "options": [
                {
                    "name": "prompt",
                    "description": "想問的內容",
                    "type": 3,  # STRING
                    "required": True,
                }
            ],
        },
        {
            "name": "history",
            "description": "查看你最近幾則 /ask 的問答紀錄",
            "type": 1,
            "options": [],
        },
    ],
    "admin": [
        {
            "name": "start-node",
            "description": "遠端啟動指定的邊緣運算節點（Kaggle／Lightning）",
            "type": 1,
            "default_member_permissions": _MANAGE_GUILD_PERMISSION,
            "options": [_node_id_option()],
        },
        {
            "name": "stop-node",
            "description": "遠端停止指定的邊緣運算節點",
            "type": 1,
            "default_member_permissions": _MANAGE_GUILD_PERMISSION,
            "options": [_node_id_option()],
        },
        {
            "name": "load-node",
            "description": "Kaggle 兩階段啟動專用：確認 GPU 分配滿意後，觸發真正的模型部署",
            "type": 1,
            "default_member_permissions": _MANAGE_GUILD_PERMISSION,
            "options": [_node_id_option()],
        },
        {
            "name": "list-nodes",
            "description": "列出所有已設定的節點與目前上線狀態",
            "type": 1,
            "default_member_permissions": _MANAGE_GUILD_PERMISSION,
            "options": [],
        },
    ],
}


def _base_url(app_id: str):
    if GUILD_ID:
        return f"https://discord.com/api/v10/applications/{app_id}/guilds/{GUILD_ID}/commands"
    return f"https://discord.com/api/v10/applications/{app_id}/commands"


def _scope_label():
    return f"Guild {GUILD_ID}（立即生效）" if GUILD_ID else "Global（可能需要等待最多 1 小時才會在客戶端顯示；Bot 加入的任何新伺服器會自動套用）"


def register(headers, app_id: str, commands):
    for payload in commands:
        resp = httpx.post(_base_url(app_id), headers=headers, json=payload, timeout=15.0)
        if resp.status_code in (200, 201):
            print(f"註冊/更新成功（{_scope_label()}）：/{payload['name']}")
        else:
            print(f"註冊失敗：/{payload['name']} → HTTP {resp.status_code}", file=sys.stderr)
            print(resp.text, file=sys.stderr)
            sys.exit(1)


def list_commands(headers, app_id: str):
    resp = httpx.get(_base_url(app_id), headers=headers, timeout=15.0)
    if resp.status_code == 200:
        for cmd in resp.json():
            print(f"- {cmd['name']}（id={cmd['id']}）：{cmd.get('description', '')}")
    else:
        print(f"查詢失敗：HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)


def delete_command(headers, app_id: str, name: str):
    resp = httpx.get(_base_url(app_id), headers=headers, timeout=15.0)
    resp.raise_for_status()
    target = next((c for c in resp.json() if c["name"] == name), None)
    if not target:
        print(f"找不到名稱為 '{name}' 的指令", file=sys.stderr)
        sys.exit(1)
    del_resp = httpx.delete(f"{_base_url(app_id)}/{target['id']}", headers=headers, timeout=15.0)
    if del_resp.status_code == 204:
        print(f"已刪除指令 '{name}'（{_scope_label()}）")
    else:
        print(f"刪除失敗：HTTP {del_resp.status_code}", file=sys.stderr)
        print(del_resp.text, file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=list(COMMAND_SETS.keys()), required=True,
                         help="要註冊哪一組指令：ask（一般問答 bot）或 admin（節點群控 bot），"
                              "決定要用哪一對憑證（見檔頭說明）")
    parser.add_argument("--list", action="store_true", help="列出目前已註冊的指令")
    parser.add_argument("--delete", metavar="NAME", help="刪除指定名稱的指令")
    parser.add_argument("--env-file", default=_default_env_file(),
                         help="機敏設定檔路徑，預設跟本檔案放在同一資料夾的 discord-admin.env")
    args = parser.parse_args()

    creds = _CREDENTIALS[args.set]
    token, app_id = creds["token"], creds["app_id"]
    if not token or not app_id:
        env_key_token = "DISCORD_BOT_TOKEN" if args.set == "ask" else "DISCORD_ADMIN_BOT_TOKEN"
        env_key_app = "DISCORD_APPLICATION_ID" if args.set == "ask" else "DISCORD_ADMIN_APPLICATION_ID"
        print(f"請先設定環境變數 {env_key_token} 與 {env_key_app}，"
              f"或是把它們寫進以下這個檔案（每行 KEY=VALUE，可以不用加 export）：",
              file=sys.stderr)
        print(f"  {args.env_file}", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    if args.list:
        list_commands(headers, app_id)
    elif args.delete:
        delete_command(headers, app_id, args.delete)
    else:
        register(headers, app_id, COMMAND_SETS[args.set])


if __name__ == "__main__":
    main()
