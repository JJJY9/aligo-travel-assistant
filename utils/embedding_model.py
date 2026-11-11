"""进程内共享的句子嵌入模型。

RAG 智能体会为每个会话构造一次；若每个实例都加载一份 BGE 模型
（约 92 MB），内存会随会话数增长。模型推理是只读的，可以安全地在
多个会话 / 线程之间共享同一实例。
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODELS: dict[str, Any] = {}
# 加载失败短时记忆：模型未下载 / 网络不可达时，每个请求都重跑一次完整的
# 加载（含带内部重试的下载，数十秒到分钟级）会把请求线程持续钉住——
# 冷却窗口内直接按上次的失败快速报错
_FAILURES: dict[str, tuple[float, Exception]] = {}
_LOAD_FAILURE_COOLDOWN_SEC = 30.0
_LOCK = threading.Lock()


def embedding_dimension(model: Any) -> int | None:
    """嵌入模型的输出维度；不可探测（新旧接口均缺失或调用失败）返回 None。

    sentence-transformers 6.0 把 ``get_sentence_embedding_dimension`` 更名为
    ``get_embedding_dimension``（旧名暂时保留但会告警）：新名优先、旧名回退，
    升级依赖后不再产生 FutureWarning，也不依赖具体版本。
    """
    getter = getattr(model, "get_embedding_dimension", None)
    if not callable(getter):
        getter = getattr(model, "get_sentence_embedding_dimension", None)
    if not callable(getter):
        return None
    try:
        return int(getter())
    except Exception:  # noqa: BLE001 - 维度探测失败按未知处理
        return None


def peek_embedding_dim() -> int | None:
    """已加载（缓存中）嵌入模型的维度；未加载 / 正在加载 / 多个模型时返回 None。

    只读缓存、绝不触发模型加载、也绝不等锁：知识库状态接口据此 best-effort
    判断集合维度是否与当前进程的嵌入模型一致（换模型后状态不再假装健康）。
    首次加载持锁数十秒（含下载更久），探测必须用非阻塞获取，否则状态接口
    会跟着挂起。
    """
    if not _LOCK.acquire(blocking=False):
        return None
    try:
        models = list(_MODELS.values())
    finally:
        _LOCK.release()
    if len(models) != 1:
        return None
    return embedding_dimension(models[0])


def configured_model_dimension(model_path: str | Path) -> int | None:
    """从本地模型目录配置读取输出维度（不加载模型 / 不 import torch）。

    sentence-transformers 导出的模型目录在 ``1_Pooling/config.json`` 记录
    ``word_embedding_dimension``（BGE 等常用模型都有）：换嵌入模型但未
    重建知识库时，doctor 在未加载模型的进程里也能发现维度不一致。
    HuggingFace 模型 ID（未下载到本地）或目录缺配置时返回 None。
    """
    try:
        root = Path(model_path).expanduser()
    except (TypeError, ValueError):
        return None
    if not root.is_dir():
        return None
    path = root / "1_Pooling" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dimension = data.get("word_embedding_dimension") if isinstance(data, dict) else None
    if isinstance(dimension, int) and dimension > 0:
        return dimension
    return None


def get_embedding_model(model_path: str) -> Any:
    """返回共享的 SentenceTransformer（首次调用时加载并缓存）。

    加载失败在冷却窗口（30 秒）内直接按上次错误快速失败，避免每个请求
    都重跑一次完整的加载 / 下载。
    """
    key = str(model_path)
    model = _MODELS.get(key)
    if model is not None:
        return model
    with _LOCK:
        model = _MODELS.get(key)
        if model is None:
            failed = _FAILURES.get(key)
            if failed is not None:
                failed_at, error = failed
                if time.monotonic() - failed_at < _LOAD_FAILURE_COOLDOWN_SEC:
                    raise RuntimeError(
                        f"嵌入模型加载失败（{error}），请稍后重试"
                    ) from error
                del _FAILURES[key]
            from sentence_transformers import SentenceTransformer

            logger.info("Loading shared embedding model: %s", key)
            try:
                model = SentenceTransformer(key)
            except Exception as exc:
                _FAILURES[key] = (time.monotonic(), exc)
                raise
            _MODELS[key] = model
        return model


def reset_embedding_models() -> None:
    """清空缓存（测试或运维需要时使用）。"""
    with _LOCK:
        _MODELS.clear()
        _FAILURES.clear()
