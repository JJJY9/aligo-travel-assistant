"""
`python cli.py sessions-export` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_sessions_export.py
"""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


class _FakeStore:
    def __init__(self, messages, titles=None):
        self.messages = list(messages)
        self.titles = dict(titles or {})
        self.fail = False
        self.closed = False

    def get_chat_history(self, limit=None, session_id=None):
        if self.fail:
            raise RuntimeError("db down")
        return list(self.messages)

    def get_session_titles(self):
        if self.fail:
            raise RuntimeError("db down")
        return dict(self.titles)

    def close(self):
        self.closed = True


def _msg(role, content, session_id, timestamp):
    return {
        "role": role,
        "content": content,
        "session_id": session_id,
        "timestamp": timestamp,
    }


def _store():
    return _FakeStore(
        [
            _msg("user", "第一问：住宿标准", "exp-a", "2026-11-01T10:00:00"),
            _msg("assistant", "回答一", "exp-a", "2026-11-01T10:01:00"),
            _msg("user", "第二问：杭州行程", "exp-b", "2026-11-02T10:00:00"),
        ],
        titles={"exp-b": "杭州行程"},
    )


def _run(monkeypatch, store, *args, **kwargs):
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_sessions_export_standalone(*args, **kwargs)
    return code, out.getvalue()


def test_export_to_stdout(monkeypatch):
    code, out = _run(monkeypatch, _store(), "u-export", None, True)
    assert code == 0
    assert out.startswith("# Aligo 会话导出")
    assert "会话数：2" in out
    assert "杭州行程" in out and "第一问：住宿标准" in out
    assert "### 用户" in out and "### Aligo" in out
    print("✓ sessions-export 输出到 stdout")


def test_export_to_file_and_overwrite_protection(monkeypatch):
    store = _store()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sessions.md"
        path.write_text("keep", encoding="utf-8")
        code, out = _run(monkeypatch, store, "u-export", str(path))
        assert code == 1 and "文件已存在" in out
        assert path.read_text(encoding="utf-8") == "keep"

        code, out = _run(
            monkeypatch, store, "u-export", str(path), to_stdout=False, force=True
        )
        assert code == 0 and "已导出 2 个会话" in out
        assert "杭州行程" in path.read_text(encoding="utf-8")
    print("✓ sessions-export 写入文件 / 默认拒绝覆盖 / --force")


def test_export_without_sessions(monkeypatch):
    code, out = _run(monkeypatch, _FakeStore([]), "u-export")
    assert code == 1 and "没有可导出的会话" in out
    print("✓ sessions-export 无会话返回 1")


def test_export_failures(monkeypatch):
    failing = _store()
    failing.fail = True
    code, out = _run(monkeypatch, failing, "u-export")
    assert code == 1 and "读取会话失败" in out and failing.closed

    def boom(*args, **kwargs):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_sessions_export_standalone("u-export")
    assert code == 1 and "打开长期记忆失败" in out.getvalue()
    print("✓ sessions-export 读取 / 打开失败返回 1")


def test_export_rejects_bad_user_id(monkeypatch):
    called = {"n": 0}

    def factory(*args, **kwargs):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", factory)
    for user_id in ("bad id", "-u1", "a" * 65):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run_sessions_export_standalone(user_id)
        assert code == 1 and "用户 ID" in out.getvalue()
    assert called["n"] == 0
    print("✓ sessions-export 非法用户 ID")


def test_dispatch_strict(monkeypatch):
    calls = []

    def fake(user, target=None, to_stdout=False, force=False):
        calls.append((user, target, to_stdout, force))
        return 0

    monkeypatch.setattr(cli, "run_sessions_export_standalone", fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "sessions-export", "u1", "out.md", "--stdout", "--force"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2, "--stdout 与 path 冲突应返回用法错误"

    monkeypatch.setattr(sys, "argv", ["cli.py", "sessions-export", "u1", "--stdout"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == [("u1", None, True, False)]

    for argv in (
        ["cli.py", "sessions-export"],
        ["cli.py", "sessions-export", "-u1"],
        ["cli.py", "sessions-export", "u1", "--unknown"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py sessions-export" in out.getvalue()
    print("✓ sessions-export 严格参数解析")


if __name__ == "__main__":
    for case in (
        test_export_to_stdout,
        test_export_to_file_and_overwrite_protection,
        test_export_without_sessions,
        test_export_failures,
        test_export_rejects_bad_user_id,
        test_dispatch_strict,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
