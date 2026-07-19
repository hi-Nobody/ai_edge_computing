"""
所有平台 controller 的共用介面。

設計原則：
    bot_gateway.py（Discord 指令處理層）完全不應該知道 Kaggle、Lightning
    各自底層 API 長什麼樣子，只呼叫這裡定義的三個方法（start/stop/describe）。
    之後要加新平台（例如 Colab，如果哪天官方也開放類似 API），只需要照這個
    介面再寫一個新檔案，不用動 bot_gateway.py 裡任何一行。

    Kaggle 跟 Lightning 實際能做到的事情不對稱（Kaggle 沒有官方遠端停止
    API，Lightning 有），這個不對稱刻意透過 StartResult/StopResult 裡的
    `confirmed` 欄位呈現出來，而不是假裝兩個平台的 stop() 效果一樣：
        confirmed=True  → 平台官方 API 已經確認關閉／啟動成功
        confirmed=False → 已經盡力（例如送出遠端停止信號），但無法從外部
                           立即確認結果，需要靠節點自己配合、或等下一次
                           心跳/輪詢才會反映實際狀態
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# 所有節點的設定集中在同一份 edge-worker/edge.conf 裡，用 [node_id] 分區塊。
# 這是刻意設計成單一事實來源：不管是手動貼進 Kaggle/Lightning 網頁執行，還是
# 用 Discord /start-node 遠端觸發，讀的都是同一份檔案，不會有 VPS 端另外組一份
# 「應該」跟節點一致、卻容易忘記同步更新的複本。節點數變多時只需要在這一份
# 檔案裡加一個新區塊，不用每個節點各開一個檔案（那樣管理成本會隨節點數線性
#增加）；共用值（例如所有節點都指向同一個 Oracle 網域）可以只寫在檔案最上面
# 一次，個別節點需要不同時才在自己的區塊覆蓋。
NODE_CONF_PATH = Path(__file__).resolve().parents[2] / "edge-worker" / "edge.conf"

# 兩邊都用得到、不確定要不要留在檔案裡就給合理預設的欄位
_DEFAULTS = {
    "MODEL_NAME": "qwen2.5-coder:14b",
    "VERIFY_TLS": "false",
    "INFERENCE_TIMEOUT_SEC": "300",
    "IDLE_STOP_SEC": "1800",
}
_REQUIRED_KEYS = ("ORACLE_URL", "NODE_API_KEY")


class NodeConfError(Exception):
    """讀取／驗證 edge.conf 失敗時丟出，訊息設計成可以直接顯示給 Discord
    使用者看，不需要呼叫端另外包裝。"""


def parse_conf_sections(path: Path):
    """解析 edge.conf，回傳 (top_level, sections)：
      - top_level：第一個 [區塊] 出現之前的 KEY=VALUE，當作所有節點共用
        的預設值
      - sections：{node_id: {key: value, ...}}，每個區塊已經繼承了「這個
        區塊開始當下」累積到的 top_level 值，區塊內同名 KEY 會覆蓋掉繼承
        來的預設值

    這支函式跟 edge-worker/bootstrap.py 的 load_conf_sections() 是同一套
    格式規則的兩份獨立拷貝——那邊是節點自己（在 Kaggle/Lightning 上）讀
    這份檔案的邏輯，這邊是 Oracle 端讀的邏輯，兩邊執行環境完全獨立（不同的
    repo checkout、不同的機器），沒辦法共用同一份程式碼，改動格式規則時
    記得兩邊一起改。"""
    top_level, sections = {}, {}
    if not path.exists():
        return top_level, sections
    current = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections[current] = dict(top_level)  # 繼承目前為止的預設值
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if current is None:
            top_level[key] = value
        else:
            sections[current][key] = value
    return top_level, sections


def load_node_conf(node_id: str) -> dict:
    """載入並驗證 edge.conf 裡 [node_id] 這個區塊的設定，缺必填欄位、缺
    區塊、或整份檔案不存在時丟 NodeConfError（訊息可以直接顯示給使用者）。
    回傳的 dict 一定含有 NODE_ID／ORACLE_URL／NODE_API_KEY，其餘欄位缺的話
    套用 _DEFAULTS 補齊，並在 dict 裡多一個 "_warnings" 鍵列出「這次用了
    哪些欄位的預設值」，呼叫端可以決定要不要把這個警告顯示在啟動結果訊息裡。
    """
    if not NODE_CONF_PATH.exists():
        raise NodeConfError(
            f"找不到節點設定檔 `edge-worker/edge.conf`。"
            f"請參考 `edge-worker/edge.conf.example` 的格式建立這個檔案，"
            f"至少要有一個 `[{node_id}]` 區塊（記得裡面的 NODE_API_KEY 要跟 "
            f"Oracle 端 finflow-queue.env 的 NODE_API_KEYS_JSON 裡 `{node_id}` "
            f"對應的值一致）。"
        )

    _, sections = parse_conf_sections(NODE_CONF_PATH)

    if node_id not in sections:
        existing = ", ".join(sections.keys()) or "（目前檔案裡沒有任何區塊）"
        raise NodeConfError(
            f"`edge-worker/edge.conf` 裡找不到 `[{node_id}]` 這個區塊。"
            f"目前有的區塊：{existing}。請照 `edge-worker/edge.conf.example` "
            f"的格式新增一個 `[{node_id}]` 區塊。"
        )

    conf = dict(sections[node_id])

    missing_required = [k for k in _REQUIRED_KEYS if not conf.get(k)]
    if missing_required:
        raise NodeConfError(
            f"`edge-worker/edge.conf` 的 `[{node_id}]` 區塊缺少必填欄位（或沒有從"
            f"檔案最上面的共用預設值繼承到）：{', '.join(missing_required)}"
        )

    # NODE_ID 用區塊名稱本身決定，不需要（也不應該）在區塊內容裡重複寫一次
    # NODE_ID=xxx 讓它有機會跟區塊名稱兜不起來；這裡直接寫入，確保下游
    # （kaggle.py／lightning.py 組出的 EDGE_CONF_BODY）一定拿得到這個欄位。
    conf["NODE_ID"] = node_id

    warnings = []
    for key, default_value in _DEFAULTS.items():
        if not conf.get(key):
            conf[key] = default_value
            warnings.append(f"{key}（用了預設值 {default_value!r}，檔案裡沒有設定這個欄位）")
    conf["_warnings"] = warnings
    return conf


@dataclass
class StartResult:
    ok: bool
    confirmed: bool  # True＝平台官方 API 已確認開始啟動；False＝送出但無法立即確認
    message: str      # 給 Discord 使用者看的人類可讀訊息
    detail: Optional[str] = None  # 除錯用的額外資訊（例如底層錯誤訊息），不一定會顯示給使用者


@dataclass
class StopResult:
    ok: bool
    confirmed: bool  # True＝平台官方 API 已確認關閉；False＝只送出了信號，實際生效要等節點配合
    message: str
    detail: Optional[str] = None


class NodeController:
    """所有平台 controller 的基底類別，子類別至少要覆寫 start()、stop()。"""

    platform_name = "unknown"

    def start(self, node_id: str, node_config: dict) -> StartResult:
        raise NotImplementedError

    def stop(self, node_id: str, node_config: dict) -> StopResult:
        raise NotImplementedError
