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
