"""
知识库路径与集合常量

RAG Skill、Web 状态接口、CLI 与 doctor 共用同一套解析逻辑，避免各自维护
路径导致“状态显示可用但检索为空”的分歧。
"""
from pathlib import Path

from utils.skill_paths import resolve_skills_root

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_KNOWLEDGE_DIR = (
    resolve_skills_root() / "ask-question" / "data" / "rag_knowledge"
)
LEGACY_KNOWLEDGE_DIR = PROJECT_ROOT / "rag_knowledge"
COLLECTION_NAME = "business_travel_knowledge"


def default_knowledge_dir() -> Path:
    """返回默认知识库目录：优先 Skill 数据目录，其次项目根目录的兼容路径。"""
    for directory in (SKILL_KNOWLEDGE_DIR, LEGACY_KNOWLEDGE_DIR):
        if (directory / "milvus_lite.db").exists():
            return directory
    return SKILL_KNOWLEDGE_DIR


def knowledge_db_path(directory: str | Path | None = None) -> Path:
    """返回 milvus_lite.db 的完整路径。"""
    base = Path(directory) if directory is not None else default_knowledge_dir()
    return base / "milvus_lite.db"
