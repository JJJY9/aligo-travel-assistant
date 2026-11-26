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
# 等待另一个线程完成首次加载的上限：加载发生在 asyncio 默认线程池内
# （聊天检索经 asyncio.to_thread 进入），无界等锁会把多个默认池 worker
# 钉在分钟级的下载上，拖垮会话落库 / 记忆读写等其它同步路径。超过该
# 窗口按「加载中」快速降级，本次检索保持 RRF 顺序
_LOAD_WAIT_SEC = 2.0
_LOCK = threading.Lock()


class RerankerCooldownError(RuntimeError):
    """共享加载器处于失败冷却中：不代表调用方实例自身加载失败。

    冷却由任一会话的失败触发；调用方不能据此把本实例永久标记为
    「重排不可用」，否则窗口内首访的所有会话都会静默失去精排。
    """


class RerankerLoadingError(RuntimeError):
    """另一个线程正在加载同一模型，有限等待内未完成。

    瞬时状态：本次按 RRF 顺序降级即可，不应计入加载失败冷却，也不应
    让调用方永久放弃重排。
    """


def get_shared_cross_encoder(model_name: str) -> Any:
    """返回进程级共享的 CrossEncoder；同名模型只加载一次。

    加载在锁内完成：首次并发请求排队等一次加载，而不是各自加载一份。
    排队有时间上限（``_LOAD_WAIT_SEC``），超时抛
    :class:`RerankerLoadingError`；失败进入 60 秒冷却并抛
    :class:`RerankerCooldownError`（两者都是 ``RuntimeError`` 子类，
    调用方按「重排不可用」退回 RRF 顺序）。
    """
    name = str(model_name or "").strip()
    if not name:
        raise ValueError("重排模型名为空")
    cached = _MODELS.get(name)
    if cached is not None:
        return cached
    # 加载放在锁内（与 utils.embedding_model 同口径）：并发首载的其它
    # 请求排队复用同一份模型，而不是各自构造一份 ~1GB 的 CrossEncoder
    if not _LOCK.acquire(timeout=_LOAD_WAIT_SEC):
        raise RerankerLoadingError(
            f"重排模型正在由其它会话加载：{name}，本次跳过重排"
        )
    try:
        cached = _MODELS.get(name)
        if cached is not None:
            return cached
        failure = _FAILURES.get(name)
        if failure is not None and (
            time.monotonic() - failure[0]
        ) <= _LOAD_FAILURE_COOLDOWN_SEC:
            raise RerankerCooldownError(f"重排模型加载失败（冷却中）：{failure[1]}")
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
    finally:
        _LOCK.release()
