"""`python cli.py stats` 测试（无需 API Key、不访问网络）。"""
import contextlib
import io
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


class _FakeStore:
    def __init__(self, preferences=None, trips=None, history=None, archived=None):
        self.preferences = dict(preferences or {})
        self.trips = list(trips or [])
        self.history = list(history or [])
        self.archived = list(archived or [])
        self.fail = False
        self.closed = False

    def get_preference(self):
        if self.fail:
            raise RuntimeError("db down")
        return dict(self.preferences)

    def get_trip_history(self, limit=None):
        if self.fail:
            raise RuntimeError("db down")
        return list(self.trips)

    def get_chat_history(self):
        if self.fail:
            raise RuntimeError("db down")
        return list(self.history)

    def get_archived_sessions(self):
        return list(self.archived)

    def close(self):
        self.closed = True


def _message(session_id, role="user", content="内容"):
    return {"session_id": session_id, "role": role, "content": content}


def _run(user_id, as_json=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_stats_standalone(user_id, as_json=as_json)
    return code, buf.getvalue()


def test_stats_text_and_json(monkeypatch):
    store = _FakeStore(
        preferences={"home_location": "北京", "hotel_brands": ["汉庭"]},
        trips=[
            {"destination": "上海", "start_date": "2099-01-01", "end_date": "2099-01-03"},
            {"destination": "杭州", "start_date": "2099-02-01", "end_date": "2099-02-01"},
            {"destination": "无日期"},
        ],
        history=[
            _message("s1"),
            _message("s1", "assistant"),
            _message("s2"),
        ],
        archived=["s2"],
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run("u-stats")
    assert code == 0 and store.closed
    assert "用户 u-stats 概览" in out
    assert "偏好 2 条" in out
    assert "行程 3 条（出行 4 天 / 到访 2 个城市 / 进行中 0 / 即将出行 2）" in out
    assert "会话 2 个（消息 3 条，归档 1）" in out

    code, out = _run("u-stats", as_json=True)
    assert code == 0
    data = json.loads(out)
    assert data["user_id"] == "u-stats"
    assert data["preferences"] == 2
    assert data["travel"]["total_trips"] == 3
    assert data["travel"]["total_days"] == 4
    assert data["travel"]["cities"] == 2
    assert data["sessions"] == {"total": 2, "messages": 3, "archived": 1}
    print("✓ stats 文本 / JSON 输出")


def test_stats_empty_and_failures(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run("u-empty")
    assert code == 0
    assert "行程 0 条（出行 0 天 / 到访 0 个城市" in out
    assert "会话 0 个" in out

    assert cli.run_stats_standalone("-bad") == 1

    store.fail = True
    code, out = _run("u-fail")
    assert code == 1 and "读取数据失败" in out

    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run("u-fail")
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ stats 空数据 / 非法用户 / 读取与打开失败")


def test_stats_dispatch(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "stats", "u1", "--json"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert json.loads(buf.getvalue())["user_id"] == "u1"

    for argv in (
        ["cli.py", "stats"],
        ["cli.py", "stats", "-u1"],
        ["cli.py", "stats", "u1", "--json", "x"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py stats" in buf.getvalue()
    print("✓ dispatch 层 stats 正常执行 / 严格参数")


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_stats_text_and_json(mp)
        mp.undo()
        mp = pytest.MonkeyPatch()
        test_stats_empty_and_failures(mp)
        mp.undo()
        mp = pytest.MonkeyPatch()
        test_stats_dispatch(mp)
    finally:
        mp.undo()
    print("\n全部通过")
