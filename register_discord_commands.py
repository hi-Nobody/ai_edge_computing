"""
一次性腳本：向 Discord 官方 API 註冊本專案使用的 Slash Command（/ask）。

跟 bot_gateway.py 是分開的——bot_gateway.py 執行期間只需要驗證/回覆 interaction，
不需要 bot token；但「有哪些指令、指令長怎樣」這件事本身要透過 Discord API
主動註冊一次，註冊完 Discord 才會在使用者的輸入框顯示這個指令。

使用方式：
    export DISCORD_BOT_TOKEN="<Bot Token>"          # Developer Portal -> Bot -> Token
    export DISCORD_APPLICATION_ID="<Application ID>" # Developer Portal -> General Information
    python3 register_discord_commands.py

註冊的是「Global Command」（所有伺服器都看得到，但 Discord 官方文件說最多可能
需要等到 1 小時才會在客戶端生效；若想立即在單一伺服器測試，可改用
Guild Command，見下方 GUILD_ID 的說明）。
"""

import os
import sys
import httpx

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")
# 選填：若只想先在自己的測試伺服器立即生效（不用等 Global Command 的傳播延遲），
# 設定這個環境變數為你的 Server（Guild）ID；留空則註冊為 Global Command。
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


def main():
    if not DISCORD_BOT_TOKEN or not DISCORD_APPLICATION_ID:
        print("請先設定環境變數 DISCORD_BOT_TOKEN 與 DISCORD_APPLICATION_ID", file=sys.stderr)
        sys.exit(1)

    if GUILD_ID:
        url = f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/guilds/{GUILD_ID}/commands"
        scope = f"Guild {GUILD_ID}（立即生效）"
    else:
        url = f"https://discord.com/api/v10/applications/{DISCORD_APPLICATION_ID}/commands"
        scope = "Global（可能需要等待最多 1 小時才會在客戶端顯示）"

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    resp = httpx.post(url, headers=headers, json=COMMAND_PAYLOAD, timeout=15.0)

    if resp.status_code in (200, 201):
        print(f"註冊成功（{scope}）：")
        print(resp.json())
    else:
        print(f"註冊失敗：HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
