"""
知识库状态（Milvus Lite 集合统计）

用于 Web / CLI 展示当前 RAG 知识库是否就绪、集合名称与片段数量。
只读取集合统计，不加载 embedding 模型。
"""
import logging
from pathlib import Path
from typing import Any

from utils.milvus_client import get_milvus_client

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DB_PATH = (
    PROJECT_ROOT / ".claude" / "skills" / "ask-question" / "data" / "rag_knowledge" / "milvus_lite.db"
)
LEGACY_DB_PATH = PROJECT_ROOT / "rag_knowledge" / "milvus_lite.db"
DEFAULT_COLLECTION = "business_travel_knowledge"


def default_db_path() -> Path:
    """返回默认知识库路径：优先 Skill 数据目录，其次项目根目录的兼容路径。"""
    for candidate in (SKILL_DB_PATH, LEGACY_DB_PATH):
        if candidate.exists():
            return candidate
    return SKILL_DB_PATH


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

    try:
        client = get_milvus_client(str(db))
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
