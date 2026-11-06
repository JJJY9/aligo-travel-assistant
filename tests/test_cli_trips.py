"""
`python cli.py trips` 测试（无需 API Key、不访问网络）

覆盖：行程列表输出（状态 / 重叠 / 顺序）、无行程、读取 / 打开失败、
非法用户与严格参数解析，以及 Web / CLI 共用的行程日期工具。

运行：python tests/test_cli_trips.py
"""
import contextlib
import io
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage
from utils.trip_conflicts import trip_conflicts, trip_interval, trip_state_label, trips_with_conflicts


class _FakeStore:
    def __init__(self, trips):
        self.trips = list(trips)
        self.fail_history = False
        self.closed = False

    def get_trip_history(self, limit=None):
        if self.fail_history:
            raise RuntimeError("db down")
        return list(self.trips)

    def close(self):
        self.closed = True


def test_trips_list_json(monkeypatch):
    """--json 输出原始行程（含状态标签与冲突列表）。"""
    trips = [
        _trip("上海", "2020-01-01", "2020-01-02", trip_id="trip_a"),
        _trip("上海", "2020-01-02", "2020-01-03", trip_id="trip_b"),
    ]
    store = _FakeStore(trips)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u-trips", as_json=True)
    assert code == 0
    data = json.loads(out.getvalue())
    assert data["user_id"] == "u-trips" and data["count"] == 2
    first = next(item for item in data["trips"] if item["trip_id"] == "trip_a")
    assert first["destination"] == "上海"
    assert first["state"] == "已结束"
    assert first.get("conflicts"), "重叠行程应带冲突列表"

    empty = _FakeStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: empty
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u-trips", as_json=True)
    assert code == 0 and json.loads(out.getvalue())["trips"] == []
    print("✓ trips --json 输出状态 / 冲突 / 空列表")


def _trip(destination, start, end=None, origin="北京", purpose="出差", trip_id=None):
    item = {
        "trip_id": trip_id or f"trip_{destination}",
        "origin": origin,
        "destination": destination,
        "start_date": start,
        "purpose": purpose,
    }
    if end is not None:
        item["end_date"] = end
    return item


def _run_list(monkeypatch, store):
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u-trips")
    return code, out.getvalue()


def _dated_trips():
    today = datetime.now().date()
    return [
        _trip(
            "上海",
            (today - timedelta(days=30)).isoformat(),
            purpose="培训",
        ),
        _trip(
            "杭州",
            (today - timedelta(days=1)).isoformat(),
            (today + timedelta(days=1)).isoformat(),
        ),
        _trip(
            "南京",
            today.isoformat(),
            (today + timedelta(days=2)).isoformat(),
        ),
        _trip(
            "成都",
            (today + timedelta(days=5)).isoformat(),
            purpose="客户拜访",
        ),
    ]


def test_trip_interval_and_state(monkeypatch):
    past = _trip("上海", "2020-01-01", "2020-01-02")
    bad_end = _trip("杭州", "2020-02-01", "2020-01-01")
    missing = _trip("南京", "")
    interval = trip_interval(past)
    assert interval is not None and interval[0].date() == date(2020, 1, 1)
    bad = trip_interval(bad_end)
    assert bad is not None and bad[1].date() == date(2020, 2, 1), "结束早于出发按单日"
    assert trip_interval(missing) is None
    assert trip_state_label(past) == "已结束"
    today = date(2026, 11, 5)
    assert trip_state_label(_trip("上海", "2026-11-04", "2026-11-06"), today) == "进行中"
    assert trip_state_label(_trip("上海", "2026-11-06"), today) == "即将出行"
    assert trip_state_label(missing) == ""
    print("✓ trip_interval / trip_state_label")


def test_trips_with_conflicts_uses_pool_without_mutation(monkeypatch):
    trips = [
        _trip("上海", "2026-12-01", "2026-12-03"),
        _trip("杭州", "2026-12-02", "2026-12-04"),
        _trip("南京", "2026-12-10"),
    ]
    views = trips_with_conflicts([trips[0]], pool=trips)
    assert [item["destination"] for item in views[0]["conflicts"]] == ["杭州"]
    assert "conflicts" not in trips[0], "不能修改传入的行程字典"
    start = datetime(2026, 12, 2)
    found = trip_conflicts(trips, start, start)
    assert [item["destination"] for item in found] == ["上海", "杭州"]
    assert trip_conflicts(trips, start, start, exclude_id="trip_杭州") == [trips[0]]
    print("✓ trips_with_conflicts / trip_conflicts")


def test_trips_list_output(monkeypatch):
    store = _FakeStore(_dated_trips())
    code, out = _run_list(monkeypatch, store)
    assert code == 0
    assert store.closed
    assert "共 4 条行程" in out
    assert "进行中" in out and "即将出行" in out and "已结束" in out
    # 记录顺序为新→旧输出：成都（最新）在最前，上海最后
    routes = [line for line in out.splitlines() if " → " in line]
    assert len(routes) == 4
    assert "北京 → 成都" in routes[0]
    assert "北京 → 南京" in routes[1]
    assert "北京 → 杭州" in routes[2]
    assert "北京 → 上海" in routes[3]
    # 杭州与南京日期重叠：双方都应有重叠提示
    assert routes[1].count("日期重叠") == 1
    assert routes[2].count("日期重叠") == 1
    assert "培训" in out and "客户拜访" in out
    print("✓ trips 列表输出（顺序 / 状态 / 重叠）")


def test_trips_list_empty(monkeypatch):
    code, out = _run_list(monkeypatch, _FakeStore([]))
    assert code == 0
    assert "还没有行程记录" in out
    print("✓ trips 空列表")


def test_trips_list_read_failure(monkeypatch):
    store = _FakeStore([])
    store.fail_history = True
    code, out = _run_list(monkeypatch, store)
    assert code == 1
    assert "读取行程失败" in out
    assert store.closed
    print("✓ trips 读取失败返回 1")


def test_trips_list_open_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u-trips")
    assert code == 1
    assert "打开长期记忆失败" in out.getvalue()
    print("✓ trips 打开存储失败返回 1")


def test_trips_list_rejects_bad_user_id(monkeypatch):
    for user_id in ("bad id", "-u1", "a" * 65):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run_trips_list_standalone(user_id)
        assert code == 1
        assert "用户 ID" in out.getvalue()
    print("✓ trips 非法用户 ID")


def test_trips_dispatch_json_flexible(monkeypatch):
    """--json 位置灵活、大小写不敏感、可重复。"""
    fake = _FakeStore([_trip("上海", "2026-12-05", "2026-12-07")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: fake
    )
    for argv in (
        ["cli.py", "trips", "--json", "u1"],
        ["cli.py", "trips", "u1", "--json", "--json"],
        ["cli.py", "trips", "u1", "--JSON"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert '"trips"' in buf.getvalue(), argv
    print("✓ dispatch 层 trips --json 位置 / 大小写 / 重复")


def test_trips_dispatch_strict(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_trips_list_standalone",
        lambda user, **kwargs: calls.append(user) or 0,
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "trips", "u1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == ["u1"]

    for argv in (
        ["cli.py", "trips"],
        ["cli.py", "trips", "-u1"],
        ["cli.py", "trips", "bad id"],
        ["cli.py", "trips", "u1", "extra"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py trips <user_id>" in out.getvalue()
    print("✓ trips 严格参数解析")


class _AddStore(_FakeStore):
    """支持 save_trip_history 的假存储（trip-add 用例）。"""

    def __init__(self, trips=()):
        super().__init__(trips)
        self.saved = []

    def save_trip_history(self, record):
        self.saved.append(dict(record))
        trip_id = f"trip_{len(self.trips) + 1}"
        self.trips.append({**record, "trip_id": trip_id})
        return trip_id


def _run_trip_add(*args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trip_add_standalone(*args, **kwargs)
    return code, out.getvalue()


def test_trip_add_saves_and_reports_conflicts(monkeypatch):
    store = _AddStore([_trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add(
        "u1", "北京", "上海", "2026-12-02", "2026-12-04", "出差"
    )
    assert code == 0
    assert "已添加行程 #trip_2" in out
    assert "北京 → 上海" in out and "2026-12-02 ~ 2026-12-04" in out
    assert "与 1 条行程日期重叠：#trip_1" in out
    assert store.saved[0]["destination"] == "上海"
    assert store.saved[0]["purpose"] == "出差"
    assert store.closed
    print("✓ trip-add 保存并提示日期重叠")


def test_trip_add_json_output(monkeypatch):
    store = _AddStore([_trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add(
        "u1", "北京", "杭州", "2026-12-02", None, as_json=True
    )
    data = json.loads(out)
    assert code == 0
    assert data["ok"] is True and data["created"] is True
    assert data["trip_id"] == "trip_2"
    assert data["conflicts"][0]["trip_id"] == "trip_1"
    print("✓ trip-add --json 输出")


def test_trip_add_dedupes_same_route_and_dates(monkeypatch):
    """同路线同日期视为同一趟行程：第二次保存更新既有记录而不是追加。"""
    import tempfile

    from context.long_term_memory import LongTermMemory

    store = LongTermMemory("u-trip-add", tempfile.mkdtemp())
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add("u-trip-add", "北京", "上海", "2026-12-01", None)
    assert code == 0 and "已添加行程" in out
    code, out = _run_trip_add(
        "u-trip-add", "北京", "上海", "2026-12-01", None, "会议"
    )
    assert code == 0 and "已更新行程" in out
    assert len(store.get_trip_history(limit=None)) == 1
    print("✓ trip-add 同路线同日期更新既有记录")


def test_trip_add_validation_errors(monkeypatch):
    store = _AddStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    cases = [
        (("bad id", "北京", "上海", "2026-12-01"), "用户 ID"),
        (("u1", "北京", "上海", "2026/12/01"), "出发日期格式"),
        (("u1", "北京", "上海", "2026-12-01", "2026-13-01"), "返程日期格式"),
        (("u1", "北京", "上海", "2026-12-05", "2026-12-01"), "不能早于"),
    ]
    for args, needle in cases:
        code, out = _run_trip_add(*args)
        assert code == 1 and needle in out, (args, out)
    assert store.saved == [], "校验失败不应写入"
    print("✓ trip-add 校验错误（ID / 日期格式 / 结束早于开始）")


def test_trip_add_store_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run_trip_add(
        "u1", "北京", "上海", "2026-12-01", as_json=True
    )
    data = json.loads(out)
    assert code == 1 and data["ok"] is False
    assert "打开长期记忆失败" in data["error"]

    class _FailSave(_AddStore):
        def save_trip_history(self, record):
            raise RuntimeError("disk full")

    store = _FailSave([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add("u1", "北京", "上海", "2026-12-01")
    assert code == 1 and "保存行程失败" in out
    print("✓ trip-add 打开 / 保存失败")


def test_trip_add_args_and_dispatch(monkeypatch):
    assert cli._parse_trip_add_args([]) is None
    assert cli._parse_trip_add_args(["u1"]) is None
    assert cli._parse_trip_add_args(["u1", "--destination", "上海"]) is None
    assert (
        cli._parse_trip_add_args(["u1", "--destination", "上海", "--start"])
        is None
    )
    assert (
        cli._parse_trip_add_args(
            ["u1", "--destination", "上海", "--start", "2026-12-01", "--bad"]
        )
        is None
    )
    assert (
        cli._parse_trip_add_args(
            ["u1", "u2", "--destination", "上海", "--start", "2026-12-01"]
        )
        is None
    )
    parsed = cli._parse_trip_add_args(
        [
            "--json",
            "u1",
            "--destination=上海",
            "--start",
            "2026-12-01",
            "--origin",
            "北京",
            "--end",
            "2026-12-02",
            "--purpose",
            "会议",
        ]
    )
    assert parsed == {
        "user_id": "u1",
        "values": {
            "destination": "上海",
            "start": "2026-12-01",
            "origin": "北京",
            "end": "2026-12-02",
            "purpose": "会议",
        },
        "as_json": True,
    }

    calls = []
    monkeypatch.setattr(
        cli,
        "run_trip_add_standalone",
        lambda user_id, origin, destination, start, end=None, purpose="出差", as_json=False: (
            calls.append((user_id, origin, destination, start, end, purpose, as_json))
            or 0
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli.py",
            "trip-add",
            "u1",
            "--destination",
            "上海",
            "--start",
            "2026-12-01",
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "", "上海", "2026-12-01", None, "出差", True)

    for argv in (
        ["cli.py", "trip-add"],
        ["cli.py", "trip-add", "u1", "--start", "2026-12-01"],
        [
            "cli.py",
            "trip-add",
            "u1",
            "--destination",
            "上海",
            "--start",
            "2026-12-01",
            "--bad",
        ],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "trip-add" in out.getvalue()
    print("✓ trip-add 参数解析与 dispatch")


class _DeleteStore(_FakeStore):
    """支持 delete_trip 的假存储（trip-delete 用例）。"""

    def __init__(self, trips=(), fail_delete=False):
        super().__init__(trips)
        self.fail_delete = fail_delete
        self.deleted = []

    def delete_trip(self, trip_id):
        if self.fail_delete:
            raise RuntimeError("disk error")
        for index, trip in enumerate(self.trips):
            if str(trip.get("trip_id")) == trip_id:
                self.deleted.append(trip_id)
                self.trips.pop(index)
                return True
        return False


def _run_trip_delete(*args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trip_delete_standalone(*args, **kwargs)
    return code, out.getvalue()


def test_trip_delete_success_and_json(monkeypatch):
    store = _DeleteStore(
        [
            _trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1"),
            _trip("杭州", "2026-12-05", trip_id="trip_2"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_delete("u1", "trip_1")
    assert code == 0
    assert "已删除行程 #trip_1" in out and "北京 → 上海" in out
    assert "2026-12-01 ~ 2026-12-03" in out
    assert store.deleted == ["trip_1"] and store.closed

    store2 = _DeleteStore([_trip("上海", "2026-12-01", trip_id="trip_9")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store2
    )
    code, out = _run_trip_delete("u1", "trip_9", as_json=True)
    data = json.loads(out)
    assert code == 0 and data["deleted"] is True and data["trip_id"] == "trip_9"
    assert data["destination"] == "上海"
    print("✓ trip-delete 文本 / JSON 删除")


def test_trip_delete_not_found_and_ambiguous(monkeypatch):
    store = _DeleteStore([_trip("上海", "2026-12-01", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_delete("u1", "trip_missing")
    assert code == 1 and "不存在" in out and store.deleted == []

    ambiguous = _DeleteStore(
        [
            _trip("上海", "2026-12-01", trip_id="trip_dup"),
            _trip("杭州", "2026-12-02", trip_id="trip_dup"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: ambiguous
    )
    code, out = _run_trip_delete("u1", "trip_dup")
    assert code == 1 and "trips-dedupe" in out
    assert ambiguous.deleted == [], "歧义 id 不能删除"
    print("✓ trip-delete 不存在 / 歧义 id")


def test_trip_delete_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run_trip_delete("u1", "trip_1", as_json=True)
    data = json.loads(out)
    assert code == 1 and "打开长期记忆失败" in data["error"]

    read_fail = _DeleteStore([])
    read_fail.fail_history = True
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: read_fail
    )
    code, out = _run_trip_delete("u1", "trip_1")
    assert code == 1 and "读取行程失败" in out

    fail_delete = _DeleteStore(
        [_trip("上海", "2026-12-01", trip_id="trip_1")], fail_delete=True
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: fail_delete
    )
    code, out = _run_trip_delete("u1", "trip_1")
    assert code == 1 and "删除行程失败" in out
    print("✓ trip-delete 打开 / 读取 / 删除失败")


def test_trip_delete_args_and_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_trip_delete_standalone",
        lambda user_id, trip_id, as_json=False: (
            calls.append((user_id, trip_id, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trip-delete", "u1", "trip_1", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "trip_1", True)
    for argv in (
        ["cli.py", "trip-delete"],
        ["cli.py", "trip-delete", "u1"],
        ["cli.py", "trip-delete", "u1", "trip_1", "extra"],
        ["cli.py", "trip-delete", "u1", "trip_1", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "trip-delete" in out.getvalue()
    print("✓ trip-delete dispatch 参数")


class _UpdateStore(_FakeStore):
    """支持 update_trip 的假存储（trip-update 用例）。"""

    def __init__(self, trips=(), update_error=None):
        super().__init__(trips)
        self.update_error = update_error
        self.updates = []

    def update_trip(self, trip_id, fields):
        if self.update_error == "conflict":
            raise ValueError("与已有行程冲突")
        if self.update_error == "boom":
            raise RuntimeError("disk error")
        for trip in self.trips:
            if str(trip.get("trip_id")) == trip_id:
                trip.update(fields)
                self.updates.append((trip_id, dict(fields)))
                return True
        return False


def _run_trip_update(*args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trip_update_standalone(*args, **kwargs)
    return code, out.getvalue()


def test_trip_update_fields_and_json(monkeypatch):
    store = _UpdateStore(
        [
            _trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1"),
            _trip("杭州", "2026-12-02", trip_id="trip_2"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_update("u1", "trip_1", {"purpose": "会议"})
    assert code == 0
    assert "已更新行程 #trip_1" in out and "北京 → 上海" in out
    assert "与 1 条行程日期重叠：#trip_2" in out
    assert store.updates[-1] == ("trip_1", {"purpose": "会议"})

    code, out = _run_trip_update(
        "u1",
        "trip_1",
        {"start": "2026-12-01", "end": "2026-12-02"},
        as_json=True,
    )
    data = json.loads(out)
    assert code == 0 and data["updated"] is True
    assert data["start_date"] == "2026-12-01" and data["end_date"] == "2026-12-02"
    assert data["conflicts"][0]["trip_id"] == "trip_2"
    print("✓ trip-update 字段更新 / JSON")


def test_trip_update_validation_errors(monkeypatch):
    store = _UpdateStore([_trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    cases = [
        (("u1", "trip_1", {}), "没有需要更新的字段"),
        (("u1", "trip_1", {"start": "2026/12/01"}), "出发日期格式"),
        (("u1", "trip_1", {"end": "2026-13-01"}), "返程日期格式"),
        (("u1", "trip_1", {"end": "2026-11-01"}), "不能早于"),
        (("u1", "trip_missing", {"purpose": "x"}), "不存在"),
        (("bad id", "trip_1", {"purpose": "x"}), "用户 ID"),
    ]
    for args, needle in cases:
        code, out = _run_trip_update(*args)
        assert code == 1 and needle in out, (args, out)
    assert store.updates == []

    dateless = _UpdateStore([{"trip_id": "trip_1", "destination": "上海"}])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: dateless
    )
    code, out = _run_trip_update("u1", "trip_1", {"end": "2026-12-05"})
    assert code == 1 and "先设置出发日期" in out

    ambiguous = _UpdateStore(
        [
            _trip("上海", "2026-12-01", trip_id="trip_dup"),
            _trip("杭州", "2026-12-02", trip_id="trip_dup"),
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: ambiguous
    )
    code, out = _run_trip_update("u1", "trip_dup", {"purpose": "x"})
    assert code == 1 and "trips-dedupe" in out and ambiguous.updates == []
    print("✓ trip-update 校验（字段 / 日期 / 不存在 / 歧义）")


def test_trip_update_store_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run_trip_update(
        "u1", "trip_1", {"purpose": "x"}, as_json=True
    )
    data = json.loads(out)
    assert code == 1 and "打开长期记忆失败" in data["error"]

    conflict = _UpdateStore(
        [_trip("上海", "2026-12-01", trip_id="trip_1")], update_error="conflict"
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: conflict
    )
    code, out = _run_trip_update("u1", "trip_1", {"purpose": "x"})
    assert code == 1 and "冲突" in out

    broken = _UpdateStore(
        [_trip("上海", "2026-12-01", trip_id="trip_1")], update_error="boom"
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: broken
    )
    code, out = _run_trip_update("u1", "trip_1", {"purpose": "x"})
    assert code == 1 and "更新行程失败" in out
    print("✓ trip-update 打开 / 冲突 / 其它失败")


def test_trip_update_args_and_dispatch(monkeypatch):
    assert cli._parse_trip_update_args([]) is None
    assert cli._parse_trip_update_args(["u1", "trip_1"]) is None
    assert cli._parse_trip_update_args(["u1", "--purpose", "x"]) is None
    assert cli._parse_trip_update_args(["u1", "trip_1", "--bad"]) is None
    parsed = cli._parse_trip_update_args(
        ["u1", "trip_1", "--purpose=会议", "--start", "2026-12-01", "--json"]
    )
    assert parsed == {
        "user_id": "u1",
        "trip_id": "trip_1",
        "values": {"purpose": "会议", "start": "2026-12-01"},
        "as_json": True,
    }
    calls = []
    monkeypatch.setattr(
        cli,
        "run_trip_update_standalone",
        lambda user_id, trip_id, fields, as_json=False: (
            calls.append((user_id, trip_id, fields, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "trip-update", "u1", "trip_1", "--purpose", "x", "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", "trip_1", {"purpose": "x"}, True)
    for argv in (
        ["cli.py", "trip-update"],
        ["cli.py", "trip-update", "u1", "trip_1"],
        ["cli.py", "trip-update", "u1", "trip_1", "--bad"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "trip-update" in out.getvalue()
    print("✓ trip-update 参数解析与 dispatch")


if __name__ == "__main__":
    for case in (
        test_trip_interval_and_state,
        test_trips_with_conflicts_uses_pool_without_mutation,
        test_trips_list_json,
        test_trips_list_output,
        test_trips_list_empty,
        test_trips_list_read_failure,
        test_trips_list_open_failure,
        test_trips_list_rejects_bad_user_id,
        test_trips_dispatch_strict,
        test_trips_dispatch_json_flexible,
        test_trip_add_saves_and_reports_conflicts,
        test_trip_add_json_output,
        test_trip_add_dedupes_same_route_and_dates,
        test_trip_add_validation_errors,
        test_trip_add_store_failures,
        test_trip_add_args_and_dispatch,
        test_trip_delete_success_and_json,
        test_trip_delete_not_found_and_ambiguous,
        test_trip_delete_failures,
        test_trip_delete_args_and_dispatch,
        test_trip_update_fields_and_json,
        test_trip_update_validation_errors,
        test_trip_update_store_failures,
        test_trip_update_args_and_dispatch,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
