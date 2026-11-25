"""进程内共享的 Cross-Encoder 重排模型。

RAG 智能体每个 Web 会话各持有一个实例（会话上限 50）；若每个实例都加载
一份重排模型（bge-reranker-base 量级约 1 GB），启用 ``RAG_RERANK_MODEL``
后内存会随会话数线性增长。重排推理是只读的，可以安全地在多个会话 /
线程之间共享同一实例——与 ``utils/embedding_model`` 的嵌入模型同一口径。
"""
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_MODELS: dict[str, Any] = {}
# 加载失败短时记忆：模型未下载 / 网络不可达时，每个会话都会重跑一次完整
# 加载（含带内部重试的下载，分钟级）并把请求线程钉住；冷却窗口内直接按
# 上次失败快速报错
_FAILURES: dict[str, tuple[float, Exception]] = {}
_LOAD_FAILURE_COOLDOWN_SEC = 60.0
_LOCK = threading.Lock()


def get_shared_cross_encoder(model_name: str) -> Any:
    """返回进程级共享的 CrossEncoder；同名模型只加载一次。

    加载在锁内完成：首次并发请求排队等一次加载，而不是各自加载一份。
    失败进入 60 秒冷却并按 ``RuntimeError`` 抛出（调用方按「重排不可用」
    退回 RRF 顺序）。
    """
    name = str(model_name or "").strip()
    if not name:
        raise ValueError("重排模型名为空")
    # 加载放在锁内（与 utils.embedding_model 同口径）：并发首载的其它
    # 请求排队复用同一份模型，而不是各自构造一份 ~1GB 的 CrossEncoder
    with _LOCK:
        cached = _MODELS.get(name)
        if cached is not None:
            return cached
        failure = _FAILURES.get(name)
        if failure is not None and (
            time.monotonic() - failure[0]
        ) <= _LOAD_FAILURE_COOLDOWN_SEC:
            raise RuntimeError(f"重排模型加载失败（冷却中）：{failure[1]}")
        try:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(name)
        except Exception as exc:  # noqa: BLE001 - 调用方按增强项降级
            _FAILURES[name] = (time.monotonic(), exc)
            raise
        _MODELS[name] = model
        _FAILURES.pop(name, None)
        logger.info("CrossEncoder 重排模型已加载并共享：%s", name)
        return model
