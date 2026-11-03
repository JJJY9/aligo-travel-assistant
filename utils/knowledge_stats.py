"""
知识库状态（Milvus Lite 集合统计）

用于 Web / CLI 展示当前 RAG 知识库是否就绪、集合名称与片段数量。
只读取集合统计，不加载 embedding 模型；未缓存的客户端用后即关，
避免仅打开记忆面板就长期占用 Milvus Lite 的文件锁。
"""
import logging
from pathlib import Path
from typing import Any

from utils.knowledge_paths import COLLECTION_NAME, default_knowledge_dir
from utils.milvus_client import create_temporary_milvus_client, peek_milvus_client

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = COLLECTION_NAME


def default_db_path() -> Path:
    """返回默认知识库数据库路径（与 RAG Skill 使用同一解析逻辑）。"""
    return default_knowledge_dir() / "milvus_lite.db"


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

    client = peek_milvus_client(str(db))
    owns_client = client is None
    try:
        if client is None:
            client = create_temporary_milvus_client(str(db))
        if not client.has_collection(collection):
            return {**base, "available": False, "reason": "集合不存在"}
        stats = client.get_collection_stats(collection)
        return {
            **base,
            "available": True,
            "documents": int(stats.get("row_count", 0) or 0),
        }
    except Exception as exc:  # noqa: BLE001 - 状态查询失败不影响主流程
        logger.warning("获取知识库状态失败: %s", exc)
        return {**base, "available": False, "reason": str(exc)}
    finally:
        if owns_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                pass
