"""
向 Discord 官方 API 註冊（或更新）本專案使用的 Slash Command（/ask）。

跟 bot_gateway.py 是分開的——bot_gateway.py 執行期間只需要驗證/回覆 interaction，
不需要 bot token；但「有哪些指令、指令長怎樣」這件事本身要透過 Discord API
主動註冊，註冊完 Discord 才會在使用者的輸入框顯示這個指令。

不是只能執行一次：往後想調整指令內容（例如幫 /ask 加新參數），直接改下面的
COMMAND_PAYLOAD 再重新執行即可，Discord 會用同名指令覆蓋舊定義，不會重複建立。

Global 與 Guild 的差異（決定要不要填 DISCORD_GUILD_ID）：
    - Global Command（GUILD_ID 留空，預設）：註冊一次之後，Bot 之後被邀請加入
      任何新的 Discord 伺服器，這個指令會自動出現，不需要每個新伺服器都重新
      註冊一次。缺點是變更後最多可能要等 1 小時才會在客戶端生效。
    - Guild Command（設定 DISCORD_GUILD_ID）：只在指定的單一伺服器生效，但
      改動立即生效，適合開發階段在自己的測試伺服器快速迭代。

「只想讓特定頻道看得到這個指令」不是這支腳本管的範圍——那是 Discord 伺服器本身
「整合權限」設定裡的頻道層級限制（伺服器設定 → 整合 → 你的 App），這支腳本
只負責「這個指令存不存在」，不負責「哪個頻道看得到」。

使用方式：
    export DISCORD_BOT_TOKEN="<Bot Token>"          # Developer Portal -> Bot -> Token
    export DISCORD_APPLICATION_ID="<Application ID>" # Developer Portal -> General Information
    python3 register_discord_commands.py             # 註冊/更新
    python3 register_discord_commands.py --list       # 查看目前已註冊的指令
    python3 register_discord_commands.py --delete ask  # 刪除指定名稱的指令
"""

import os
import sys
import argparse
import httpx

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")
# 選填：若只想先在自己的測試伺服器立即生效（不用等 Global Command 的傳播延遲），
# 設定這個環境變數為你的 Server（Guild）ID；留空則註冊為 Global Command，
# 之後 Bot 加入的任何新伺服器都會自動套用，不需要重新執行這支腳本。
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

COMMAND_PAYLOAD = {
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
}


def _base_url():
    if GUILD_ID:
        return f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/guilds/{GUILD_ID}/commands"
    return f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/commands"


def _scope_label():
    return f"Guild {GUILD_ID}（立即生效）" if GUILD_ID else "Global（可能需要等待最多 1 小時才會在客戶端顯示；Bot 加入的任何新伺服器會自動套用）"


def register(headers):
    resp = httpx.post(_base_url(), headers=headers, json=COMMAND_PAYLOAD, timeout=15.0)
    if resp.status_code in (200, 201):
        print(f"註冊/更新成功（{_scope_label()}）：")
        print(resp.json())
    else:
        print(f"註冊失敗：HTTP {resp.status_code}", file=sys.stderr)
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
    parser.add_argument("--list", action="store_true", help="列出目前已註冊的指令")
    parser.add_argument("--delete", metavar="NAME", help="刪除指定名稱的指令")
    args = parser.parse_args()

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    if args.list:
        list_commands(headers)
    elif args.delete:
        delete_command(headers, args.delete)
    else:
        register(headers)


if __name__ == "__main__":
    main()
