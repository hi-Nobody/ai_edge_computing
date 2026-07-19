"""
Kaggle 節點 controller —— 透過官方 `kaggle` CLI 觸發遠端執行。

**已知限制（不是這支程式的 bug，是 Kaggle 平台本身的限制）**：
    Kaggle 官方 API 沒有任何「遠端停止」端點（社群從 2021 年就在
    https://github.com/Kaggle/kaggle-api/issues/388 要求這個功能，至今
    未實作）。所以這裡的 stop() 做不到「立即確認關閉」，只能透過
    server.py 的 POST /nodes/{id}/stop 標記「請這個節點自己盡快停止」，
    真正的關閉要等跑在 Kaggle 裡的 bootstrap.py 下一次輪詢
    （通常 2 秒內）讀到這個信號、自行結束 process，Kaggle 才會判定
    這次 kernel run 執行完畢、回收 GPU session。

    start() 用官方 CLI `kaggle kernels push`，需要一份「script 型」kernel
    （單一 .py 檔案，不是 .ipynb），內容在推送前用這個模組動態產生，
    模板放在 edge-worker/kaggle-kernel/ 底下，實際會用到的變數
    （NODE_API_KEY、MODEL_NAME 等）在這裡用字串取代填入，避免把機密
    寫進 repo 裡的模板檔案本身。
"""

import os
import sys
import json
import shutil
import string
import tempfile
import subprocess
import logging
from pathlib import Path

from .base import NodeController, StartResult, StopResult, load_node_conf, NodeConfError

log = logging.getLogger("node-controllers.kaggle")

KAGGLE_USERNAME = os.environ.get("KAGGLE_USERNAME", "")
KAGGLE_KEY = os.environ.get("KAGGLE_KEY", "")

# 不要直接用裸指令字串 "kaggle" 交給 subprocess 靠 PATH 去找——bot-gateway.service
# 是 systemd 服務，繼承的是 systemd 自己的最小化 PATH，不是互動式 shell 那個做過
# venv activate 調整的 PATH，裸指令字串在服務環境下不保證找得到，即使套件真的
# 裝在這個 venv 裡也一樣。sys.executable 是目前這個 Python 行程自己的解譯器路徑，
# 一定就是這個 venv 底下的 python3（因為這支程式本來就在這個 venv 裡執行），
# 同一個 venv 的 bin/ 目錄下如果有裝 kaggle 套件，kaggle 執行檔一定跟 python3
# 放在同一層——用這個方式解析出來的絕對路徑，不受 PATH 影響，跟這個專案其他
# systemd 服務一律用絕對路徑啟動執行檔是同一個原則。
_KAGGLE_CLI = str(Path(sys.executable).parent / "kaggle")

# repo 裡的模板檔案位置（跟這支程式在同一份 checkout 裡，不需要另外下載）
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "edge-worker" / "kaggle-kernel"
_KERNEL_SCRIPT_TEMPLATE = _TEMPLATE_DIR / "kernel_script.py.template"
_KERNEL_METADATA_TEMPLATE = _TEMPLATE_DIR / "kernel-metadata.json.template"

# bootstrap.py 的權威來源（單一版本，不在 Kaggle kernel 裡另外維護一份拷貝）
BOOTSTRAP_RAW_URL = os.environ.get(
    "BOOTSTRAP_RAW_URL",
    "https://raw.githubusercontent.com/hi-Nobody/ai_edge_computing/main/edge-worker/bootstrap.py",
)

# kaggle kernels push -t 的最大執行秒數（安全網，即使 IDLE_STOP_SEC／遠端停止信號
# 都失效，Kaggle 官方也會在這個時間強制結束，不會無限期燒 GPU 配額）
KAGGLE_HARD_TIMEOUT_SEC = int(os.environ.get("KAGGLE_HARD_TIMEOUT_SEC", "32400"))  # 9 小時


def _kaggle_env():
    """組出呼叫 kaggle CLI 需要的環境變數（不用 ~/.kaggle/kaggle.json 檔案，
    kaggle 套件官方支援直接讀這兩個環境變數做認證，跟本專案其他金鑰一樣
    集中放在 finflow-queue.env 管理）"""
    env = os.environ.copy()
    env["KAGGLE_USERNAME"] = KAGGLE_USERNAME
    env["KAGGLE_KEY"] = KAGGLE_KEY
    return env


class KaggleController(NodeController):
    platform_name = "kaggle"

    def start(self, node_id: str, node_config: dict) -> StartResult:
        if not KAGGLE_USERNAME or not KAGGLE_KEY:
            return StartResult(False, False, "尚未設定 KAGGLE_USERNAME／KAGGLE_KEY，無法啟動 Kaggle 節點。")

        if not Path(_KAGGLE_CLI).exists():
            return StartResult(
                False, False,
                f"找不到 kaggle CLI（預期路徑：{_KAGGLE_CLI}）。"
                f"請在 bot-gateway 的 venv 裡執行 `pip install -r requirements.txt`"
                f"（已包含 kaggle 套件）後重啟 bot-gateway 服務。",
            )

        kernel_slug = node_config.get("kernel_slug")
        if not kernel_slug:
            return StartResult(False, False, f"NODE_PLATFORM_MAP 裡 {node_id} 沒有設定 kernel_slug。")

        if not _KERNEL_SCRIPT_TEMPLATE.exists() or not _KERNEL_METADATA_TEMPLATE.exists():
            return StartResult(False, False, "找不到 Kaggle kernel 模板檔案，部署是否完整？",
                                detail=str(_TEMPLATE_DIR))

        # 節點的身分／模型／連線資訊一律從 edge-worker/edge.conf 裡對應的
        # [node_id] 區塊讀，
        # 不再由 bot_gateway.py 動態組裝——這份檔案跟手動貼進 Kaggle 網頁執行
        # 時用的是同一份，兩條啟動路徑不會再讀到不一致的設定（見 base.py
        # load_node_conf() 的說明）。
        try:
            node_conf = load_node_conf(node_id)
        except NodeConfError as e:
            return StartResult(False, False, str(e))
        warning_suffix = ""
        if node_conf["_warnings"]:
            warning_suffix = "\n\n⚠️ 以下欄位使用了預設值，不是你以為的設定：\n- " + \
                              "\n- ".join(node_conf["_warnings"])

        try:
            with tempfile.TemporaryDirectory(prefix="kaggle-push-") as tmp:
                tmp_path = Path(tmp)

                # EDGE_CONF_BODY：把 nodes/<node_id>.conf 除了註解/空行以外的
                # 每一行原樣重組回去，直接餵給階段二寫出的 edge.conf——不逐欄位
                # 挑著填，這樣以後 edge.conf 的格式多了新欄位（例如 v6 的
                # INFERENCE_ENGINE／VLLM_*），這裡完全不用跟著改，自動就會
                # 一併帶過去，不會漏。
                edge_conf_lines = [
                    f"{k}={v}" for k, v in node_conf.items() if not k.startswith("_")
                ]
                script_tpl = string.Template(_KERNEL_SCRIPT_TEMPLATE.read_text(encoding="utf-8"))
                script_content = script_tpl.substitute(
                    ORACLE_URL=node_conf["ORACLE_URL"],
                    NODE_ID=node_id,
                    NODE_API_KEY=node_conf["NODE_API_KEY"],
                    VERIFY_TLS=node_conf["VERIFY_TLS"],
                    EDGE_CONF_BODY="\n".join(edge_conf_lines),
                    BOOTSTRAP_URL=BOOTSTRAP_RAW_URL,
                )
                (tmp_path / "kernel_script.py").write_text(script_content, encoding="utf-8")

                meta_tpl = string.Template(_KERNEL_METADATA_TEMPLATE.read_text(encoding="utf-8"))
                meta_content = meta_tpl.substitute(
                    KAGGLE_USERNAME=KAGGLE_USERNAME,
                    KERNEL_SLUG=kernel_slug,
                    KERNEL_TITLE=f"FinFlow Edge Worker - {node_id}",
                )
                (tmp_path / "kernel-metadata.json").write_text(meta_content, encoding="utf-8")

                # --accelerator 是 kaggle CLI 的參數（不是寫在 kernel-metadata.json
                # 裡），可以指定要哪一種加速器（例如 NvidiaTeslaT4），修正先前
                # 誤以為「完全無法指定型號」的說法——型號可以選，但目前沒有已知
                # 方式能保證拿到「兩張」（例如 T4 x2），這件事 Kaggle 官方論壇上
                # 也還有未解決的討論串在問。NODE_PLATFORM_MAP 沒填 accelerator
                # 就交給 Kaggle 預設分配。
                #
                # -t/--timeout 是這次 kernel 執行本身的最長時間上限（不是推送動作
                # 的逾時），用 KAGGLE_HARD_TIMEOUT_SEC 當作安全網：即使
                # IDLE_STOP_SEC、遠端停止信號都沒有正常觸發，Kaggle 官方也會在
                # 這個時間強制結束，不會無限期燒 GPU 配額。
                push_cmd = [
                    _KAGGLE_CLI, "kernels", "push", "-p", str(tmp_path),
                    "-t", str(KAGGLE_HARD_TIMEOUT_SEC),
                ]
                accelerator = node_config.get("accelerator")
                if accelerator:
                    push_cmd += ["--accelerator", accelerator]

                result = subprocess.run(
                    push_cmd,
                    env=_kaggle_env(),
                    capture_output=True,
                    text=True,
                    timeout=60,  # 這是「推送指令本身」在本機執行的逾時，
                                 # 跟上面 -t 控制的「kernel 執行時間」是兩回事
                )
                # 注意：kaggle CLI 的 -t/--timeout 是「這次 push 動作」本身的
                # 逾時參數，不是設定 kernel 執行多久後強制停止的參數——目前
                # kaggle-api 沒有公開的方式能透過 push 指定「kernel 最長執行
                # 時間」，這件事只能靠 Kaggle 網頁上該帳號的 session 時限
                # （通常 9-12 小時，依帳號等級而定）自然發生，KAGGLE_HARD_TIMEOUT_SEC
                # 目前只作為文件記錄用途，不是真的有一個 API 參數在使用它，
                # 如果之後 kaggle-api 開放這個功能，這裡才會真的接上去。

                accelerator_unsupported = False
                if (
                    accelerator
                    and result.returncode != 0
                    and "unrecognized arguments" in (result.stderr or "")
                    and "accelerator" in (result.stderr or "").lower()
                ):
                    # 目前安裝的 kaggle CLI 版本不吃 --accelerator 這個參數
                    # （這個參數的名稱、甚至存不存在，會隨 CLI 版本改變——
                    # 官方文件跟 changelog 對這個 flag 的寫法本身就不一致，
                    # 見 KaggleController 檔頭說明）。與其整個失敗，退回成
                    # 「讓 Kaggle 自動分配」，並在成功訊息裡誠實告知，讓
                    # 使用者知道這次沒有真的照指定型號拿到節點。
                    log.warning(
                        "kaggle CLI 不接受 --accelerator 參數（%s），"
                        "改用不指定 accelerator 重試一次", result.stderr[-200:]
                    )
                    push_cmd = [
                        _KAGGLE_CLI, "kernels", "push", "-p", str(tmp_path),
                        "-t", str(KAGGLE_HARD_TIMEOUT_SEC),
                    ]
                    result = subprocess.run(
                        push_cmd, env=_kaggle_env(), capture_output=True,
                        text=True, timeout=60,
                    )
                    accelerator_unsupported = True

            if result.returncode != 0:
                log.error("kaggle kernels push 失敗：%s", result.stderr)
                stderr_tail = (result.stderr or "").strip()[-300:]
                return StartResult(
                    False, False,
                    f"Kaggle 啟動失敗（推送 kernel 時出錯）：\n```\n{stderr_tail}\n```",
                    detail=result.stderr[-500:],
                )

            msg = (
                f"已透過 Kaggle API 觸發節點 `{node_id}`（kernel: {KAGGLE_USERNAME}/{kernel_slug}，"
                f"模型：{node_conf['MODEL_NAME']}）。"
                f"開機、裝 Ollama、拉模型可能需要幾分鐘，之後可以用 /list-nodes 確認是否已上線。"
            )
            if accelerator_unsupported:
                msg += (
                    f"\n\n⚠️ 目前安裝的 kaggle CLI 版本不接受 `--accelerator` 這個參數，"
                    f"這次已改成不指定型號、交給 Kaggle 自動分配，**不是**照 `{accelerator}` "
                    f"拿到節點。想指定型號的話，先跑 `kaggle kernels push -h` 確認這個版本"
                    f"實際支援的參數名稱，再回報更新 `kaggle.py`。"
                )
            msg += warning_suffix
            return StartResult(True, True, msg)
        except subprocess.TimeoutExpired:
            return StartResult(False, False, "呼叫 Kaggle API 逾時（60 秒內沒有回應），請稍後重試。")
        except Exception as e:
            log.exception("Kaggle 節點啟動時發生未預期例外")
            return StartResult(False, False, "Kaggle 啟動時發生未預期錯誤，詳情已寫入伺服器日誌。", detail=str(e))

    def stop(self, node_id: str, node_config: dict) -> StopResult:
        # Kaggle 沒有官方停止 API（見檔頭說明），實際的「停止」動作交給
        # 呼叫端（bot_gateway.py）另外呼叫 Oracle 的 POST /nodes/{id}/stop，
        # 這裡只是誠實回報「做不到立即確認」，避免呼叫端誤以為 Kaggle
        # 有跟 Lightning 一樣的即時關閉能力
        return StopResult(
            True, False,
            f"Kaggle 沒有官方遠端停止 API，已改為送出停止信號給節點 `{node_id}`，"
            f"節點會在下一次輪詢（通常 2 秒內）自行結束並釋放 Kaggle GPU session，"
            f"不是立即生效，請稍候用 /list-nodes 確認。",
        )
