"""
node_controllers 套件對外的唯一入口。

bot_gateway.py 只需要用這個模組提供的 get_controller() / get_node_config()
兩個函式，不需要（也不應該）直接 import kaggle.py／lightning.py。
"""

import os
import json
import logging

from .base import NodeController, StartResult, StopResult
from .kaggle import KaggleController
from .lightning import LightningController

log = logging.getLogger("node-controllers")

# node_id -> {"platform": "kaggle"/"lightning", ...平台各自需要的欄位}
# 範例：
#   {
#     "kaggle-1": {"platform": "kaggle", "kernel_slug": "finflow-edge-kaggle-1"},
#     "lightning-1": {"platform": "lightning", "studio_name": "finflow-edge-1", "teamspace": "my-teamspace"}
#   }
# 放在 finflow-queue.env 的 NODE_PLATFORM_MAP，理由：跟 NODE_API_KEYS_JSON
# 一樣是「常駐服務隨時需要讀取」的設定，不是一次性腳本才用得到的機密。
def load_platform_map() -> dict:
    raw = os.environ.get("NODE_PLATFORM_MAP", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("NODE_PLATFORM_MAP 不是合法的 JSON，節點群控功能將無法使用：%s", raw[:200])
        return {}


_CONTROLLERS = {
    "kaggle": KaggleController(),
    "lightning": LightningController(),
}


def get_controller(platform: str) -> NodeController:
    """回傳對應平台的 controller；平台名稱沒有對應的 controller 時丟
    KeyError，呼叫端（bot_gateway.py）自己決定怎麼回覆使用者比較好。"""
    if platform not in _CONTROLLERS:
        raise KeyError(f"不支援的平台：{platform}（目前支援：{', '.join(_CONTROLLERS)}）")
    return _CONTROLLERS[platform]


def get_node_config(node_id: str) -> dict:
    """回傳 NODE_PLATFORM_MAP 裡這個 node_id 的設定；找不到回傳 {}。"""
    return load_platform_map().get(node_id, {})


def list_configured_nodes() -> dict:
    """回傳整個 NODE_PLATFORM_MAP，給 /list-nodes 這類需要列出「所有已知
    節點」（不只是已經上線回報過心跳的）的指令使用。"""
    return load_platform_map()
