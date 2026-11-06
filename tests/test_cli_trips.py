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


if __name__ == "__main__":
    for case in (
        test_trip_interval_and_state,
        test_trips_with_conflicts_uses_pool_without_mutation,
        test_trips_list_output,
        test_trips_list_empty,
        test_trips_list_read_failure,
        test_trips_list_open_failure,
        test_trips_list_rejects_bad_user_id,
        test_trips_dispatch_strict,
        test_trips_dispatch_json_flexible,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
