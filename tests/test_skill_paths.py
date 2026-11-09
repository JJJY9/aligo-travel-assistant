"""
技能目录解析与 wheel 打包清单测试（无需 API Key、不访问网络）

运行：python tests/test_skill_paths.py
"""
import os
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 没有 tomllib
    tomllib = None  # type: ignore[assignment]

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import skill_paths


def test_source_tree_resolves_repo_skills():
    root = skill_paths.resolve_skills_root()
    assert root == Path(project_root) / ".claude" / "skills"
    assert (root / "plan-trip" / "script" / "agent.py").is_file()
    print("✓ 源码运行解析到仓库 .claude/skills")


def test_env_override(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        custom = Path(tmp) / "skills"
        (custom / "demo").mkdir(parents=True)
        monkeypatch.setenv(skill_paths.ENV_SKILLS_DIR, str(custom))
        assert skill_paths.resolve_skills_root() == custom
    print("✓ ALIGO_SKILLS_DIR 覆盖技能目录")


def test_prefix_fallback_for_installed_wheel(monkeypatch):
    """源码目录不存在时回退到 sys.prefix/.claude/skills（wheel data-files 位置）。"""
    monkeypatch.delenv(skill_paths.ENV_SKILLS_DIR, raising=False)
    with tempfile.TemporaryDirectory() as fake_pkg, tempfile.TemporaryDirectory() as prefix:
        monkeypatch.setattr(
            skill_paths, "__file__", str(Path(fake_pkg) / "utils" / "skill_paths.py")
        )
        installed = Path(prefix) / ".claude" / "skills"
        (installed / "plan-trip").mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", prefix)
        assert skill_paths.resolve_skills_root() == installed
    print("✓ 源码目录缺失时回退到 sys.prefix/.claude/skills")


def test_user_base_fallback_for_pip_user_install(monkeypatch):
    """源码与 sys.prefix 都没有技能时，回退到 pip install --user 的用户目录。"""
    monkeypatch.delenv(skill_paths.ENV_SKILLS_DIR, raising=False)
    with (
        tempfile.TemporaryDirectory() as fake_pkg,
        tempfile.TemporaryDirectory() as prefix,
        tempfile.TemporaryDirectory() as userbase,
    ):
        monkeypatch.setattr(
            skill_paths, "__file__", str(Path(fake_pkg) / "utils" / "skill_paths.py")
        )
        monkeypatch.setattr(sys, "prefix", prefix)
        monkeypatch.setattr("site.getuserbase", lambda: userbase)
        installed = Path(userbase) / ".claude" / "skills"
        (installed / "plan-trip").mkdir(parents=True)
        assert skill_paths.resolve_skills_root() == installed

        # sys.prefix 下的技能目录优先于用户目录
        prefix_skills = Path(prefix) / ".claude" / "skills"
        (prefix_skills / "plan-trip").mkdir(parents=True)
        assert skill_paths.resolve_skills_root() == prefix_skills
    print("✓ pip install --user 的用户目录回退，且 sys.prefix 优先")


def test_skill_scripts_discovers_agents():
    """skill_scripts 返回技能名 → agent.py，缺失目录返回空字典。"""
    from utils.skill_paths import skill_scripts

    root = Path(project_root) / ".claude" / "skills"
    scripts = skill_scripts(root)
    assert set(scripts) == {
        "ask-question",
        "event-collection",
        "memory-query",
        "plan-trip",
        "preference",
        "query-info",
    }
    assert scripts["plan-trip"].is_file()
    assert skill_scripts(root / "missing") == {}
    print("✓ skill_scripts 发现技能脚本，缺失目录返回空")


def test_skill_scripts_handles_permission_error(monkeypatch):
    """技能目录不可读时返回空字典，健康检查不应因此报错。"""
    from utils.skill_paths import skill_scripts

    def denied(self):
        raise PermissionError("denied")

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(Path, "iterdir", denied)
        assert skill_scripts(Path(tmp)) == {}
    print("✓ 技能目录不可读时安全返回空")


def test_sysconfig_data_dir_fallback(monkeypatch):
    """sys.prefix 下没有技能时按 sysconfig 的 data 目录解析（系统级 Python）。"""
    monkeypatch.delenv(skill_paths.ENV_SKILLS_DIR, raising=False)
    with (
        tempfile.TemporaryDirectory() as fake_pkg,
        tempfile.TemporaryDirectory() as prefix,
        tempfile.TemporaryDirectory() as data_dir,
    ):
        monkeypatch.setattr(
            skill_paths, "__file__", str(Path(fake_pkg) / "utils" / "skill_paths.py")
        )
        monkeypatch.setattr(sys, "prefix", prefix)
        installed = Path(data_dir) / ".claude" / "skills"
        (installed / "plan-trip").mkdir(parents=True)
        monkeypatch.setattr(
            skill_paths, "_data_scheme_skills", lambda: installed
        )
        assert skill_paths.resolve_skills_root() == installed
    print("✓ sys.prefix 无技能时回退 sysconfig data 目录")


def test_permission_error_degrades_to_empty(monkeypatch):
    """权限受限（EACCES）时技能解析降级为空，健康检查不崩。"""
    monkeypatch.delenv(skill_paths.ENV_SKILLS_DIR, raising=False)

    def denied(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_dir", denied)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    root = skill_paths.resolve_skills_root()
    assert isinstance(root, Path)
    assert skill_paths.skill_scripts(root) == {}
    print("✓ 权限受限时技能解析降级为空")


def test_wheel_data_files_cover_skill_code():
    """pyproject 的 data-files 必须覆盖全部技能代码与声明，且不包含运行时数据。"""
    if tomllib is None:
        pytest.skip("Python 3.10 无 tomllib；打包清单在 Python 3.12 CI 校验")
    with open(Path(project_root) / "pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    packaged = {path for files in data_files.values() for path in files}

    skills_root = Path(project_root) / ".claude" / "skills"
    expected = {
        path.relative_to(project_root).as_posix()
        for path in skills_root.rglob("*")
        if path.is_file()
        and "data" not in path.relative_to(skills_root).parts
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    missing = expected - packaged
    assert not missing, f"wheel 缺少技能文件：{sorted(missing)}"

    for path in packaged:
        assert "/data/" not in path and "__pycache__" not in path, path
        assert (Path(project_root) / path).is_file(), path
    print(f"✓ wheel data-files 覆盖 {len(packaged)} 个技能文件（无知识库数据）")


def test_is_file_tolerates_oserror(monkeypatch):
    """Path.is_file 抛 OSError（权限不足）按「不存在」处理。"""

    def boom(self):
        raise OSError("denied")

    monkeypatch.setattr(Path, "is_file", boom)
    assert skill_paths._is_file(Path("any")) is False
    print("✓ _is_file 容错 OSError")


def test_data_scheme_skills_fallbacks(monkeypatch):
    """sysconfig 不可用 / 没有 data 目录时返回 None。"""
    import sysconfig

    def boom():
        raise RuntimeError("no sysconfig")

    monkeypatch.setattr(sysconfig, "get_paths", boom)
    assert skill_paths._data_scheme_skills() is None

    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"data": ""})
    assert skill_paths._data_scheme_skills() is None
    print("✓ data scheme 技能目录回退 None")


def test_user_base_skills_fallbacks(monkeypatch):
    """取不到用户目录 / 目录为空时返回 None。"""

    def boom():
        raise RuntimeError("no user base")

    monkeypatch.setattr("site.getuserbase", boom)
    assert skill_paths._user_base_skills() is None

    monkeypatch.setattr("site.getuserbase", lambda: "")
    assert skill_paths._user_base_skills() is None
    print("✓ 用户目录技能路径回退 None")


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_source_tree_resolves_repo_skills()
        test_env_override(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_prefix_fallback_for_installed_wheel(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_base_fallback_for_pip_user_install(mp)
    finally:
        mp.undo()
    test_skill_scripts_discovers_agents()
    mp = pytest.MonkeyPatch()
    try:
        test_skill_scripts_handles_permission_error(mp)
    finally:
        mp.undo()
    test_wheel_data_files_cover_skill_code()
    mp = pytest.MonkeyPatch()
    try:
        test_sysconfig_data_dir_fallback(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_permission_error_degrades_to_empty(mp)
    finally:
        mp.undo()
    for case in (
        test_is_file_tolerates_oserror,
        test_data_scheme_skills_fallbacks,
        test_user_base_skills_fallbacks,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
