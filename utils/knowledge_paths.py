"""
知识库路径与集合常量

RAG Skill、Web 状态接口、CLI 与 doctor 共用同一套解析逻辑，避免各自维护
路径导致“状态显示可用但检索为空”的分歧。
"""
import os
from pathlib import Path

from utils.paths import default_data_root, is_source_checkout
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


def _wheel_data_knowledge_dir() -> Path | None:
    """wheel 安装时的知识库目录：跟随用户数据根（不写安装前缀）。

    技能随 wheel 装在 ``sys.prefix/.claude/skills``，把 Milvus Lite 数据
    写在那里会随重建 venv / pipx reinstall 一起删除；#732 起运行期数据
    统一在 ``default_data_root()``。源码 checkout 保持原行为（技能目录
    优先），避免迁移既有库。
    """
    try:
        if is_source_checkout():
            return None
        return default_data_root() / "rag_knowledge"
    except Exception:  # noqa: BLE001 - 路径解析失败按旧行为
        return None


def default_knowledge_dir() -> Path:
    """返回默认知识库目录。

    源码 checkout：优先 Skill 数据目录，其次项目根目录的兼容路径；
    wheel 安装：优先用户数据目录（见 :func:`_wheel_data_knowledge_dir`）。

    首次解析成功后固定在进程内：面板管理接口每次调用都会重新解析，而 RAG
    智能体在构造时缓存自己的路径——服务运行期间新建出另一个库目录（例如
    手动跑初始化脚本生成 skill 库）时，两者会分裂到两个库（面板看似被清空、
    新增写进旧库）。固定后同一进程始终使用同一个库；重建 / 迁移完成后重启
    进程（或显式调用 :func:`reset_default_knowledge_dir`）再切换。
    """
    global _default_dir
    if _default_dir is not None:
        return _default_dir
    wheel_dir = _wheel_data_knowledge_dir()
    preferred = wheel_dir if wheel_dir is not None else SKILL_KNOWLEDGE_DIR
    for directory in (preferred, SKILL_KNOWLEDGE_DIR, LEGACY_KNOWLEDGE_DIR):
        if not (directory / "milvus_lite.db").exists():
            continue
        # 初始化中止清理失败留下的空库（带待清理标记）：不能当作已初始化
        # 的来源，否则旧部署的知识库永远不再被迁移
        if (directory / ".discard-pending").exists():
            continue
        _default_dir = directory
        return directory
    # 尚未初始化：不固定，库创建后按同一目录解析
    return preferred


def knowledge_db_path(directory: str | Path | None = None) -> Path:
    """返回 milvus_lite.db 的完整路径。"""
    base = Path(directory) if directory is not None else default_knowledge_dir()
    return base / "milvus_lite.db"


def _windows_acp() -> int | None:
    """Windows ANSI 代码页；非 Windows / 探测失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        # 非 Windows 的类型存根没有 windll；运行时该分支只在 nt 走到
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return 936
        return int(windll.kernel32.GetACP())
    except Exception:  # noqa: BLE001 - 探测失败按非 UTF-8 处理
        return 936


def knowledge_path_encoding_hint(
    path: str | Path,
    *,
    windows: bool | None = None,
    acp: int | None = None,
    source_checkout: bool | None = None,
) -> str | None:
    """知识库路径在 Windows 下含非 ASCII 字符时的修正提示（其余情况 None）。

    仓库钉死的 pymilvus / milvus-lite 在 Windows 上把索引路径按 ANSI 代码页
    传给 faiss：路径含中文时索引写盘失败，但同进程内 insert / 检索仍走内存
    索引、初始化脚本自检全部通过；进程重启后集合加载必然失败。写入端没有
    办法提前感知，只能在这里统一给出可操作的规避指引（init 提前失败、
    状态接口把「重新运行初始化脚本」换成真实原因）。

    提示按部署形态给可执行文案：源码 checkout 下知识库目录固定在
    ``.claude/skills``（``ALIGO_DATA_DIR`` 不会移动它），让用户设置该变量
    是无效指引；只有 wheel 安装才跟随用户数据目录。
    """
    is_windows = os.name == "nt" if windows is None else bool(windows)
    if not is_windows:
        return None
    if str(path).isascii():
        return None
    code_page = _windows_acp() if acp is None else acp
    if code_page == 65001:
        # 已启用「Beta: 使用 Unicode UTF-8 提供全球语言支持」：ANSI 代码页
        # 就是 UTF-8，faiss 按 ANSI 解释也能得到正确路径
        return None
    if source_checkout is None:
        try:
            source_checkout = is_source_checkout()
        except Exception:  # noqa: BLE001 - 路径解析失败按安装形态给通用文案
            source_checkout = False
    if source_checkout:
        return (
            "知识库路径含非 ASCII 字符，Windows 下 Milvus Lite 无法把索引写入磁盘"
            "（重启后集合加载失败）；请把仓库放到纯英文路径（如 D:\\Code\\chailv），"
            "或把 .claude/skills 复制到纯英文目录并用 ALIGO_SKILLS_DIR 指向它后"
            "重建知识库（源码运行时 ALIGO_DATA_DIR 不会移动知识库）"
        )
    return (
        "知识库路径含非 ASCII 字符，Windows 下 Milvus Lite 无法把索引写入磁盘"
        "（重启后集合加载失败）；请把 ALIGO_DATA_DIR（或 ALIGO_SKILLS_DIR）"
        "指向纯英文路径后重建知识库"
    )
