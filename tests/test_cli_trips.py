"""
`python cli.py trips` 测试（无需 API Key、不访问网络）

覆盖：行程列表输出（状态 / 重叠 / 顺序）、无行程、读取 / 打开失败、
非法用户与严格参数解析，以及 Web / CLI 共用的行程日期工具。

运行：python tests/test_cli_trips.py
"""
import contextlib
import io
import json
import logging
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


def test_trips_status_filter_text_and_json(monkeypatch):
    """--status 按面板状态口径过滤：进行中 / 即将出行 / 已结束，无日期不出现。"""
    from datetime import date

    today = date.today().isoformat()
    store = _FakeStore(
        [
            {"destination": "老行程", "start_date": "2000-01-01", "end_date": "2000-01-02"},
            {"destination": "今天的行程", "start_date": today, "end_date": today},
            {"destination": "未来的行程", "start_date": "2099-01-01", "end_date": "2099-01-02"},
            {"destination": "无日期"},
        ]
    )
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)

    def _run(status, as_json=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.run_trips_list_standalone(
                "u-trips", as_json=as_json, status_filter=status
            )
        return code, buf.getvalue()

    code, out = _run("past", as_json=True)
    payload = json.loads(out)
    assert code == 0
    assert payload["status"] == "已结束"
    assert [t["destination"] for t in payload["trips"]] == ["老行程"]

    code, out = _run("active")
    assert code == 0 and "今天的行程" in out and "老行程" not in out
    assert "状态为「进行中」" in out

    code, out = _run("即将出行", as_json=True)
    payload = json.loads(out)
    assert [t["destination"] for t in payload["trips"]] == ["未来的行程"]
    assert all(t["destination"] != "无日期" for t in payload["trips"])

    print("✓ trips --status 过滤（进行中 / 即将 / 已结束 / 无日期排除）")


def test_filter_trips_by_query_helper(monkeypatch):
    """面板 / API / CLI 共用的关键词过滤口径。"""
    from utils.trip_conflicts import filter_trips_by_query

    trips = [
        {
            "origin": "北京",
            "destination": "上海",
            "purpose": "出差",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "purpose": "会议",
            "start_date": "2026-12-05",
        },
    ]
    assert filter_trips_by_query(trips, "") == trips
    assert filter_trips_by_query(trips, "上海") == [trips[0]]
    assert filter_trips_by_query(trips, "北京 上海") == [trips[0]], "跨字段多词"
    assert filter_trips_by_query(trips, "会议") == [trips[1]]
    assert filter_trips_by_query(trips, "不存在") == []
    print("✓ filter_trips_by_query（面板口径）")


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


def test_trips_list_strips_terminal_controls(monkeypatch):
    """行程字段里的 ESC / OSC 不能原样送达终端（列表输出同样清洗）。"""
    store = _FakeStore(
        [_trip("上海\x1b]52;c;QUJD\x07", "2026-12-05", "2026-12-07")]
    )
    code, out = _run_list(monkeypatch, store)
    assert code == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "上海" in out
    print("✓ trips 列表剥离终端控制序列")


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


def test_trip_add_without_purpose_preserves_existing(monkeypatch):
    """未提供 --purpose 时不上报目的字段：命中既有行程保留用户改过的目的。"""
    import tempfile

    from context.long_term_memory import LongTermMemory

    store = LongTermMemory("u-trip-purpose", tempfile.mkdtemp())
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, _ = _run_trip_add(
        "u-trip-purpose", "北京", "上海", "2026-12-01", None, "会议"
    )
    assert code == 0
    code, out = _run_trip_add("u-trip-purpose", "北京", "上海", "2026-12-01")
    assert code == 0 and "已更新行程" in out
    trips = store.get_trip_history(limit=None)
    assert len(trips) == 1
    assert trips[0]["purpose"] == "会议", "未提交的目的不能被默认值覆盖"
    # 新建行程未提供目的时也不臆造字段（展示层有「出行」兜底）
    code, _ = _run_trip_add("u-trip-purpose", "北京", "杭州", "2026-12-05")
    assert code == 0
    created = [
        trip
        for trip in store.get_trip_history(limit=None)
        if trip.get("destination") == "杭州"
    ]
    assert len(created) == 1
    assert "purpose" not in created[0]
    print("✓ trip-add 未提供目的时保留既有目的 / 不臆造字段")


def test_trip_add_reports_success_when_post_save_read_fails(monkeypatch):
    """写入已提交后读取失败：按成功返回并带 trip_id（不能谎报失败）。"""

    class _FlakyStore(_AddStore):
        def __init__(self, trips=()):
            super().__init__(trips)
            self.reads = 0

        def get_trip_history(self, limit=None):
            self.reads += 1
            # 偶数次读取（保存后的那次）失败，保存前的预读仍成功
            if self.reads % 2 == 0:
                raise RuntimeError("db down")
            return super().get_trip_history(limit)

    store = _FlakyStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add(
        "u1", "北京", "上海", "2026-12-01", as_json=True
    )
    data = json.loads(out)
    assert code == 0
    assert data["ok"] is True and data["saved"] is True
    assert data["trip_id"] == "trip_1"
    assert data["created"] is None
    assert "读取行程失败" in data["warning"]
    code, out = _run_trip_add("u1", "北京", "上海", "2026-12-01")
    assert code == 0 and "已保存行程" in out and "读取行程失败" in out
    print("✓ trip-add 写后读取失败仍报告成功")


def test_trip_update_reports_success_when_post_save_read_fails(monkeypatch):
    """更新已提交后读取失败：按成功返回并带 trip_id。"""

    class _FlakyUpdateStore(_UpdateStore):
        def __init__(self, trips=()):
            super().__init__(trips)
            self.reads = 0

        def get_trip_history(self, limit=None):
            self.reads += 1
            if self.reads >= 2:
                raise RuntimeError("db down")
            return super().get_trip_history(limit)

    store = _FlakyUpdateStore(
        [_trip("上海", "2026-12-01", trip_id="trip_1")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_update(
        "u1", "trip_1", {"purpose": "会议"}, as_json=True
    )
    data = json.loads(out)
    assert code == 0
    assert data["ok"] is True and data["saved"] is True
    assert data["trip_id"] == "trip_1"
    assert data["created"] is None and "读取行程失败" in data["warning"]
    print("✓ trip-update 写后读取失败仍报告成功")


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


def test_trip_add_rejects_oversized_fields(monkeypatch):
    """与 Web 请求模型同一上限：超长字段直接拒绝，不落库。"""
    store = _AddStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    cases = [
        (("u1", "长" * 101, "上海", "2026-12-01"), "出发地"),
        (("u1", "北京", "长" * 101, "2026-12-01"), "目的地"),
        (("u1", "北京", "上海", "2026-12-01", None, "长" * 101), "目的"),
    ]
    for args, needle in cases:
        code, out = _run_trip_add(*args)
        assert code == 1 and needle in out, (args, out)
    assert store.saved == [], "超长字段不应落库"
    print("✓ trip-add 拒绝超过 100 字的字段")


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
        cli._parse_trip_add_args(
            [
                "u1",
                "--destination",
                "上海",
                "--start",
                "2026-12-01",
                "--purpose",
                "--json",
            ]
        )
        is None
    ), "以 - 开头的值（疑似下一个参数）必须拒绝，不能存成 purpose"
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
        lambda user_id, origin, destination, start, end=None, purpose=None, as_json=False: (
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
    assert calls[-1] == ("u1", "", "上海", "2026-12-01", None, None, True)

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


def test_trip_update_rejects_oversized_fields(monkeypatch):
    """trip-update 同样限制 100 字，避免落库后面板保存必然 422。"""
    store = _UpdateStore([_trip("上海", "2026-12-01", "2026-12-03", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    for field in ("origin", "destination", "purpose"):
        code, out = _run_trip_update("u1", "trip_1", {field: "长" * 101})
        assert code == 1 and "100" in out, (field, out)
    assert store.updates == [], "超长字段不应落库"
    print("✓ trip-update 拒绝超过 100 字的字段")


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


def test_trip_update_legacy_unpadded_dates(monkeypatch):
    """存量未补零出发日期：仅改目的时冲突照常计算，补填返程不被误拒。"""
    legacy = _trip("上海", "2026-1-5", purpose="出差", trip_id="trip_legacy")
    store = _UpdateStore(
        [legacy, _trip("杭州", "2026-01-05", "2026-01-06", trip_id="trip_2")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_update("u1", "trip_legacy", {"purpose": "会议"})
    assert code == 0 and "重叠" in out, out

    code, out = _run_trip_update("u1", "trip_legacy", {"end": "2026-01-07"})
    assert code == 0, out
    assert store.updates and store.updates[-1][0] == "trip_legacy"
    print("✓ 存量未补零日期的更新与冲突判断")


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
    assert (
        cli._parse_trip_update_args(["u1", "trip_1", "--purpose", "--json"])
        is None
    ), "以 - 开头的值必须拒绝，避免把 --json 存成 purpose"
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


def test_trips_query_filter_text_and_json(monkeypatch):
    trips = [
        _trip("上海", "2026-12-01", "2026-12-03", trip_id="t1", purpose="出差"),
        _trip("杭州", "2026-12-05", "2026-12-06", trip_id="t2", purpose="会议"),
        _trip("上海", "2026-12-02", "2026-12-02", trip_id="t3", purpose="培训"),
    ]
    store = _FakeStore(trips)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", query="上海")
    text = out.getvalue()
    assert code == 0
    assert "共 2 条匹配「上海」的行程" in text
    assert "杭州" not in text
    assert "重叠" in text, "过滤后冲突提示仍按完整历史计算"

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", as_json=True, query="会议")
    data = json.loads(out.getvalue())
    assert code == 0 and data["query"] == "会议" and data["count"] == 1
    assert data["trips"][0]["destination"] == "杭州"

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", query="不存在")
    assert code == 0 and "没有匹配「不存在」的行程" in out.getvalue()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", as_json=True, query="不存在")
    data = json.loads(out.getvalue())
    assert data["count"] == 0 and data["trips"] == []

    # 跨字段多词查询（与面板一致：五个字段空格拼接后整体包含）
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", as_json=True, query="北京 上海")
    data = json.loads(out.getvalue())
    assert code == 0 and data["count"] == 2, "「北京 上海」应命中两条上海行程"

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_trips_list_standalone("u1", as_json=True, query="上海 出差")
    data = json.loads(out.getvalue())
    assert data["count"] == 1 and data["trips"][0]["trip_id"] == "t1"
    print("✓ trips --query 过滤（文本 / JSON / 无匹配 / 跨字段多词）")


def test_trips_query_args_and_dispatch(monkeypatch):
    assert cli._parse_trips_list_args([]) is None
    assert cli._parse_trips_list_args(["-u1"]) is None
    assert cli._parse_trips_list_args(["u1", "extra"]) is None
    assert cli._parse_trips_list_args(["u1", "--query"]) is None
    assert cli._parse_trips_list_args(["u1", "--query", " "]) is None
    assert (
        cli._parse_trips_list_args(["u1", "--query", "--json"]) is None
    ), "缺值的 --query 不能把 --json 当成关键词"
    assert cli._parse_trips_list_args(["u1", "--bad"]) is None
    assert cli._parse_trips_list_args(["u1"]) == ("u1", "", False, "")
    assert cli._parse_trips_list_args(
        ["--json", "u1", "--query=上海"]
    ) == ("u1", "上海", True, "")
    assert cli._parse_trips_list_args(["u1", "--query", "会 议"]) == (
        "u1",
        "会 议",
        False,
        "",
    )
    assert cli._parse_trips_list_args(["u1", "--status", "active"]) == (
        "u1",
        "",
        False,
        "active",
    )
    assert cli._parse_trips_list_args(["u1", "--status=即将出行"]) == (
        "u1",
        "",
        False,
        "即将出行",
    )
    assert cli._parse_trips_list_args(["u1", "--status"]) is None
    assert cli._parse_trips_list_args(["u1", "--status", "someday"]) is None
    assert cli._parse_trips_list_args(["u1", "--status", "--json"]) is None

    calls = []
    monkeypatch.setattr(
        cli,
        "run_trips_list_standalone",
        lambda user_id, as_json=False, query="", status_filter="": (
            calls.append((user_id, as_json, query, status_filter)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips", "u1", "--query", "上海", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", True, "上海", "")
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "trips", "u1", "--status", "upcoming", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", True, "", "upcoming")
    print("✓ trips --query / --status 参数解析与 dispatch")


def test_trip_conflicts_excludes_only_saved_record_with_duplicate_ids(monkeypatch):
    """重复 trip_id：按身份排除本次保存的那条，其它同 id 行程仍参与重叠判断。"""
    from utils.trip_conflicts import find_saved_trip

    record = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
    }
    other = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "广州",
        "start_date": "2026-12-02",
        "end_date": "2026-12-04",
    }
    third = {
        "trip_id": "trip_2",
        "origin": "北京",
        "destination": "深圳",
        "start_date": "2026-12-03",
        "end_date": "2026-12-05",
    }
    trips = [record, other, third]
    saved = find_saved_trip(trips, "trip_1", record)
    assert saved is record
    conflicts = trip_conflicts(
        trips,
        datetime(2026, 12, 1),
        datetime(2026, 12, 3),
        exclude_id="trip_1",
        exclude_trip=saved,
    )
    destinations = [trip["destination"] for trip in conflicts]
    assert "上海" not in destinations, "本次保存的记录应被排除"
    assert "广州" in destinations and "深圳" in destinations
    print("✓ 重复 trip_id 下按身份排除本次记录")


def test_trip_conflicts_exclude_id_only_when_unique(monkeypatch):
    """exclude_id 在 id 重复时不能整条排除（会隐藏其它行程的真实重叠）。"""
    dup1 = {
        "trip_id": "trip_1",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
    }
    dup2 = dict(dup1)
    conflicts = trip_conflicts(
        [dup1, dup2],
        datetime(2026, 12, 1),
        datetime(2026, 12, 2),
        exclude_id="trip_1",
    )
    assert len(conflicts) == 2, "重复 id 无法区分数本条时必须都参与重叠判断"
    assert (
        trip_conflicts(
            [dup1],
            datetime(2026, 12, 1),
            datetime(2026, 12, 2),
            exclude_id="trip_1",
        )
        == []
    ), "唯一 id 时排除本条"
    print("✓ exclude_id 仅在唯一时整条排除")


def test_trip_interval_warns_on_invalid_end(caplog):
    """返程日期非法（历史脏数据）时告警并按出发当日计算。"""
    with caplog.at_level(logging.WARNING, logger="utils.trip_conflicts"):
        interval = trip_interval(
            {"trip_id": "t1", "start_date": "2026-12-01", "end_date": "未知"}
        )
    assert interval == (datetime(2026, 12, 1), datetime(2026, 12, 1))
    assert any("无法解析" in record.message for record in caplog.records)
    print("✓ 返程日期非法时告警并按出发当日计算")


def test_filter_trips_by_query_multi_word_any_order(monkeypatch):
    """多词筛选：任意空白（含全角）分隔、词序无关、全部命中才保留。"""
    from utils.trip_conflicts import filter_trips_by_query

    trips = [
        {
            "origin": "北京",
            "destination": "上海",
            "purpose": "出差",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
        }
    ]
    assert filter_trips_by_query(trips, "上海 北京") == trips
    assert filter_trips_by_query(trips, "北京　上海") == trips
    assert filter_trips_by_query(trips, "上海  北京") == trips, "多空格应合并"
    assert filter_trips_by_query(trips, "上海 广州") == []
    print("✓ 多词筛选词序与空白无关")


def test_trip_update_ignores_invalid_legacy_end(monkeypatch):
    """历史行程的返程日期非法时，仅修改目的不应被日期校验拒绝。"""
    store = _UpdateStore(
        [
            {
                "trip_id": "trip_1",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "未知",
                "purpose": "出差",
            }
        ]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_update("u1", "trip_1", {"purpose": "会议"})
    assert code == 0, out
    assert store.updates[-1] == ("trip_1", {"purpose": "会议"})
    print("✓ trip-update 历史脏日期不阻断其它字段更新")


def test_trip_add_rejects_non_padded_date(monkeypatch):
    """2026-1-1 这类非补零写法绕不过去重，必须按格式错误拒绝。"""
    store = _UpdateStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run_trip_add("u1", "北京", "上海", "2026-1-1", None, "出差")
    assert code == 1 and "YYYY-MM-DD" in out
    print("✓ trip-add 拒绝非补零日期")

def test_trip_add_rejects_when_total_limit_reached(monkeypatch):
    """行程总数达到上限后不再写入：否则 user-export 将永久无法导出。"""
    from utils import trip_import as trip_import_module

    store = _AddStore([_trip("上海", "2026-12-01", trip_id="trip_1")])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(trip_import_module, "MAX_TOTAL_TRIPS", 1)
    code, out = _run_trip_add("u1", "北京", "杭州", "2026-12-02", None)
    assert code == 1 and "上限" in out, out
    assert store.saved == [], "达到上限时不能写入"
    print("✓ trip-add 行程总量超限时拒绝写入")

if __name__ == "__main__":
    for case in (
        test_trip_conflicts_excludes_only_saved_record_with_duplicate_ids,
        test_trip_conflicts_exclude_id_only_when_unique,
        test_filter_trips_by_query_multi_word_any_order,
        test_trip_interval_and_state,
        test_filter_trips_by_query_helper,
        test_trips_status_filter_text_and_json,
        test_trips_with_conflicts_uses_pool_without_mutation,
        test_trips_list_json,
        test_trips_list_output,
        test_trips_list_strips_terminal_controls,
        test_trips_list_empty,
        test_trips_list_read_failure,
        test_trips_list_open_failure,
        test_trips_list_rejects_bad_user_id,
        test_trips_dispatch_strict,
        test_trips_dispatch_json_flexible,
        test_trips_query_filter_text_and_json,
        test_trips_query_args_and_dispatch,
        test_trip_add_saves_and_reports_conflicts,
        test_trip_add_json_output,
        test_trip_add_rejects_when_total_limit_reached,
        test_trip_add_dedupes_same_route_and_dates,
        test_trip_add_without_purpose_preserves_existing,
        test_trip_add_reports_success_when_post_save_read_fails,
        test_trip_add_validation_errors,
        test_trip_add_rejects_oversized_fields,
        test_trip_add_store_failures,
        test_trip_add_args_and_dispatch,
        test_trip_delete_success_and_json,
        test_trip_delete_not_found_and_ambiguous,
        test_trip_delete_failures,
        test_trip_delete_args_and_dispatch,
        test_trip_update_fields_and_json,
        test_trip_update_validation_errors,
        test_trip_update_legacy_unpadded_dates,
        test_trip_update_rejects_oversized_fields,
        test_trip_update_store_failures,
        test_trip_update_reports_success_when_post_save_read_fails,
        test_trip_update_args_and_dispatch,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
        test_trip_update_ignores_invalid_legacy_end(mp)
        test_trip_add_rejects_non_padded_date(mp)
    print("\n全部通过")
