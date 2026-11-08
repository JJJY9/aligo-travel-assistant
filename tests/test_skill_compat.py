"""agents/_skill_compat 加载器的错误路径（无需 API Key / 模型 / 网络）。

覆盖：技能文件缺失、类名不存在、spec 无法建立（loader 为空）与正常加载。
运行：python tests/test_skill_compat.py
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents import _skill_compat


def _write_agent(directory: Path, skill: str, source: str) -> None:
    script_dir = directory / skill / "script"
    script_dir.mkdir(parents=True)
    (script_dir / "agent.py").write_text(source, encoding="utf-8")


def test_missing_file_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(_skill_compat, "resolve_skills_root", lambda: Path(tmp))
        with pytest.raises(ImportError, match="不存在"):
            _skill_compat.load_skill_agent("missing-skill", "AnyClass")
    print("✓ 技能文件缺失抛 ImportError")


def test_missing_class_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _write_agent(Path(tmp), "demo", "class Other:\n    pass\n")
        monkeypatch.setattr(_skill_compat, "resolve_skills_root", lambda: Path(tmp))
        with pytest.raises(ImportError, match="里没有 Missing"):
            _skill_compat.load_skill_agent("demo", "Missing")
    print("✓ 类名不存在抛 ImportError")


def test_unloadable_spec_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _write_agent(Path(tmp), "demo", "class Demo:\n    pass\n")
        monkeypatch.setattr(_skill_compat, "resolve_skills_root", lambda: Path(tmp))
        monkeypatch.setattr(
            _skill_compat.importlib.util,
            "spec_from_file_location",
            lambda name, path: None,
        )
        with pytest.raises(ImportError, match="无法加载"):
            _skill_compat.load_skill_agent("demo", "Demo")
    print("✓ spec 建立失败抛 ImportError")


def test_loads_class_from_skill_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _write_agent(Path(tmp), "demo", "class Demo:\n    marker = 42\n")
        monkeypatch.setattr(_skill_compat, "resolve_skills_root", lambda: Path(tmp))
        loaded = _skill_compat.load_skill_agent("demo", "Demo")
        assert loaded.marker == 42
    print("✓ 正常加载技能文件里的类")


if __name__ == "__main__":
    for case in (
        test_missing_file_raises,
        test_missing_class_raises,
        test_unloadable_spec_raises,
        test_loads_class_from_skill_file,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
