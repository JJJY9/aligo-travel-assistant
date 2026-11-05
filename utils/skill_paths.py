"""技能目录解析（源码运行 / 安装 wheel / 自定义目录）。

优先级：
1. ``ALIGO_SKILLS_DIR`` 环境变量（自定义技能目录）；
2. 仓库或 site-packages 旁的 ``.claude/skills``（源码运行）；
3. ``sys.prefix/.claude/skills``（wheel 的 data-files 安装位置，虚拟环境根目录）。

wheel 中的技能文件通过 ``[tool.setuptools.data-files]`` 安装到 ``sys.prefix``，
因此安装后也能像源码运行一样动态加载技能。
"""
import os
import sys
from pathlib import Path

ENV_SKILLS_DIR = "ALIGO_SKILLS_DIR"


def skill_roots() -> list[Path]:
    """按优先级返回候选技能目录列表。"""
    roots: list[Path] = []
    override = os.environ.get(ENV_SKILLS_DIR, "").strip()
    if override:
        roots.append(Path(override).expanduser())
    roots.append(Path(__file__).resolve().parent.parent / ".claude" / "skills")
    roots.append(Path(sys.prefix) / ".claude" / "skills")
    return roots


def resolve_skills_root() -> Path:
    """返回实际存在的技能目录；都不存在时返回最高优先级路径。"""
    roots = skill_roots()
    for root in roots:
        if root.is_dir():
            return root
    return roots[0]
