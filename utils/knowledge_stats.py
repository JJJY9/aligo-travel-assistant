"""
知识库状态（Milvus Lite 集合统计）

用于 Web / CLI 展示当前 RAG 知识库是否就绪、集合名称与片段数量。
只读取集合统计，不加载 embedding 模型；客户端与知识库维护接口共享
（Milvus Lite 的 data-dir 锁在整个进程生命周期内保持，见 README 已知限制）。
"""
import contextlib
import logging
from pathlib import Path
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, default_knowledge_dir
from utils.milvus_client import get_milvus_client

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = COLLECTION_NAME


def default_db_path() -> Path:
    """返回默认知识库数据库路径（与 RAG Skill 使用同一解析逻辑）。"""
    return default_knowledge_dir() / "milvus_lite.db"


def _collection_dimension(client: Any, collection: str) -> int | None:
    """集合向量字段的维度；不可读取（旧客户端 / 异常）返回 None。"""
    try:
        description = client.describe_collection(collection)
        for field in description.get("fields") or []:
            params = field.get("params") or {}
            if "dim" in params:
                return int(params["dim"])
    except Exception as exc:  # noqa: BLE001 - 维度探测失败不影响状态主体
        logger.debug("读取集合维度失败: %s", exc)
    return None


def _loaded_embedding_dim() -> int | None:
    """当前进程已加载嵌入模型的维度（不触发加载）。"""
    try:
        from utils.embedding_model import peek_embedding_dim

        return peek_embedding_dim()
    except Exception:  # noqa: BLE001 - 可选依赖缺失时按未知处理
        return None


def knowledge_status(
    db_path: str | Path | None = None,
    collection: str = DEFAULT_COLLECTION,
) -> dict[str, Any]:
    """返回知识库状态：是否可用、集合名、片段数与数据库路径。"""
    db = Path(db_path) if db_path is not None else default_db_path()
    base: dict[str, Any] = {"collection": collection, "path": str(db)}

    # Milvus Lite 2.x 为单文件，3.x 为目录，两者都视为已初始化
    if not db.exists():
        return {**base, "available": False, "reason": "知识库未初始化"}

    client = None
    # 与知识库写锁互斥：整组替换是多步写入，期间读到中间行数会让面板
    # chip 与列表 total 不一致
    lock_ctx: Any
    try:
        from utils.knowledge_admin import _WRITE_LOCK

        lock_ctx = _WRITE_LOCK
    except Exception:  # noqa: BLE001 - 维护模块不可用时保持原行为
        lock_ctx = contextlib.nullcontext()
    try:
        client = get_milvus_client(str(db))
        with lock_ctx:
            if not client.has_collection(collection):
                return {**base, "available": False, "reason": "集合不存在"}
            stats = client.get_collection_stats(collection)
            dimension = _collection_dimension(client, collection)
        payload = {
            **base,
            "available": True,
            "documents": int(stats.get("row_count", 0) or 0),
        }
        if dimension is not None:
            payload["dimension"] = dimension
        expected = _loaded_embedding_dim()
        if expected is not None and dimension is not None:
            payload["dimension_mismatch"] = int(dimension) != int(expected)
        return payload
    except Exception as exc:  # noqa: BLE001 - 状态查询失败不影响主流程
        logger.warning("获取知识库状态失败: %s", exc)
        return {**base, "available": False, "reason": str(exc)}
