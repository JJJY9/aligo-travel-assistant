"""
`python cli.py sessions` / `session-archive` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_sessions.py
"""
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


def _message(role, content, session_id, timestamp="2026-11-04T10:00:00"):
    return {
        "role": role,
        "content": content,
        "session_id": session_id,
        "timestamp": timestamp,
    }


class _FakeStore:
    def __init__(self, messages, titles=None, pinned=None, archived=None, trips=None):
        self.messages = list(messages)
        self.titles = dict(titles or {})
        self.pinned = list(pinned or [])
        self.archived = list(archived or [])
        self.trips = list(trips or [])
        self.archived_calls = []
        self.fail_history = False
        self.fail_delete = False
        self.deleted_sessions = []
        self.closed = False

    def get_chat_history(self, limit=None, session_id=None):
        if self.fail_history:
            raise RuntimeError("db down")
        if session_id:
            return [m for m in self.messages if m.get("session_id") == session_id]
        return list(self.messages)

    def get_trip_history(self, limit=None):
        return list(self.trips)

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

    def delete_chat_session(self, session_id):
        if self.fail_delete:
            raise RuntimeError("delete down")
        before = len(self.messages)
        self.messages = [
            m for m in self.messages if m.get("session_id") != session_id
        ]
        for trip in self.trips:
            if trip.get("session_id") == session_id:
                trip["session_id"] = None
        self.deleted_sessions.append(session_id)
        return before - len(self.messages)

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


def _run_list(user_id, archived=False, include_all=False, query="", as_json=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_sessions_list_standalone(
            user_id,
            archived_only=archived,
            include_all=include_all,
            query=query,
            as_json=as_json,
        )
    return code, buf.getvalue()


def _run_archive(user_id, session_id, archived=True):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_archive_standalone(user_id, session_id, archived)
    return code, buf.getvalue()


def test_lists_active_sessions(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1")
    assert code == 0
    assert "s-active" in out and "s-pinned" in out
    assert "s-archived" not in out, "默认列表不展示归档会话"
    assert "置顶" in out
    assert fake.closed
    print("✓ sessions 默认列出未归档会话（置顶标记）")


def test_lists_archived_and_all(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1", archived=True)
    assert code == 0 and "s-archived" in out and "s-active" not in out

    code, out_all = _run_list("u1", include_all=True)
    assert code == 0
    for sid in ("s-active", "s-pinned", "s-archived"):
        assert sid in out_all
    print("✓ --archived / --all 视图正确过滤")


def test_list_filters_by_query(monkeypatch):
    """--query 在标题与消息内容中匹配，命中时附带片段；归档范围遵循视图。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1", query="行程")
    assert code == 0
    assert "s-pinned" in out and "s-active" not in out
    assert "关键词：行程" in out
    assert "> 行程规划问题" in out, "命中内容应附带片段"

    code, out = _run_list("u1", query="回答 C")  # 只在归档会话里
    assert code == 0 and "没有匹配" in out, out

    code, out = _run_list("u1", query="回答 C", include_all=True)
    assert code == 0 and "s-archived" in out
    print("✓ sessions --query 按标题 / 内容过滤并附带片段")


def test_list_json_output(monkeypatch):
    """--json 输出会话摘要（含命中片段），空结果也是合法 JSON。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1", as_json=True)
    assert code == 0
    data = json.loads(out)
    assert data["total"] == 2 and data["archived_total"] == 1
    ids = {item["session_id"] for item in data["sessions"]}
    assert ids == {"s-active", "s-pinned"}
    pinned = next(item for item in data["sessions"] if item["session_id"] == "s-pinned")
    assert pinned["pinned"] is True and pinned["archived"] is False
    assert "_parts" not in pinned

    code, out = _run_list("u1", query="行程", as_json=True)
    data = json.loads(out)
    assert data["total"] == 1
    assert data["sessions"][0]["snippet"].startswith("行程")

    code, out = _run_list("u1", query="不存在", as_json=True)
    assert code == 0 and json.loads(out)["sessions"] == []

    # 关键词在消息后段时，片段应围绕命中位置（而不是只取前 40 字）
    fake = _FakeStore(
        [_message("user", "x" * 60 + "后段关键词", "s-long")]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1", query="后段关键词", as_json=True)
    snippet = json.loads(out)["sessions"][0]["snippet"]
    assert "后段关键词" in snippet, snippet
    print("✓ sessions --json 输出摘要 / 片段 / 空列表 / 命中窗口")


def test_session_list_converts_utc_timestamps(monkeypatch):
    """PostgreSQL 返回带时区的 UTC 时间，列表应按本地时区展示。

    直接把 TZ 固定为 Asia/Shanghai 并给出字面量期望值：此前期望值用同一
    astimezone 规则现算，在 UTC 的 CI 上删掉时区转换仍然通过。Windows 没有
    time.tzset，跳过（CI 在 Linux 上做确定性校验）。
    """
    import time as time_module

    if not hasattr(time_module, "tzset"):
        pytest.skip("当前平台不支持动态时区（time.tzset）")
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time_module.tzset()
    try:
        fake = _FakeStore(
            [_message("user", "问题", "s-utc", "2026-11-04T12:00:00+00:00")]
        )
        monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
        code, out = _run_list("u1")
        assert code == 0
        assert "2026-11-04 20:00" in out, "UTC 12:00 应显示为东八区 20:00"
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time_module.tzset()
    print("✓ UTC 时间戳按固定时区确定性展示")


def test_empty_sessions(monkeypatch):
    fake = _FakeStore([])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
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
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_archive("u1", "s-active", archived=True)
    assert code == 0 and "已归档会话 s-active" in out
    assert fake.archived_calls == [("s-active", True)]

    code, out = _run_archive("u1", "s-active", archived=False)
    assert code == 0 and "已恢复会话 s-active" in out
    assert fake.archived_calls[-1] == ("s-active", False)
    print("✓ session-archive 归档 / 恢复")


def test_archive_missing_session(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_archive("u1", "s-missing", archived=True)
    assert code == 1 and "不存在或没有消息" in out
    assert fake.archived_calls == []
    print("✓ 不存在的会话返回 1")


def test_sessions_updated_at_uses_max_timestamp(monkeypatch):
    """updated_at 取转录最大时间戳：恢复备份把旧消息追加到末尾后不回拨。"""
    store = _FakeStore(
        [
            _message("user", "新问题", "s-max", "2026-11-04T12:00:00"),
            _message("assistant", "回答", "s-max", "2026-11-04T12:01:00"),
            _message("user", "旧消息追加", "s-max", "2026-11-03T08:00:00"),
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
    code, out = _run_list("u1", as_json=True)
    payload = json.loads(out)
    entry = next(
        item for item in payload["sessions"] if item["session_id"] == "s-max"
    )
    assert entry["updated_at"] == "2026-11-04T12:01:00"
    print("✓ sessions updated_at 取最大时间戳")


def test_archive_json_output(monkeypatch):
    import json as _json

    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_archive_standalone(
            "u1", "s-active", archived=True, as_json=True
        )
    payload = _json.loads(buf.getvalue())
    assert code == 0
    assert payload == {
        "ok": True,
        "user_id": "u1",
        "session_id": "s-active",
        "archived": True,
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_archive_standalone(
            "u1", "s-missing", archived=False, as_json=True
        )
    payload = _json.loads(buf.getvalue())
    assert code == 1 and payload["ok"] is False
    assert "不存在或没有消息" in payload["error"]
    print("✓ session-archive --json 成功 / 错误输出")


def test_archive_dispatch_json(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_archive_standalone",
        lambda user_id, session_id, archived=True, as_json=False: (
            calls.append((user_id, session_id, archived, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "session-archive", "u1", "s1", "--restore", "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "s1", False, True)
    print("✓ session-archive dispatch --json")


def _run_delete(user_id, session_id, as_json=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_delete_standalone(user_id, session_id, as_json=as_json)
    return code, buf.getvalue()


def test_session_delete_removes_messages(monkeypatch):
    fake = _sessions_store()
    cleared = []
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        storage,
        "clear_short_term_session",
        lambda sid, uid="": cleared.append((sid, uid)),
    )
    code, out = _run_delete("u1", "s-active")
    assert code == 0 and "已删除会话 s-active" in out and "2 条消息" in out
    assert fake.deleted_sessions == ["s-active"]
    assert fake.messages and all(
        m.get("session_id") != "s-active" for m in fake.messages
    ), "其它会话的消息不能受影响"
    assert cleared == [("s-active", "u1")], "短期记忆须同步清理（Redis 会话键）"
    assert fake.closed
    print("✓ session-delete 删除消息并清理短期记忆")


def test_session_delete_clears_trip_links_for_empty_session(monkeypatch):
    """没有消息但被行程引用的会话：允许删除以清空行程链接（与 Web 一致）。"""
    fake = _sessions_store()
    fake.trips = [
        {"trip_id": "trip_1", "session_id": "s-empty", "origin": "北京",
         "destination": "上海"},
        {"trip_id": "trip_2", "session_id": "s-active", "origin": "杭州",
         "destination": "成都"},
    ]
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)
    code, out = _run_delete("u1", "s-empty")
    assert code == 0 and "已删除会话 s-empty（0 条消息）" in out, out
    assert fake.deleted_sessions == ["s-empty"]
    assert fake.trips[0]["session_id"] is None
    assert fake.trips[1]["session_id"] == "s-active", "其它会话的链接不受影响"
    print("✓ session-delete 可清理仅被行程引用的空会话链接")


def test_session_delete_writes_shared_tombstone(monkeypatch):
    """配置 Redis 时写入与 Web 同键的共享删除墓碑（NX、TTL 与 Web 一致 1 小时）。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)

    class _Redis:
        def __init__(self):
            self.sets = []

        def set(self, key, value, nx=False, ex=None):
            self.sets.append((key, value, nx, ex))
            return True

        def incr(self, key):
            return 1

        def expire(self, key, ttl):
            return 1

        def get(self, key):
            return None

    redis = _Redis()
    monkeypatch.setattr(storage, "get_shared_redis_client", lambda force=False: redis)
    code, out = _run_delete("u1", "s-active", as_json=True)
    assert code == 0
    assert json.loads(out)["shared_tombstone"] is True
    assert len(redis.sets) == 1
    key, value, nx, ex = redis.sets[0]
    assert key == "aligo:deleted:u1:s-active"
    assert value.startswith("cli-") and len(value) > len("cli-"), (
        "墓碑值必须是本进程唯一 token（并发 CLI 删除时归属可区分）"
    )
    assert nx is True and ex == 3600
    print("✓ session-delete 写入共享删除墓碑（唯一 token）")


def test_session_delete_without_shared_tombstone_hints_refresh(monkeypatch):
    """无共享 Redis 时提示刷新其它已打开的 Web 页面。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)
    code, out = _run_delete("u1", "s-active")
    assert code == 0 and "请刷新" in out
    print("✓ session-delete 无共享墓碑时提示刷新")


def test_session_delete_marks_tombstone_before_deleting(monkeypatch):
    """先写共享墓碑再删库：删库与写墓碑之间其它实例的守卫不能漏看。"""
    fake = _sessions_store()
    order = []
    original_delete = fake.delete_chat_session

    def tracked_delete(session_id):
        order.append("delete")
        return original_delete(session_id)

    fake.delete_chat_session = tracked_delete
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)

    from utils import tombstones

    monkeypatch.setattr(
        tombstones,
        "mark_session_deleted",
        lambda uid, sid, **kwargs: (order.append("mark"), True)[1],
    )
    code, out = _run_delete("u1", "s-active")
    assert code == 0
    assert order == ["mark", "delete"], order
    print("✓ session-delete 先写墓碑再删库")


def test_session_delete_failure_revokes_cli_tombstone(monkeypatch):
    """删除失败、会话仍在：撤销本进程写的墓碑恢复可写。"""
    fake = _sessions_store()

    def boom(session_id):
        raise RuntimeError("db down")

    fake.delete_chat_session = boom
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)

    from utils import tombstones

    calls = []
    monkeypatch.setattr(
        tombstones,
        "mark_session_deleted",
        lambda uid, sid, **kwargs: (calls.append("mark"), True)[1],
    )
    monkeypatch.setattr(
        tombstones,
        "unmark_session_deleted",
        lambda uid, sid, **kwargs: (calls.append("unmark"), True)[1],
    )
    code, out = _run_delete("u1", "s-active")
    assert code == 1 and "删除会话失败" in out
    assert calls == ["mark", "unmark"], calls
    print("✓ 删除失败撤销 CLI 墓碑")


def test_session_delete_missing_session(monkeypatch):
    fake = _sessions_store()
    cleared = []
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        storage,
        "clear_short_term_session",
        lambda sid, uid="": cleared.append(sid),
    )
    code, out = _run_delete("u1", "s-missing")
    assert code == 1 and "不存在或没有消息" in out
    assert fake.deleted_sessions == []
    assert cleared == [], "不存在的会话不应触发短期清理"
    print("✓ session-delete 不存在的会话返回 1")


def test_archive_and_pin_report_when_vanished_concurrently(monkeypatch):
    """预检后会话被并发删除（set_session_* 返回 False）：报「操作未生效」退出 1。"""

    class _VanishingStore(_FakeStore):
        def set_session_archived(self, session_id, archived):
            return False

    store = _VanishingStore([_message("user", "问题", "s-gone")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
    code, out = _run_archive("u1", "s-gone", archived=True)
    assert code == 1 and "操作未生效" in out

    class _VanishingPin(_PinStore):
        def set_session_pinned(self, session_id, pinned):
            return False

    pin_store = _VanishingPin([_message("user", "问题", "s-gone")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: pin_store)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_pin_standalone("u1", "s-gone")
    assert code == 1 and "操作未生效" in buf.getvalue()
    print("✓ archive/pin 并发删除后报操作未生效")


def test_session_delete_json_output(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(storage, "clear_short_term_session", lambda sid, uid="": None)
    code, out = _run_delete("u1", "s-active", as_json=True)
    payload = json.loads(out)
    assert code == 0
    assert payload == {
        "ok": True,
        "user_id": "u1",
        "session_id": "s-active",
        "deleted": 2,
        "shared_tombstone": False,
    }

    code, out = _run_delete("u1", "s-active", as_json=True)  # 已删干净
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert "不存在或没有消息" in payload["error"]
    print("✓ session-delete --json 成功 / 错误输出")


def test_session_delete_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_delete_standalone",
        lambda user_id, session_id, as_json=False: (
            calls.append((user_id, session_id, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "session-delete", "u1", "s1", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "s1", True)

    monkeypatch.setattr(sys, "argv", ["cli.py", "session-delete", "u1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    print("✓ session-delete dispatch 与用法错误")


def test_session_delete_failures(monkeypatch):
    fake = _sessions_store()
    fake.fail_history = True
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_delete("u1", "s-active")
    assert code == 1 and "读取会话失败" in out

    fake = _sessions_store()
    fake.fail_delete = True
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_delete("u1", "s-active")
    assert code == 1 and "删除会话失败" in out
    assert fake.closed
    print("✓ session-delete 读取 / 删除失败返回 1")


class _TitleStore(_FakeStore):
    """支持 set_session_title 的假存储（session-title 用例）。"""

    def __init__(self, messages, titles=None, fail_set=False):
        super().__init__(messages, titles=titles)
        self.fail_set = fail_set
        self.title_calls = []

    def set_session_title(self, session_id, title):
        if self.fail_set:
            raise RuntimeError("disk full")
        self.title_calls.append((session_id, title))
        if title:
            self.titles[session_id] = title
        else:
            self.titles.pop(session_id, None)
        return True


def test_session_title_set_and_reset(monkeypatch):
    import json as _json

    store = _TitleStore(
        [_message("user", "内容", "s-active")], titles={"s-active": "旧标题"}
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("u1", "s-active", "新标题")
    assert code == 0 and "已设置会话 s-active 标题：新标题" in out.getvalue()
    assert store.title_calls == [("s-active", "新标题")] and store.closed

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("u1", "s-active", "", as_json=True)
    data = _json.loads(out.getvalue())
    assert code == 0 and data["title"] == ""
    assert store.title_calls[-1] == ("s-active", "")
    print("✓ session-title 设置 / 重置 / JSON")


def test_session_title_validation_and_failures(monkeypatch):
    store = _TitleStore([_message("user", "内容", "s-active")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("u1", "s-active", "长" * 61)
    assert code == 1 and "最多 60" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("u1", "s-missing", "x")
    assert code == 1 and "不存在" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("bad id", "s1", "x")
    assert code == 1 and "用户 ID" in out.getvalue()

    store.fail_set = True
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone(
            "u1", "s-active", "x", as_json=True
        )
    data = json.loads(out.getvalue())
    assert code == 1 and "操作失败" in data["error"]
    print("✓ session-title 校验与写入失败")


def test_session_title_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_title_standalone",
        lambda user_id, session_id, title, as_json=False: (
            calls.append((user_id, session_id, title, as_json)) or 0
        ),
    )
    for argv, expected in (
        (
            ["cli.py", "session-title", "u1", "s1", "新标题"],
            ("u1", "s1", "新标题", False),
        ),
        (
            ["cli.py", "session-title", "u1", "s1", "--reset", "--json"],
            ("u1", "s1", "", True),
        ),
        (
            ["cli.py", "session-title", "u1", "s1", "标题", "--JSON"],
            ("u1", "s1", "标题", True),
        ),
        # 单个 '-' 开头的标题直接可用；'--' 开头需 -- 分隔（与 argparse 同语义）
        (
            ["cli.py", "session-title", "u1", "s1", "- 北京", "--json"],
            ("u1", "s1", "- 北京", True),
        ),
        (
            ["cli.py", "session-title", "u1", "s1", "--", "-- 北京"],
            ("u1", "s1", "-- 北京", False),
        ),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert calls[-1] == expected, argv
    for argv in (
        ["cli.py", "session-title"],
        ["cli.py", "session-title", "u1"],
        ["cli.py", "session-title", "u1", "s1"],
        ["cli.py", "session-title", "u1", "s1", "--reset", "标题"],
        ["cli.py", "session-title", "u1", "s1", "标题", "多余"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "session-title" in out.getvalue()
    print("✓ session-title dispatch 参数")


class _PinStore(_FakeStore):
    """支持 set_session_pinned 的假存储（session-pin 用例）。"""

    def __init__(self, messages, pinned=None, fail_set=False):
        super().__init__(messages, pinned=pinned)
        self.fail_set = fail_set
        self.pin_calls = []

    def set_session_pinned(self, session_id, pinned):
        if self.fail_set:
            raise RuntimeError("disk full")
        self.pin_calls.append((session_id, pinned))
        if pinned and session_id not in self.pinned:
            self.pinned.append(session_id)
        elif not pinned and session_id in self.pinned:
            self.pinned.remove(session_id)
        return True


def test_session_pin_set_and_unset(monkeypatch):
    import json as _json

    store = _PinStore([_message("user", "内容", "s-active")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_pin_standalone("u1", "s-active")
    assert code == 0 and "已置顶会话 s-active" in out.getvalue()
    assert store.pin_calls == [("s-active", True)] and store.closed

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_pin_standalone(
            "u1", "s-active", pinned=False, as_json=True
        )
    data = _json.loads(out.getvalue())
    assert code == 0 and data["pinned"] is False
    assert store.pin_calls[-1] == ("s-active", False)
    print("✓ session-pin 置顶 / 取消 / JSON")


def test_session_pin_validation_and_failures(monkeypatch):
    store = _PinStore([_message("user", "内容", "s-active")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_pin_standalone("u1", "s-missing")
    assert code == 1 and "不存在" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_pin_standalone("bad id", "s1")
    assert code == 1 and "用户 ID" in out.getvalue()

    store.fail_set = True
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_pin_standalone("u1", "s-active", as_json=True)
    data = json.loads(out.getvalue())
    assert code == 1 and "操作失败" in data["error"]
    print("✓ session-pin 校验与写入失败")


def test_session_pin_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_pin_standalone",
        lambda user_id, session_id, pinned=True, as_json=False: (
            calls.append((user_id, session_id, pinned, as_json)) or 0
        ),
    )
    for argv, expected in (
        (["cli.py", "session-pin", "u1", "s1"], ("u1", "s1", True, False)),
        (
            ["cli.py", "session-pin", "u1", "s1", "--off", "--json"],
            ("u1", "s1", False, True),
        ),
        (
            ["cli.py", "session-pin", "u1", "s1", "--JSON"],
            ("u1", "s1", True, True),
        ),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert calls[-1] == expected, argv
    for argv in (
        ["cli.py", "session-pin"],
        ["cli.py", "session-pin", "u1"],
        ["cli.py", "session-pin", "u1", "s1", "extra"],
        ["cli.py", "session-pin", "u1", "s1", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "session-pin" in out.getvalue()
    print("✓ session-pin dispatch 参数")


def test_session_show_text_and_json(monkeypatch):
    import json as _json

    store = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-pinned")
    text = out.getvalue()
    assert code == 0 and store.closed
    assert "会话 s-pinned：（自动标题）（置顶） · 2 条消息" in text
    assert "[2026-11-04 11:00:00] assistant: 回答 B" in text

    store.titles["s-active"] = "住宿咨询"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-active")
    assert "会话 s-active：住宿咨询 · 2 条消息" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-archived", as_json=True)
    data = _json.loads(out.getvalue())
    assert code == 0 and data["archived"] is True
    assert data["messages"][0]["content"] == "归档的问题"
    print("✓ session-show 文本 / JSON")


def test_session_show_renders_assistant_markdown(monkeypatch):
    """助手消息是编排 JSON 时，预览要重新渲染成 Markdown 文本。"""
    import json as _json

    payload = _json.dumps(
        {"status": "no_agents", "results": []}, ensure_ascii=False
    )
    store = _FakeStore([_message("assistant", payload, "s-json")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-json")
    assert code == 0 and "已记录您的需求" in out.getvalue()
    print("✓ session-show 助手消息重新渲染")


def test_session_show_errors(monkeypatch):
    store = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-missing")
    assert code == 1 and "不存在或没有消息" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("bad id", "s1")
    assert code == 1 and "用户 ID" in out.getvalue()

    store.fail_history = True
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-active")
    assert code == 1 and "读取会话失败" in out.getvalue()
    store.fail_history = False

    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "s-active", as_json=True)
    data = json.loads(out.getvalue())
    assert code == 1 and "打开长期记忆失败" in data["error"]
    print("✓ session-show 不存在 / 非法 ID / 读取与打开失败")


def test_session_show_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_show_standalone",
        lambda user_id, session_id, as_json=False: (
            calls.append((user_id, session_id, as_json)) or 0
        ),
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "session-show", "u1", "s1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and calls[-1] == ("u1", "s1", False)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "session-show", "u1", "s1", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and calls[-1] == ("u1", "s1", True)
    for argv in (
        ["cli.py", "session-show"],
        ["cli.py", "session-show", "u1"],
        ["cli.py", "session-show", "u1", "s1", "extra"],
        ["cli.py", "session-show", "u1", "s1", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "session-show" in out.getvalue()
    print("✓ session-show dispatch 参数")


def test_read_failure(monkeypatch):
    fake = _sessions_store()
    fake.fail_history = True
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1")
    assert code == 1 and "读取会话失败" in out
    assert fake.closed
    print("✓ 读取失败返回 1")


def test_store_open_failure(monkeypatch):
    def boom(*args, **kwargs):
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
        storage, "create_long_term_memory", lambda *args, **kwargs: _JsonStore([])
    )
    code, out = _run_list("u1")
    assert code == 1 and "回退本地 JSON" in out
    print("✓ PostgreSQL 回退时拒绝执行")


def test_flag_parsing_helpers():
    assert cli._parse_sessions_flags([]) == (False, False, "", False)
    assert cli._parse_sessions_flags(["--archived"]) == (True, False, "", False)
    assert cli._parse_sessions_flags(["--ALL"]) == (False, True, "", False)
    assert cli._parse_sessions_flags(["--query", "住宿"]) == (
        False,
        False,
        "住宿",
        False,
    )
    assert cli._parse_sessions_flags(["--query=住宿", "--json"]) == (
        False,
        False,
        "住宿",
        True,
    )
    assert cli._parse_sessions_flags(["--json"]) == (False, False, "", True)
    assert cli._parse_sessions_flags(["--query"]) is None
    assert cli._parse_sessions_flags(["--query="]) is None
    assert cli._parse_sessions_flags(["--query", "--all"]) is None
    assert cli._parse_sessions_flags(["--archived=true"]) is None
    assert cli._parse_sessions_flags(["--archivd"]) is None
    assert cli._parse_sessions_flags(["--archived", "--all"]) is None
    assert cli._parse_archive_flags([]) == (False, False)
    assert cli._parse_archive_flags(["--restore"]) == (True, False)
    assert cli._parse_archive_flags(["--json", "--RESTORE"]) == (True, True)
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
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1")
    assert code == 0 and "pg-s1" in out
    assert fake.closed
    print("✓ PostgreSQL 正常时正常执行（仅回退时拒绝）")


def test_save_failure_reports_error(monkeypatch):
    fake = _sessions_store()

    def boom(session_id, archived):
        raise OSError("长期记忆写入失败")

    fake.set_session_archived = boom
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_archive("u1", "s-active", archived=True)
    assert code == 1 and "操作失败" in out
    print("✓ 存储写入失败返回 1")


def test_main_rejects_bad_usage(monkeypatch):
    """dispatch 层：漏写用户 / 未知参数 / --restore=1 均返回用法错误（退出码 2）。"""
    for argv in (
        ["cli.py", "sessions", "--archived"],
        ["cli.py", "sessions", "u1", "--archived=true"],
        ["cli.py", "sessions", "u1", "--archived", "--all"],
        ["cli.py", "session-archive", "u1", "s1", "--restore=1"],
        ["cli.py", "session-archive", "u1", "--restore"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法" in buf.getvalue(), argv
    print("✓ dispatch 层拒绝错误用法（退出码 2）")


def test_main_accepts_valid_flags(monkeypatch):
    """合法参数应进入对应函数（用假 store，避免真实读取）。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(sys, "argv", ["cli.py", "sessions", "u1", "--archived"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "s-archived" in buf.getvalue()
    print("✓ dispatch 层合法参数正常执行")


def _run_export(
    user_id, session_id, target=None, to_stdout=False, force=False, html=False
):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_session_export_standalone(
            user_id,
            session_id,
            target=target,
            to_stdout=to_stdout,
            force=force,
            html=html,
        )
    return code, buf.getvalue()


def test_main_accepts_query_flag(monkeypatch):
    """dispatch 层接受 --query 关键词并传给列表函数。"""
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "sessions", "u1", "--query", "住宿"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "s-active" in buf.getvalue()
    assert "s-pinned" not in buf.getvalue()
    print("✓ dispatch 层 --query 正常执行")


def test_session_export_to_file(monkeypatch):
    import tempfile

    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    target = os.path.join(tempfile.mkdtemp(), "session.md")
    code, out = _run_export("u1", "s-active", target=target)
    assert code == 0 and "已导出会话 s-active" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert "# 住宿标准问题" in text
    assert "## 用户" in text and "住宿标准问题" in text
    assert "## Aligo" in text and "回答 A" in text
    assert fake.closed
    print("✓ session-export 写入 Markdown 文件")


def test_session_export_to_stdout(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_export("u1", "s-pinned", to_stdout=True)
    assert code == 0
    assert "# 行程规划问题" in out and "回答 B" in out
    print("✓ session-export --stdout 输出 Markdown")


def test_session_export_missing_and_invalid(monkeypatch):
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_export("u1", "s-missing")
    assert code == 1 and "不存在或没有消息" in out
    code, out = _run_export("--bad", "s-active")
    assert code == 1 and "用户 ID" in out
    print("✓ session-export 缺失会话 / 非法 ID")


def test_session_export_flag_parsing():
    assert cli._parse_export_args([]) == (None, False, False, False, "", "")
    assert cli._parse_export_args(["out.md"]) == (
        "out.md",
        False,
        False,
        False,
        "",
        "",
    )
    assert cli._parse_export_args(["--stdout"]) == (None, True, False, False, "", "")
    assert cli._parse_export_args(["--force"]) == (None, False, True, False, "", "")
    assert cli._parse_export_args(["out.md", "--force"]) == (
        "out.md",
        False,
        True,
        False,
        "",
        "",
    )
    assert cli._parse_export_args(["out.md", "--stdout"]) is None
    assert cli._parse_export_args(["a.md", "b.md"]) is None
    assert cli._parse_export_args(["--bad"]) is None
    assert cli._parse_export_args(["--ics"]) is None, "会话导出不接受 --ics"
    assert cli._parse_export_args(["--query", "x"]) is None, "会话导出不接受 --query"
    assert cli._parse_export_args(["--status", "past"]) is None, (
        "会话导出不接受 --status"
    )
    print("✓ session-export 参数解析（--stdout / --ics / --query 与路径冲突被拒绝）")


def test_session_export_html(monkeypatch):
    """--html：单文件 HTML、转义注入内容、默认后缀 .html。"""
    import tempfile

    class _HtmlStore(_FakeStore):
        pass

    fake = _HtmlStore(
        [
            _message("user", "<script>alert(1)</script> 与 & 符号", "s-html"),
            _message("assistant", '{"reply": "回答 <b>加粗</b>"}', "s-html", "2026-11-04T10:02:00"),
        ],
        titles={"s-html": "HTML 导出"},
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    target = os.path.join(tempfile.mkdtemp(), "session.html")
    code, out = _run_export("u1", "s-html", target=target, html=True)
    assert code == 0 and "已导出会话 s-html" in out
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert text.startswith("<!doctype html>")
    assert "<title>HTML 导出</title>" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text, "用户内容必须转义"
    assert "<script>alert(1)</script>" not in text
    assert "回答 &lt;b&gt;加粗&lt;/b&gt;" in text
    assert 'class="msg user"' in text and 'class="msg assistant"' in text

    # 默认文件名后缀为 .html（在临时目录内验证）
    monkeypatch.chdir(tempfile.mkdtemp())
    code, out = _run_export("u1", "s-html", html=True)
    assert code == 0
    assert os.path.exists(os.path.join("exports", "aligo_u1_s-html.html"))
    print("✓ session-export --html 转义与默认后缀")


def test_session_export_html_title_not_double_escaped(monkeypatch):
    """自定义标题含尖括号：HTML 导出只转义一次（不能出现 &amp;lt;）。"""
    import tempfile

    fake = _FakeStore(
        [_message("user", "你好", "s-title")],
        titles={"s-title": "预算<5000 对比 A>B"},
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    target = os.path.join(tempfile.mkdtemp(), "session.html")
    code, out = _run_export("u1", "s-title", target=target, html=True)
    assert code == 0
    with open(target, encoding="utf-8") as handle:
        text = handle.read()
    assert "<title>预算&lt;5000 对比 A&gt;B</title>" in text
    assert "&amp;lt;" not in text and "&amp;gt;" not in text, "标题只应转义一次"

    # Markdown 导出仍按单次转义显示（与 sessions-export / Web 同口径）
    code, out = _run_export("u1", "s-title", to_stdout=True)
    assert code == 0
    assert "# 预算&lt;5000 对比 A&gt;B" in out
    assert "预算<5000" not in out
    print("✓ session-export HTML 标题单次转义 / Markdown 保持转义")


def test_session_export_html_flag_parsing():
    assert cli._parse_session_export_args([]) == (None, False, False, False)
    assert cli._parse_session_export_args(["out.html"]) == (
        "out.html",
        False,
        False,
        False,
    )
    assert cli._parse_session_export_args(["--html"]) == (None, False, False, True)
    assert cli._parse_session_export_args(["--stdout", "--html"]) == (
        None,
        True,
        False,
        True,
    )
    assert cli._parse_session_export_args(["out.html", "--stdout"]) is None
    assert cli._parse_session_export_args(["a", "b"]) is None
    assert cli._parse_session_export_args(["--bad"]) is None
    print("✓ session-export --html 参数解析")


def test_session_export_html_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_session_export_standalone",
        lambda user_id, session_id, target=None, to_stdout=False, force=False, html=False: (
            calls.append((user_id, session_id, target, to_stdout, force, html)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "session-export", "u1", "s1", "out.html", "--html"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "s1", "out.html", False, False, True)
    print("✓ session-export dispatch --html")


def test_session_export_write_failure_reports(monkeypatch):
    """父路径被文件占用时 session-export 写入失败明确报错。"""
    import tempfile

    fake = _FakeStore([_message("user", "问题", "s-exp")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: fake)
    blocker = os.path.join(tempfile.mkdtemp(), "blocker")
    with io.open(blocker, "w", encoding="utf-8") as handle:
        handle.write("x")
    code, out = _run_export("u1", "s-exp", target=os.path.join(blocker, "out.md"))
    assert code == 1 and "写入失败" in out
    print("✓ session-export 写入失败友好报错")


def test_session_export_refuses_overwrite(monkeypatch):
    import tempfile

    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    target = os.path.join(tempfile.mkdtemp(), "existing.md")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("旧内容")

    code, out = _run_export("u1", "s-active", target=target)
    assert code == 1 and "文件已存在" in out
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "旧内容", "未加 --force 不应覆盖"

    code, out = _run_export("u1", "s-active", target=target, force=True)
    assert code == 0
    with open(target, encoding="utf-8") as handle:
        assert "住宿标准问题" in handle.read()
    print("✓ 默认拒绝覆盖已有文件，--force 可覆盖")


def test_session_export_default_path(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_export("u1", "s-active")
    assert code == 0
    path = os.path.join(workdir, "exports", "aligo_u1_s-active.md")
    assert os.path.exists(path)
    print("✓ 未指定路径时写入 exports/aligo_<user>_<session>.md")


def test_main_session_export_dispatch(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    fake = _sessions_store()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "session-export", "u1", "s-active", "--force"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.exists(os.path.join(workdir, "exports", "aligo_u1_s-active.md"))
    print("✓ dispatch 层 session-export 正常执行")


def test_interactive_user_id_validation(monkeypatch):
    answers = iter(["../etc/passwd", "-bad", "alice"])
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: next(answers))

    # 模拟交互终端：非 TTY（管道 / pytest 捕获）下首个非法行会直接退出，
    # 不再把后续行当作重试输入（避免把管道里的查询数据当成用户 ID）
    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _TTY())
    instance = cli.AligoCLI()
    with contextlib.redirect_stdout(io.StringIO()):
        assert instance._prompt_user_id() == "alice"
    print("✓ 交互式用户 ID 校验（拒绝路径穿越 / 短横线开头）")


def test_session_title_ignores_stale_false(monkeypatch):
    """存在性预检后会话被并发删除：set_session_* 返回 False 不能报成功。"""
    store = _TitleStore(
        [_message("user", "内容", "s-active")], titles={"s-active": "旧标题"}
    )

    def stale(self, session_id, title):
        return False

    monkeypatch.setattr(_TitleStore, "set_session_title", stale)
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_title_standalone("u1", "s-active", "新标题")
    assert code == 1 and "未生效" in out.getvalue()
    print("✓ 会话操作检查 set_session_* 返回值")


def test_session_export_sanitizes_custom_title(monkeypatch):
    """自定义标题的换行 / HTML 不能伪造导出结构或原样落盘。"""
    fake = _sessions_store()
    fake.titles["s-active"] = "正常标题\n# 伪造标题 <script>"
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_export("u1", "s-active", to_stdout=True)
    assert code == 0
    assert "# 正常标题 # 伪造标题 &lt;script&gt;" in out
    assert "\n# 伪造标题" not in out
    assert "<script>" not in out
    print("✓ session-export 标题单行化并转义")


def test_sessions_query_matches_rendered_assistant_content(monkeypatch):
    """--query 用渲染后的助手正文匹配：原始 JSON 的字段名不算命中。"""
    import json as json_module

    fake = _sessions_store()
    rendered = json_module.dumps(
        {
            "status": "completed",
            "results": [
                {
                    "agent_name": "rag_knowledge",
                    "priority": 1,
                    "status": "success",
                    "data": {"answer": "推荐入住汉庭酒店"},
                }
            ],
        },
        ensure_ascii=False,
    )
    fake.messages = [
        {
            "role": "user",
            "content": "住哪里",
            "session_id": "s-json",
            "timestamp": "2026-11-01T00:00:00",
        },
        {
            "role": "assistant",
            "content": rendered,
            "session_id": "s-json",
            "timestamp": "2026-11-01T00:00:01",
        },
    ]
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_list("u1", query="汉庭")
    assert code == 0 and "s-json" in out
    code, out = _run_list("u1", query="status")
    assert code == 0 and "没有匹配" in out, "原始 JSON 字段名不应算命中"
    print("✓ sessions --query 匹配渲染后的助手正文")

def test_sessions_export_counts_only_rendered_messages(monkeypatch, tmp_path):
    """消息数按实际渲染条数统计（无 session_id 的旧消息不计入、不导出）。"""
    from pathlib import Path

    fake = _sessions_store()
    fake.messages.append(_message("user", "旧版无会话消息", ""))
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    buf = io.StringIO()
    path = Path(tmp_path) / "all.md"
    with contextlib.redirect_stdout(buf):
        code = cli.run_sessions_export_standalone("u1", str(path), force=True)
    assert code == 0, buf.getvalue()
    out = buf.getvalue()
    assert "已导出 3 个会话" in out, out
    assert "（6 条消息）" in out, out
    content = path.read_text(encoding="utf-8")
    assert "旧版无会话消息" not in content
    print("✓ sessions-export 消息数与导出内容一致")


def test_sessions_export_without_sessions_returns_1(monkeypatch, tmp_path):
    """只有无 session_id 的旧消息：不写空文件、返回 1（README 约定）。"""
    from pathlib import Path

    fake = _FakeStore([_message("user", "旧版无会话消息", "")])
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    buf = io.StringIO()
    path = Path(tmp_path) / "empty.md"
    with contextlib.redirect_stdout(buf):
        code = cli.run_sessions_export_standalone("u1", str(path), force=True)
    assert code == 1, buf.getvalue()
    assert "没有可导出的会话" in buf.getvalue()
    assert not path.exists()
    print("✓ 只有无会话旧消息时返回 1 且不写空文件")


def test_session_export_stdout_strips_terminal_controls(monkeypatch):
    """单会话 --stdout 不把存储内容里的 ESC / OSC 序列送达终端。"""
    fake = _FakeStore(
        [
            _message("user", "before\x1b]52;c;SGVsbG8=\x07after", "s-esc"),
            _message("assistant", "回答", "s-esc"),
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    code, out = _run_export("u1", "s-esc", to_stdout=True)
    assert code == 0
    assert "\x1b]" not in out
    assert "before" in out and "after" in out
    print("✓ session-export --stdout 剥离终端控制序列")


def test_sessions_export_stdout_strips_terminal_controls(monkeypatch):
    """全部会话 --stdout 同样剥离终端控制序列。"""
    fake = _FakeStore(
        [
            _message("user", "before\x1b]52;c;SGVsbG8=\x07after", "s-esc"),
            _message("assistant", "回答", "s-esc"),
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: fake)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_sessions_export_standalone("u1", to_stdout=True)
    assert code == 0
    assert "\x1b]" not in buf.getvalue()
    print("✓ sessions-export --stdout 剥离终端控制序列")


def test_session_views_strip_terminal_controls(monkeypatch):
    """标题 / 消息预览 / 回读标题里的 OSC 序列不能直达终端。"""
    evil_title = "标题\x1b]8;;http://evil.example\x07链接"
    evil_body = "内容\x1b]52;c;SGVsbG8=\x07尾部"

    class _TitleStore(_FakeStore):
        def set_session_title(self, session_id, title):
            self.titles[session_id] = title
            return True

    store = _TitleStore(
        [_message("assistant", evil_body, "s-esc")],
        titles={"s-esc": evil_title},
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    outputs = []
    for func in (
        lambda: cli.run_sessions_list_standalone("u1"),
        lambda: cli.run_session_show_standalone("u1", "s-esc"),
        lambda: cli.run_session_title_standalone("u1", "s-esc", evil_title),
    ):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func()
        assert code == 0, out.getvalue()
        outputs.append(out.getvalue())
    joined = "".join(outputs)
    assert "\x1b]" not in joined and "\x07" not in joined, joined
    print("✓ 会话标题 / 消息预览 / 回读标题剥离终端控制序列")

def test_session_show_allows_device_like_session_id(monkeypatch):
    """Web 能创建的 nul / con 会话在 CLI 也能查看；用户 ID 仍拒绝设备名。"""
    store = _FakeStore([_message("user", "hi", "nul")], titles={"nul": "设备名"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("u1", "nul")
    assert code == 0 and "设备名" in out.getvalue(), out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_session_show_standalone("nul", "s1")
    assert code == 1 and "用户 ID" in out.getvalue()
    print("✓ 会话 ID 允许设备名，用户 ID 仍拒绝")

def test_session_title_newline_cannot_forge_rows(monkeypatch):
    """标题里的换行不能伪造会话列表 / 详情里的行。"""
    fake_title = "标题Z\n  [置顶] fake  888 条"
    store = _FakeStore(
        [_message("user", "hi", "s-forge")], titles={"s-forge": fake_title}
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    for call in (
        lambda: cli.run_sessions_list_standalone("u1"),
        lambda: cli.run_session_show_standalone("u1", "s-forge"),
    ):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = call()
        text = out.getvalue()
        assert code == 0, text
        assert "\n  [置顶] fake" not in text, text
        assert "\n  [置顶]" not in text, text
        assert "标题Z" in text
    print("✓ 会话标题内嵌换行不能伪造列表行")

def _legacy_session_summaries(store):
    """改动前的实现（逐字复刻）：用于对齐新实现的无搜索 / 无命中输出。"""
    from utils.stored_message import render_stored_message
    from utils.timestamps import chronological_key

    history = store.get_chat_history()
    titles = store.get_session_titles() or {}
    pinned = {str(item) for item in (store.get_pinned_sessions() or [])}
    archived = {str(item) for item in (store.get_archived_sessions() or [])}
    grouped: dict[str, dict] = {}
    for message in history:
        session_id = message.get("session_id")
        if not session_id:
            continue
        sid = str(session_id)
        entry = grouped.setdefault(
            sid,
            {"session_id": sid, "title": "", "messages": 0, "updated_at": ""},
        )
        entry["messages"] += 1
        timestamp = str(message.get("timestamp") or "")
        if timestamp and (
            not entry["updated_at"]
            or chronological_key(timestamp)
            >= chronological_key(entry["updated_at"])
        ):
            entry["updated_at"] = timestamp
        if not entry["title"] and message.get("role") == "user":
            title = " ".join(str(message.get("content", "")).split())
            if title:
                entry["title"] = title[:40]
        if message.get("role") == "user":
            raw_text = str(message.get("content", ""))
        else:
            raw_text = render_stored_message(message)
        content = " ".join(raw_text.split())
        if content:
            entry.setdefault("_parts", []).append(content)
    for entry in grouped.values():
        entry["title"] = str(
            titles.get(entry["session_id"]) or entry["title"] or "新对话"
        )
        entry["pinned"] = entry["session_id"] in pinned
        entry["archived"] = entry["session_id"] in archived
        entry["_parts"] = [entry["title"], *entry.pop("_parts", [])]
    return sorted(
        grouped.values(),
        key=lambda item: chronological_key(item["updated_at"]),
        reverse=True,
    )


def test_session_summaries_matches_legacy_and_defers_search_text(monkeypatch):
    """新聚合与旧实现逐字段一致；无搜索不渲染助手消息，搜索命中即停。"""
    assistant = json.dumps(
        {"results": ["回答甲包含关键词"], "_meta": {}}, ensure_ascii=False
    )
    messages = [
        _message("user", "北京出差", "s1", "2026-11-04T10:00:00"),
        _message("assistant", assistant, "s1", "2026-11-04T11:00:00"),
        _message("user", "第二问（旧消息排末尾）", "s1", "2026-11-03T09:00:00"),
        _message("user", "上海", "s2", "2026-11-02 08:00:00"),
        _message("user", "无效时间", "s2", "not-a-time"),
        _message("user", "", "s2", ""),
        _message("user", "无会话", "", "2026-11-05T00:00:00"),
    ]
    store = _FakeStore(
        messages, titles={"s1": "自定义标题"}, pinned=["s2"], archived=["s9"]
    )

    def _display(entries):
        # 旧实现总带 _parts（搜索文本）；无搜索的新实现不构建，比较展示字段
        return [
            {key: value for key, value in item.items() if key != "_parts"}
            for item in entries
        ]

    legacy = _legacy_session_summaries(store)
    fresh = cli._session_summaries(store)
    assert fresh == _display(legacy)
    assert all("_parts" not in item for item in fresh), "无搜索不构建匹配文本"

    rendered = []
    import utils.stored_message as stored_message

    real = stored_message.render_stored_message

    def _counting(message):
        rendered.append(1)
        return real(message)

    monkeypatch.setattr(stored_message, "render_stored_message", _counting)
    assert cli._session_summaries(store) == _display(legacy)
    assert rendered == [], "无搜索时不渲染助手消息"

    def _hits(entries, needle):
        found = {}
        for item in entries:
            hit = next(
                (part for part in item.get("_parts", []) if needle in part.lower()),
                None,
            )
            if hit is not None:
                found[item["session_id"]] = hit
        return found

    searched = cli._session_summaries(store, needle="关键词")
    assert rendered, "搜索时才渲染助手消息"
    assert _hits(searched, "关键词") == _hits(legacy, "关键词")
    # 无命中时仍需全量扫描：匹配文本与旧实现完全一致
    missing = cli._session_summaries(store, needle="不存在的关键词")
    assert missing == _legacy_session_summaries(store)
    print("✓ 会话摘要新实现与旧实现一致（搜索文本按需构建）")


def test_session_summaries_equal_instant_keeps_later_original():
    """等 chronological_key 的不同原始串：updated_at 仍取后一条原文（>= 语义）。"""
    store = _FakeStore(
        [
            _message("user", "a", "s", "2026-11-04T10:00:00+08:00"),
            _message("user", "b", "s", "2026-11-04T02:00:00+00:00"),
        ]
    )
    summaries = cli._session_summaries(store)
    assert summaries[0]["updated_at"] == "2026-11-04T02:00:00+00:00"
    print("✓ 等时刻时间戳保留后一条原文")


if __name__ == "__main__":
    # 用例常用 tempfile.mkdtemp() 自建记忆目录：直跑没有 pytest 的
    # conftest 回收，登记后在进程退出时统一清理
    import _tmp_registry

    _tmp_registry.install()
    # 直跑模式自动发现全部用例（monkeypatch 自动提供；pytest.skip 条件
    # 跳过不再中断整体，跳过数量明确报告）
    import asyncio
    import inspect
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError
    # 把清单截断在第一个用例；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    import pathlib
    import shutil
    import tempfile

    ran = 0
    skipped = 0
    skipped_names: list[str] = []
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        fixture = None
        kwargs: dict = {}
        tmp_dir_paths: list[pathlib.Path] = []
        unsupported = False
        for param, spec in params.items():
            if param == "monkeypatch":
                fixture = pytest.MonkeyPatch()
                kwargs[param] = fixture
            elif param == "tmp_path":
                _tmp_dir = pathlib.Path(tempfile.mkdtemp())
                tmp_dir_paths.append(_tmp_dir)
                kwargs[param] = _tmp_dir
            else:
                unsupported = True
                break
        if unsupported:
            if fixture is not None:
                fixture.undo()
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
            skipped += 1
            skipped_names.append(f"{name}（需 pytest 专用夹具）")
            continue
        # helper 导入必须在兜底 try 之外：_direct_run_kb 缺失 / 隔离装配
        # 失败要显式中止，不能被「user_import 不可用」的兜底吞掉后
        # 静默跑真实知识库
        import _direct_run_kb

        kb_fixture: pytest.MonkeyPatch | None = None
        try:
            from utils import user_import as _ui_for_kb

            kb_fixture = pytest.MonkeyPatch()
            kb_fixture.setattr(
                _ui_for_kb, "_existing_knowledge_rows", lambda: []
            )
            # 预览的知识可用性探针会加载真实嵌入模型：直跑默认 no-op
            kb_fixture.setattr(
                _ui_for_kb, "_knowledge_ready_probe", lambda: None
            )
        except Exception:  # noqa: BLE001 - 模块不可用时不补丁
            if kb_fixture is not None:
                kb_fixture.undo()
            kb_fixture = None
        else:
            # 与 pytest conftest 同口径：规划 / 布局扫描也不能打开真实
            # Milvus（与 CI「无库」结论一致）
            _direct_run_kb.isolate(kb_fixture)
        try:
            # async def 用例同样要真正执行：直接调用只创建 coroutine，
            # 断言从不运行，直跑清单会给出假绿
            if inspect.iscoroutinefunction(func):
                asyncio.run(func(**kwargs))
            else:
                func(**kwargs)
            ran += 1
        except pytest.skip.Exception as exc:
            skipped += 1
            skipped_names.append(f"{name}（条件跳过：{exc}）")
        finally:
            if fixture is not None:
                fixture.undo()
            if kb_fixture is not None:
                kb_fixture.undo()
            # 直跑模式自建的临时目录要自己清：否则每跑一次清单就在系统
            # 临时目录里留下一批带记忆文件的目录
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
    total = sum(
        1
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    if skipped_names:
        print("直跑跳过：" + "、".join(skipped_names))
    if skipped:
        print(
            f"\n执行完成（{ran} 个通过；{skipped} 个按条件跳过 / "
            "需 pytest 专用夹具）"
        )
    else:
        print(f"\n全部通过（{ran}/{total} 个用例）")
