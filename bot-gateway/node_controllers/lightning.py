"""
Lightning AI 節點 controller —— 用官方 `lightning_sdk` 套件直接控制 Studio。

跟 Kaggle 最大的差異：Lightning 的 Studio 是「持續存在的虛擬機」，不是
Kaggle 那種「每次執行都是全新容器」的批次模型，所以：
    - stop() 可以直接呼叫 Studio.stop()，是官方支援的真正遠端關閉，
      不像 Kaggle 只能送信號等節點自己配合
    - start() 呼叫 Studio.start() 之後，用 Studio.run() 執行啟動腳本，
      腳本本身可以直接用一般的 `nohup ... &` 背景執行（Lightning 是完整
      的 VM，不像 Kaggle Notebook 的 ipykernel 會擋 shell 背景語法）
    - 因為 Studio 的檔案系統是持久的，ollama、模型檔案裝過一次之後
      重開機不需要重新下載，這是 Lightning 相對 Kaggle 的一大優勢，
      但也代表 start() 每次都要重新確認 bootstrap.py 是最新版本
      （見下方 run() 那段會重新下載覆蓋）
"""

import os
import logging

from .base import NodeController, StartResult, StopResult

log = logging.getLogger("node-controllers.lightning")

LIGHTNING_USER_ID = os.environ.get("LIGHTNING_USER_ID", "")
LIGHTNING_API_KEY = os.environ.get("LIGHTNING_API_KEY", "")

BOOTSTRAP_RAW_URL = os.environ.get(
    "BOOTSTRAP_RAW_URL",
    "https://raw.githubusercontent.com/hi-Nobody/ai_edge_computing/main/edge-worker/bootstrap.py",
)


def _build_start_command(node_id: str, node_config: dict) -> str:
    """組出在 Studio 裡執行的一行指令：寫 edge.conf、更新 bootstrap.py、
    背景啟動。跟 Kaggle 版模板做同樣的事，但因為 Lightning 是持久 VM，
    ollama 安裝與模型下載交給 bootstrap.py 自己的 ensure_ollama()／
    ensure_model() 判斷是否已存在，不會重複下載。"""
    edge_conf = "\\n".join([
        f"ORACLE_URL={node_config['oracle_url']}",
        f"NODE_ID={node_id}",
        f"NODE_API_KEY={node_config['node_api_key']}",
        f"MODEL_NAME={node_config.get('model_name', 'qwen2.5-coder:14b')}",
        f"VERIFY_TLS={node_config.get('verify_tls', 'false')}",
        f"INFERENCE_TIMEOUT_SEC={node_config.get('inference_timeout_sec', 300)}",
        f"IDLE_STOP_SEC={node_config.get('idle_stop_sec', 1800)}",
    ])
    return (
        f"mkdir -p ~/finflow-edge && cd ~/finflow-edge && "
        f"printf '{edge_conf}\\n' > edge.conf && "
        f"curl -fsSL {BOOTSTRAP_RAW_URL} -o bootstrap.py && "
        f"pip install -q requests && "
        f"nohup python bootstrap.py > bootstrap.log 2>&1 &"
    )


class LightningController(NodeController):
    platform_name = "lightning"

    def _get_studio(self, node_config: dict):
        from lightning_sdk import Studio  # 延遲載入：避免沒裝這個套件時，
                                           # 連只想用 Kaggle 的人也會噴 ImportError
        return Studio(
            name=node_config["studio_name"],
            teamspace=node_config["teamspace"],
            user=node_config.get("lightning_user", LIGHTNING_USER_ID),
        )

    def _resolve_machine(self, node_config: dict):
        """把 NODE_PLATFORM_MAP 裡的 "machine" 字串（例如 "T4"）轉成
        lightning_sdk.Machine 的對應成員。不設定就回傳 None，讓
        studio.start() 用預設行為（沿用這個 Studio 上次的機型）。
        字串刻意做大寫比對，這樣設定檔裡寫 "t4" 或 "T4" 都能吃。"""
        machine_str = node_config.get("machine")
        if not machine_str:
            return None
        from lightning_sdk import Machine
        candidate = machine_str.strip().upper()
        if hasattr(Machine, candidate):
            return getattr(Machine, candidate)
        valid = [name for name in dir(Machine) if not name.startswith("_")]
        raise ValueError(
            f"machine 設定值 {machine_str!r} 不是有效的 Lightning 機型，"
            f"目前 lightning_sdk 版本已知的選項：{', '.join(valid)}"
        )

    def start(self, node_id: str, node_config: dict) -> StartResult:
        if not LIGHTNING_USER_ID or not LIGHTNING_API_KEY:
            return StartResult(False, False, "尚未設定 LIGHTNING_USER_ID／LIGHTNING_API_KEY，無法啟動 Lightning 節點。")
        if "studio_name" not in node_config or "teamspace" not in node_config:
            return StartResult(False, False, f"NODE_PLATFORM_MAP 裡 {node_id} 缺少 studio_name／teamspace。")

        try:
            machine = self._resolve_machine(node_config)
        except ValueError as e:
            return StartResult(False, False, str(e))

        try:
            studio = self._get_studio(node_config)
            if machine is not None:
                studio.start(machine)
            else:
                studio.start()
            command = _build_start_command(node_id, node_config)
            studio.run(command)
            machine_desc = f"，機型 `{node_config['machine']}`" if machine is not None else "（沿用這個 Studio 目前/上次的機型，未在設定裡指定）"
            return StartResult(
                True, True,
                f"已呼叫 Lightning API 啟動 Studio `{node_config['studio_name']}`（節點 `{node_id}`）"
                f"{machine_desc}，並在裡面背景啟動 bootstrap.py。第一次啟動需要下載 Ollama／模型可能較久，"
                f"之後重開會快很多（Studio 硬碟是持久的）。",
            )
        except Exception as e:
            log.exception("Lightning 節點啟動時發生例外")
            return StartResult(False, False, "Lightning 啟動失敗，詳情已寫入伺服器日誌。", detail=str(e))

    def stop(self, node_id: str, node_config: dict) -> StopResult:
        if not LIGHTNING_USER_ID or not LIGHTNING_API_KEY:
            return StopResult(False, False, "尚未設定 LIGHTNING_USER_ID／LIGHTNING_API_KEY，無法停止 Lightning 節點。")
        try:
            studio = self._get_studio(node_config)
            studio.stop()
            return StopResult(
                True, True,
                f"已呼叫 Lightning 官方 API 關閉 Studio `{node_config['studio_name']}`（節點 `{node_id}`），"
                f"這是官方支援的即時關閉，GPU 計費會立刻停止。",
            )
        except Exception as e:
            log.exception("Lightning 節點停止時發生例外")
            return StopResult(
                False, False,
                f"呼叫 Lightning 停止 API 失敗，已另外送出遠端停止信號給節點本身（見 stop_requested 機制）"
                f"作為備援，但無法保證 Studio 真的被關閉，請自行到 Lightning 後台確認。",
                detail=str(e),
            )
