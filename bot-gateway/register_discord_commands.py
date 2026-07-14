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
建立獨立的 bot），所以這支腳本需要對每一組各執行一次，分別帶那個 Application
自己的 DISCORD_BOT_TOKEN／DISCORD_APPLICATION_ID：

    export DISCORD_BOT_TOKEN="<ask bot 的 Bot Token>"
    export DISCORD_APPLICATION_ID="<ask bot 的 Application ID>"
    python3 register_discord_commands.py --set ask

    export DISCORD_BOT_TOKEN="<admin bot 的 Bot Token>"
    export DISCORD_APPLICATION_ID="<admin bot 的 Application ID>"
    python3 register_discord_commands.py --set admin

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
    如果執行這支腳本時，環境變數裡也讀得到 NODE_PLATFORM_MAP（跟
    finflow-queue.env 裡的格式一樣），/start-node、/stop-node 的 node_id
    參數會自動變成下拉選單而不是要你手動打字，手機上操作更方便。沒有設定
    這個環境變數也沒關係，只是退回自由輸入文字。Discord 的下拉選項上限是
    25 個，超過就自動退回自由輸入（並印出警告）。

使用方式：
    export DISCORD_BOT_TOKEN="<Bot Token>"          # Developer Portal -> Bot -> Token
    export DISCORD_APPLICATION_ID="<Application ID>" # Developer Portal -> General Information
    python3 register_discord_commands.py --set ask       # 註冊/更新一般問答指令
    python3 register_discord_commands.py --set admin      # 註冊/更新節點群控指令
    python3 register_discord_commands.py --set ask --list # 查看目前已註冊的指令
    python3 register_discord_commands.py --set ask --delete ask  # 刪除指定名稱的指令
"""

import os
import sys
import json
import argparse
import httpx

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

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
            "name": "list-nodes",
            "description": "列出所有已設定的節點與目前上線狀態",
            "type": 1,
            "default_member_permissions": _MANAGE_GUILD_PERMISSION,
            "options": [],
        },
    ],
}


def _base_url():
    if GUILD_ID:
        return f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/guilds/{GUILD_ID}/commands"
    return f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/commands"


def _scope_label():
    return f"Guild {GUILD_ID}（立即生效）" if GUILD_ID else "Global（可能需要等待最多 1 小時才會在客戶端顯示；Bot 加入的任何新伺服器會自動套用）"


def register(headers, commands):
    for payload in commands:
        resp = httpx.post(_base_url(), headers=headers, json=payload, timeout=15.0)
        if resp.status_code in (200, 201):
            print(f"註冊/更新成功（{_scope_label()}）：/{payload['name']}")
        else:
            print(f"註冊失敗：/{payload['name']} → HTTP {resp.status_code}", file=sys.stderr)
            print(resp.text, file=sys.stderr)
            sys.exit(1)


def list_commands(headers):
    resp = httpx.get(_base_url(), headers=headers, timeout=15.0)
    if resp.status_code == 200:
        for cmd in resp.json():
            print(f"- {cmd['name']}（id={cmd['id']}）：{cmd.get('description', '')}")
    else:
        print(f"查詢失敗：HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)


def delete_command(headers, name: str):
    resp = httpx.get(_base_url(), headers=headers, timeout=15.0)
    resp.raise_for_status()
    target = next((c for c in resp.json() if c["name"] == name), None)
    if not target:
        print(f"找不到名稱為 '{name}' 的指令", file=sys.stderr)
        sys.exit(1)
    del_resp = httpx.delete(f"{_base_url()}/{target['id']}", headers=headers, timeout=15.0)
    if del_resp.status_code == 204:
        print(f"已刪除指令 '{name}'（{_scope_label()}）")
    else:
        print(f"刪除失敗：HTTP {del_resp.status_code}", file=sys.stderr)
        print(del_resp.text, file=sys.stderr)
        sys.exit(1)


def main():
    if not DISCORD_BOT_TOKEN or not DISCORD_APPLICATION_ID:
        print("請先設定環境變數 DISCORD_BOT_TOKEN 與 DISCORD_APPLICATION_ID", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=list(COMMAND_SETS.keys()), required=True,
                         help="要註冊哪一組指令：ask（一般問答 bot）或 admin（節點群控 bot）")
    parser.add_argument("--list", action="store_true", help="列出目前已註冊的指令")
    parser.add_argument("--delete", metavar="NAME", help="刪除指定名稱的指令")
    args = parser.parse_args()

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    if args.list:
        list_commands(headers)
    elif args.delete:
        delete_command(headers, args.delete)
    else:
        register(headers, COMMAND_SETS[args.set])


if __name__ == "__main__":
    main()
