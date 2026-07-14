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
from typing import Optional


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
