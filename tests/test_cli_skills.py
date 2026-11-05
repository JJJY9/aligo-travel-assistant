"""
`python cli.py skills` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_skills.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils import skill_paths


def _run() -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_skills_standalone()
    return code, buf.getvalue()


def test_lists_repo_skills():
    code, output = _run()
    assert code == 0
    assert "技能目录" in output
    assert "已发现 6 个技能" in output
    for name in ("ask-question", "event-collection", "plan-trip"):
        assert name in output
    print("✓ cli.py skills 列出仓库技能")


def test_missing_root(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(
            skill_paths, "resolve_skills_root", lambda: Path(tmp) / "missing"
        )
        code, output = _run()
    assert code == 1
    assert "未找到技能目录" in output
    assert "ALIGO_SKILLS_DIR" in output
    print("✓ 技能目录缺失 → 退出码 1 并提示环境变量")


def test_empty_root(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(skill_paths, "resolve_skills_root", lambda: Path(tmp))
        code, output = _run()
    assert code == 1
    assert "没有可加载的技能" in output
    print("✓ 空技能目录 → 退出码 1")


def test_main_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cli.py", "skills"])
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    print("✓ cli.py skills 命令分发")


def test_json_output():
    """--json 输出机器可读的技能列表（监控 / 脚本）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_skills_standalone(as_json=True)
    payload = json.loads(buf.getvalue())
    assert code == 0
    assert payload["ok"] is True
    assert payload["skills"] == sorted(payload["skills"])
    assert "plan-trip" in payload["skills"]
    assert payload["root"].replace("\\", "/").endswith(".claude/skills")
    print("✓ skills --json 输出")


def test_main_rejects_unknown_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cli.py", "skills", "--bogus"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "用法" in buf.getvalue()
    print("✓ skills 未知参数 → 退出码 2")


if __name__ == "__main__":
    test_lists_repo_skills()
    mp = pytest.MonkeyPatch()
    try:
        test_missing_root(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_empty_root(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_main_dispatch(mp)
    finally:
        mp.undo()
    test_json_output()
    mp = pytest.MonkeyPatch()
    try:
        test_main_rejects_unknown_flag(mp)
    finally:
        mp.undo()
    print("\n全部通过")
