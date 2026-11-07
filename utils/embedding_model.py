"""进程内共享的句子嵌入模型。

RAG 智能体会为每个会话构造一次；若每个实例都加载一份 BGE 模型
（约 92 MB），内存会随会话数增长。模型推理是只读的，可以安全地在
多个会话 / 线程之间共享同一实例。
"""
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MODELS: dict[str, Any] = {}
_LOCK = threading.Lock()


def peek_embedding_dim() -> int | None:
    """已加载（缓存中）嵌入模型的维度；未加载 / 多个模型时返回 None。

    只读缓存、绝不触发模型加载：知识库状态接口据此 best-effort 判断
    集合维度是否与当前进程的嵌入模型一致（换模型后状态不再假装健康）。
    """
    with _LOCK:
        models = list(_MODELS.values())
    if len(models) != 1:
        return None
    try:
        return int(models[0].get_sentence_embedding_dimension())
    except Exception:  # noqa: BLE001 - 维度探测失败按未知处理
        return None


def get_embedding_model(model_path: str) -> Any:
    """返回共享的 SentenceTransformer（首次调用时加载并缓存）。"""
    key = str(model_path)
    model = _MODELS.get(key)
    if model is not None:
        return model
    with _LOCK:
        model = _MODELS.get(key)
        if model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading shared embedding model: %s", key)
            model = SentenceTransformer(key)
            _MODELS[key] = model
        return model


def reset_embedding_models() -> None:
    """清空缓存（测试或运维需要时使用）。"""
    with _LOCK:
        _MODELS.clear()
