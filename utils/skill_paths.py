"""技能目录解析（源码运行 / 安装 wheel / 自定义目录）。

优先级：
1. ``ALIGO_SKILLS_DIR`` 环境变量（自定义技能目录）；
2. 仓库或 site-packages 旁的 ``.claude/skills``（源码运行）；
3. ``sys.prefix/.claude/skills``（wheel 的 data-files 安装位置，虚拟环境根目录）；
4. ``site.getuserbase()/.claude/skills``（``pip install --user`` 的 data-files 位置）。

wheel 中的技能文件通过 ``[tool.setuptools.data-files]`` 安装到 ``sys.prefix``
（普通安装）或用户目录（``--user`` 安装），因此安装后也能像源码运行一样动态加载技能。
"""
import os
import sys
from pathlib import Path

ENV_SKILLS_DIR = "ALIGO_SKILLS_DIR"


def _user_base_skills() -> Path | None:
    """返回 ``pip install --user`` 对应的技能目录；取不到用户目录时返回 None。"""
    try:
        import site

        base = site.getuserbase()
    except Exception:  # noqa: BLE001 - 可选路径，环境不支持时忽略
        return None
    if not base:
        return None
    return Path(base) / ".claude" / "skills"


def skill_roots() -> list[Path]:
    """按优先级返回候选技能目录列表。"""
    roots: list[Path] = []
    override = os.environ.get(ENV_SKILLS_DIR, "").strip()
    if override:
        roots.append(Path(override).expanduser())
    roots.append(Path(__file__).resolve().parent.parent / ".claude" / "skills")
    roots.append(Path(sys.prefix) / ".claude" / "skills")
    user_base = _user_base_skills()
    if user_base is not None:
        roots.append(user_base)
    return roots


def resolve_skills_root() -> Path:
    """返回实际存在的技能目录；都不存在时返回最高优先级路径。"""
    roots = skill_roots()
    for root in roots:
        if root.is_dir():
            return root
    return roots[0]


def skill_scripts(root: Path | None = None) -> dict[str, Path]:
    """返回技能名 → ``script/agent.py`` 路径（目录不存在或没有技能时为空）。"""
    base = root if root is not None else resolve_skills_root()
    scripts: dict[str, Path] = {}
    if not base.is_dir():
        return scripts
    try:
        entries = list(base.iterdir())
    except OSError:  # 权限不足等文件系统问题：按「没有技能」处理，避免健康检查 500
        return scripts
    for skill_dir in entries:
        if not skill_dir.is_dir():
            continue
        script = skill_dir / "script" / "agent.py"
        if script.is_file():
            scripts[skill_dir.name] = script
    return scripts
