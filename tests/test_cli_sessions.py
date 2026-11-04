"""
`python cli.py sessions` / `session-archive` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_sessions.py
"""
import contextlib
import io
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


def _message(role, content, session_id, timestamp="2026-11-04T10:00:00"):
    return {
        "role": role,
        "content": content,
        "session_id": session_id,
        "timestamp": timestamp,
    }


class _FakeStore:
    def __init__(self, messages, titles=None, pinned=None, archived=None):
        self.messages = list(messages)
        self.titles = dict(titles or {})
        self.pinned = list(pinned or [])
        self.archived = list(archived or [])
        self.archived_calls = []
        self.fail_history = False
        self.closed = False

    def get_chat_history(self, limit=None, session_id=None):
        if self.fail_history:
            raise RuntimeError("db down")
        if session_id:
            return [m for m in self.messages if m.get("session_id") == session_id]
        return list(self.messages)

    def get_session_titles(self):
        return dict(self.titles)

    def get_pinned_sessions(self):
        return list(self.pinned)

    def get_archived_sessions(self):
        return list(self.archived)

    def set_session_archived(self, session_id, archived):
        self.archived_calls.append((session_id, archived))
        if archived and session_id not in self.archived:
            self.archived.append(session_id)
        elif not archived and session_id in self.archived:
            self.archived.remove(session_id)

    def close(self):
        self.closed = True


def _sessions_store() -> _FakeStore:
    return _FakeStore(
        [
            _message("user", "住宿标准问题", "s-active"),
            _message("assistant", "回答 A", "s-active", "2026-11-04T10:02:00"),
            _message("user", "行程规划问题", "s-pinned"),
            _message("assistant", "回答 B", "s-pinned", "2026-11-04T11:00:00"),
            _message("user", "归档的问题", "s-archived"),
            _message("assistant", "回答 C", "s-archived", "2026-11-04T09:00:00"),
        ],
        pinned=["s-pinned"],
        archived=["s-archived"],
    )


def _run_list(user_id, archived=False, include_all=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_sessions_list_standalone(
            user_id, archived_only=archived, include_all=include_all
        )
    return code, buf.getvalue()


def _run_archive(user_id, session_id, archived=True):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_archive_standalone(user_id, session_id, archived)
    return code, buf.getvalue()


def test_lists_active_sessions(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_list("u1")
    assert code == 0
    assert "s-active" in out and "s-pinned" in out
    assert "s-archived" not in out, "默认列表不展示归档会话"
    assert "置顶" in out
    assert fake.closed
    print("✓ sessions 默认列出未归档会话（置顶标记）")


def test_lists_archived_and_all(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_list("u1", archived=True)
    assert code == 0 and "s-archived" in out and "s-active" not in out

    code, out_all = _run_list("u1", include_all=True)
    assert code == 0
    for sid in ("s-active", "s-pinned", "s-archived"):
        assert sid in out_all
    print("✓ --archived / --all 视图正确过滤")


def test_empty_sessions(monkeypatch):
    fake = _FakeStore([])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_list("u1")
    assert code == 0 and "没有未归档会话" in out
    print("✓ 无会话时正常返回")


def test_invalid_user_id(monkeypatch):
    called = {"n": 0}

    def fake_factory(*args):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    code, out = _run_list("../etc/passwd")
    assert code == 1 and "用户 ID" in out
    assert called["n"] == 0, "非法 ID 不应打开存储"
    print("✓ 非法用户 ID 被拒绝")


def test_archive_and_restore(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_archive("u1", "s-active", archived=True)
    assert code == 0 and "已归档会话 s-active" in out
    assert fake.archived_calls == [("s-active", True)]

    code, out = _run_archive("u1", "s-active", archived=False)
    assert code == 0 and "已恢复会话 s-active" in out
    assert fake.archived_calls[-1] == ("s-active", False)
    print("✓ session-archive 归档 / 恢复")


def test_archive_missing_session(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_archive("u1", "s-missing", archived=True)
    assert code == 1 and "不存在或没有消息" in out
    assert fake.archived_calls == []
    print("✓ 不存在的会话返回 1")


def test_read_failure(monkeypatch):
    fake = _sessions_store()
    fake.fail_history = True
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_list("u1")
    assert code == 1 and "读取会话失败" in out
    assert fake.closed
    print("✓ 读取失败返回 1")


def test_store_open_failure(monkeypatch):
    def boom(*args):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run_list("u1")
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ 存储打开失败返回 1")


def test_pg_fallback_refused(monkeypatch):
    """PostgreSQL 配置错误回退 JSON 时，会话维护命令必须拒绝执行。"""
    from config import STORAGE_CONFIG
    from context import long_term_memory as ltm

    class _JsonStore(_FakeStore):
        pass

    monkeypatch.setattr(ltm, "LongTermMemory", _JsonStore)
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://localhost/x")
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args: _JsonStore([])
    )
    code, out = _run_list("u1")
    assert code == 1 and "回退本地 JSON" in out
    print("✓ PostgreSQL 回退时拒绝执行")


def test_flag_parsing_helpers():
    assert cli._parse_sessions_flags([]) == (False, False)
    assert cli._parse_sessions_flags(["--archived"]) == (True, False)
    assert cli._parse_sessions_flags(["--ALL"]) == (False, True)
    assert cli._parse_sessions_flags(["--archived=true"]) is None
    assert cli._parse_sessions_flags(["--archivd"]) is None
    assert cli._parse_sessions_flags(["--archived", "--all"]) is None
    assert cli._parse_archive_flags([]) is False
    assert cli._parse_archive_flags(["--restore"]) is True
    assert cli._parse_archive_flags(["--restore=1"]) is None
    print("✓ 未知 / 冲突参数被拒绝（--restore=1 不会误归档）")


def test_leading_hyphen_ids_rejected(monkeypatch):
    called = {"n": 0}

    def fake_factory(*args):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    code, out = _run_list("--archived")
    assert code == 1 and "用户 ID" in out
    code, out = _run_archive("u1", "--restore")
    assert code == 1 and "会话 ID" in out
    assert called["n"] == 0, "以短横线开头的参数不应打开存储"
    print("✓ 以短横线开头的 ID（疑似参数）被拒绝")


def test_pg_healthy_store_passthrough(monkeypatch):
    """配置了 PG 且返回健康 PG 存储时应正常执行，而不是一律拒绝。"""
    from config import STORAGE_CONFIG
    from context import long_term_memory as ltm

    class _JsonCls:
        pass

    class _PgStore(_FakeStore):
        pass

    monkeypatch.setattr(ltm, "LongTermMemory", _JsonCls)
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://localhost/x")
    fake = _PgStore(
        [_message("user", "PG 会话", "pg-s1")],
        archived=[],
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_list("u1")
    assert code == 0 and "pg-s1" in out
    assert fake.closed
    print("✓ PostgreSQL 正常时正常执行（仅回退时拒绝）")


def test_save_failure_reports_error(monkeypatch):
    fake = _sessions_store()

    def boom(session_id, archived):
        raise OSError("长期记忆写入失败")

    fake.set_session_archived = boom
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args: fake)
    code, out = _run_archive("u1", "s-active", archived=True)
    assert code == 1 and "操作失败" in out
    print("✓ 存储写入失败返回 1")


if __name__ == "__main__":
    test_flag_parsing_helpers()
    for case in (
        test_lists_active_sessions,
        test_lists_archived_and_all,
        test_empty_sessions,
        test_invalid_user_id,
        test_leading_hyphen_ids_rejected,
        test_pg_healthy_store_passthrough,
        test_save_failure_reports_error,
        test_archive_and_restore,
        test_archive_missing_session,
        test_read_failure,
        test_store_open_failure,
        test_pg_fallback_refused,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
