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


_default_dir: Path | None = None


def reset_default_knowledge_dir() -> None:
    """丢弃进程级目录快照（测试或知识库重建 / 迁移后显式切换）。"""
    global _default_dir
    _default_dir = None


def default_knowledge_dir() -> Path:
    """返回默认知识库目录：优先 Skill 数据目录，其次项目根目录的兼容路径。

    首次解析成功后固定在进程内：面板管理接口每次调用都会重新解析，而 RAG
    智能体在构造时缓存自己的路径——服务运行期间新建出另一个库目录（例如
    手动跑初始化脚本生成 skill 库）时，两者会分裂到两个库（面板看似被清空、
    新增写进旧库）。固定后同一进程始终使用同一个库；重建 / 迁移完成后重启
    进程（或显式调用 :func:`reset_default_knowledge_dir`）再切换。
    """
    global _default_dir
    if _default_dir is not None:
        return _default_dir
    for directory in (SKILL_KNOWLEDGE_DIR, LEGACY_KNOWLEDGE_DIR):
        if (directory / "milvus_lite.db").exists():
            _default_dir = directory
            return directory
    # 尚未初始化：不固定，库创建后按同一目录解析
    return SKILL_KNOWLEDGE_DIR


def knowledge_db_path(directory: str | Path | None = None) -> Path:
    """返回 milvus_lite.db 的完整路径。"""
    base = Path(directory) if directory is not None else default_knowledge_dir()
    return base / "milvus_lite.db"
