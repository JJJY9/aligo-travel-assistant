"""从 `.claude/skills` 插件目录加载智能体类。

项目已把各子智能体迁到 `skills/<skill>/script/agent.py`，旧的
`agents.<name>_agent` 导入路径保留为兼容重导出（测试与外部入口仍在使用）。
加载方式与 `LazyAgentRegistry` 一致：按文件路径动态加载，不复制实现。
"""
import importlib.util
from pathlib import Path

from utils.skill_paths import resolve_skills_root

_SKILLS_ROOT = Path(__file__).resolve().parent.parent / ".claude" / "skills"


def load_skill_agent(skill: str, class_name: str):
    """加载 `skills/<skill>/script/agent.py` 里的智能体类。

    Args:
        skill: 技能目录名，例如 ``event-collection``。
        class_name: 该文件里要取出的类名。

    Raises:
        ImportError: 文件不存在、无法加载，或文件里没有这个类。
    """
    path = resolve_skills_root() / skill / "script" / "agent.py"
    if not path.is_file():
        raise ImportError(f"智能体实现不存在: {path}")
    module_name = f"_chailv_skill_{skill.replace('-', '_')}_agent"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载智能体实现: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, class_name)
    except AttributeError as error:
        raise ImportError(f"{path} 里没有 {class_name}") from error
