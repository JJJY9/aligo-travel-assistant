"""行程冲突计算测试（无需 API Key、不访问网络）。

运行：python tests/test_trip_conflicts.py
"""
import os
import sys
from datetime import date, datetime, timedelta

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.trip_conflicts import trip_interval, trips_with_conflicts


def _naive_conflicts(trips, pool=None):
    """参考实现：逐对比较（O(n²)），用于验证二分版本等价。"""
    context = pool if pool is not None else trips
    intervals = [(trip, trip_interval(trip)) for trip in context]
    results = []
    for trip in trips:
        interval = trip_interval(trip)
        conflicts = []
        if interval is not None:
            start_dt, end_dt = interval
            for other, other_interval in intervals:
                if other is trip or other_interval is None:
                    continue
                other_start, other_end = other_interval
                if other_start <= end_dt and start_dt <= other_end:
                    conflicts.append(other)
            conflicts.sort(
                key=lambda item: _parsed_start_key(item)
            )
        results.append([item.get("trip_id") for item in conflicts])
    return results


def _parsed_start_key(trip):
    """与实现相同的排序口径：按解析后的出发日期，无法解析的值排最前。"""
    from datetime import datetime

    raw = str(trip.get("start_date") or "")
    try:
        return datetime.strptime(raw, "%Y-%m-%d"), raw
    except ValueError:
        return datetime.min, raw


def _trip(trip_id, start_offset, duration):
    start = date(2026, 1, 1) + timedelta(days=start_offset)
    end = start + timedelta(days=duration)
    return {
        "trip_id": trip_id,
        "origin": "北京",
        "destination": "上海",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def test_optimized_matches_naive():
    trips = [
        _trip("a", 0, 3),
        _trip("b", 2, 2),  # 与 a 重叠
        _trip("c", 10, 0),
        _trip("d", 10, 0),  # 与 c 同日重叠
        _trip("e", 100, 5),
        {"trip_id": "bad", "start_date": "not-a-date"},
        {"trip_id": "empty", "start_date": ""},
    ]
    optimized = [
        [item.get("trip_id") for item in view["conflicts"]]
        for view in trips_with_conflicts(trips)
    ]
    assert optimized == _naive_conflicts(trips)
    # pool 语义（分页：一页行程按完整历史计算）
    pool = trips + [_trip("z", 101, 1)]
    optimized_pool = [
        [item.get("trip_id") for item in view["conflicts"]]
        for view in trips_with_conflicts(trips[:2], pool)
    ]
    assert optimized_pool == _naive_conflicts(trips[:2], pool)
    print("✓ 二分版与逐对参考实现等价（含脏日期 / pool 分页）")


def test_large_history_fast_and_correct():
    """几千条行程：结果与重叠对数正确，且复杂度不再是 O(n²)。"""
    count = 3000
    trips = [_trip(f"t{index}", index * 4, 5) for index in range(count)]
    views = trips_with_conflicts(trips)
    total = sum(len(view["conflicts"]) for view in views)
    # [i*4, i*4+5] 与前后各一条重叠：共 2*(n-1) 对
    assert total == 2 * (count - 1), total
    # 抽查前 50 条与参考实现一致
    optimized = [
        [item.get("trip_id") for item in view["conflicts"]] for view in views[:50]
    ]
    assert optimized == _naive_conflicts(trips[:50], trips)
    print(f"✓ {count} 条行程：冲突对 {total}（二分实现与参考一致）")


def test_trips_with_conflicts_reuses_precomputed_intervals():
    """调用方预计算的区间表（同一请求内 /api/status 复用）优先于重新解析。"""
    first = {"trip_id": "a", "start_date": "2026-01-01", "end_date": "2026-01-02"}
    second = {"trip_id": "b", "start_date": "2026-01-10", "end_date": "2026-01-11"}
    pinned = {
        id(first): (datetime(2026, 1, 1), datetime(2026, 1, 9)),
        id(second): (datetime(2026, 1, 8), datetime(2026, 1, 11)),
    }
    views = trips_with_conflicts([first], [first, second], intervals=pinned)
    assert views[0]["conflict_count"] == 1, "应按传入区间判定重叠"
    # 不传区间表时按原始日期计算（既有语义不变：两条互不重叠）
    plain = trips_with_conflicts([first], [first, second])
    assert plain[0]["conflict_count"] == 0
    print("✓ 冲突计算复用预计算区间")


def test_conflicts_capped_with_exact_count():
    """重叠极多时只物化前 N 条，conflict_count 仍精确（避免 O(N²) 物化）。"""
    count = 300
    trips = [_trip(f"s{index}", 0, 0) for index in range(count)]
    views = trips_with_conflicts(trips)
    assert all(view["conflict_count"] == count - 1 for view in views)
    assert all(len(view["conflicts"]) <= 20 for view in views)
    assert all(len(view["conflicts"]) == 20 for view in views[::25])
    timings = sum(len(view["conflicts"]) for view in views)
    assert timings == count * 20
    print("✓ 冲突列表截断且总数精确")


def test_conflict_order_uses_parsed_dates():
    """未补零历史日期按真实时间排序（字符串序会把 1-15 排到 1-2 前面）。"""
    trips = [
        {
            "trip_id": "target",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-01-01",
            "end_date": "2026-01-20",
        },
        {
            "trip_id": "early",
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-1-2",
            "end_date": "2026-1-3",
        },
        {
            "trip_id": "late",
            "origin": "北京",
            "destination": "南京",
            "start_date": "2026-1-15",
            "end_date": "2026-1-16",
        },
    ]
    views = trips_with_conflicts(trips)
    order = [item["trip_id"] for item in views[0]["conflicts"]]
    assert order == ["early", "late"], order
    print("✓ 冲突列表按解析后的日期排序")


def test_find_saved_trip_normalizes_identity_fields():
    """历史行未补零日期 / CRLF 路线仍能定位本次保存的记录（不与自己重叠）。"""
    from utils.trip_conflicts import find_saved_trip

    trip = {
        "trip_id": "trip_1",
        "origin": "北京\r\n西城",
        "destination": "a\rb\r\nc",
        "start_date": "2026-1-5",
        "end_date": "2026-1-6",
    }
    record = {
        "origin": "北京\n西城",
        "destination": "a\nb\nc",
        "start_date": "2026-01-05",
        "end_date": "2026-01-06",
    }
    assert find_saved_trip([trip], "trip_1", record) is trip

    # 记录缺返程（同日行程）与历史行显式同日等价
    same_day = {
        "trip_id": "trip_2",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-01-05",
        "end_date": "2026-01-05",
    }
    assert (
        find_saved_trip(
            [same_day],
            "trip_2",
            {"origin": "北京", "destination": "上海", "start_date": "2026-01-05"},
        )
        is same_day
    )
    print("✓ find_saved_trip 按身份口径归一日期 / 换行")


def test_find_saved_trip_distinguishes_origin():
    """同 trip_id、同日同目的地但出发地不同：按出发地定位本次保存的那条。"""
    from utils.trip_conflicts import find_saved_trip

    trips = [
        {
            "trip_id": "trip_1",
            "origin": "广州",
            "destination": "上海",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
    ]
    record = {
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
    }
    found = find_saved_trip(trips, "trip_1", record)
    assert found is trips[1], found
    print("✓ find_saved_trip 按出发地精确匹配")


def test_inverted_date_range_is_logged(caplog):
    """返程早于出发的历史数据静默截成单日，但必须留下告警。"""
    import logging

    from utils import trip_conflicts as module

    module._WARNED_TRIP_ISSUES.clear()
    trip = {
        "trip_id": "trip_inv",
        "start_date": "2026-03-18",
        "end_date": "2026-03-11",
    }
    with caplog.at_level(logging.WARNING):
        interval = trip_interval(trip)
    assert "早于出发日期" in caplog.text
    assert interval is not None
    assert interval[0] == interval[1]
    print("✓ 倒置日期区间记录告警")


def test_trip_interval_warns_once_per_bad_value(caplog):
    """同一脏日期只警告一次：面板全量刷新不再放大成数万条日志。"""
    import logging

    from utils import trip_conflicts as module

    module._WARNED_TRIP_ISSUES.clear()
    bad_end = {
        "trip_id": "t-bad",
        "start_date": "2030-01-01",
        "end_date": "下周一",
    }
    inverted = {
        "trip_id": "t-inv",
        "start_date": "2030-02-10",
        "end_date": "2030-02-01",
    }
    with caplog.at_level(logging.WARNING, logger="utils.trip_conflicts"):
        caplog.clear()
        for _ in range(5):
            assert module.trip_interval(bad_end) is not None
            assert module.trip_interval(inverted) is not None
        messages = [record.getMessage() for record in caplog.records]
    unparsable = [m for m in messages if "无法解析" in m]
    inverted_hits = [m for m in messages if "早于出发" in m]
    assert len(unparsable) == 1, messages
    assert len(inverted_hits) == 1, messages
    # 不同脏值各自首次仍会告警
    with caplog.at_level(logging.WARNING, logger="utils.trip_conflicts"):
        caplog.clear()
        module.trip_interval(
            {"trip_id": "t-bad2", "start_date": "2030-01-01", "end_date": "下周"}
        )
        assert any("无法解析" in record.getMessage() for record in caplog.records)
    module._WARNED_TRIP_ISSUES.clear()
    print("✓ 脏日期警告按值去重（不同值仍逐类告警）")


def test_trip_interval_parses_identity_dates():
    """备份保真保留的斜杠 / 未补零 / 全角数字日期按身份口径参与区间解析。"""
    from utils.trip_conflicts import trip_state_label

    slash = {
        "trip_id": "t-slash",
        "destination": "杭州",
        "start_date": "2026/03/12",
        "end_date": "2026/3/14",
    }
    interval = trip_interval(slash)
    assert interval is not None, interval
    assert interval[0].date() == date(2026, 3, 12)
    assert interval[1].date() == date(2026, 3, 14)

    # 出发补零、返程斜杠：返程不能被静默截成出发当日
    mixed = {
        "trip_id": "t-mixed",
        "start_date": "2026-01-01",
        "end_date": "2026/01/10",
    }
    mixed_interval = trip_interval(mixed)
    assert mixed_interval is not None
    assert mixed_interval[1].date() == date(2026, 1, 10)

    # 全角数字经身份归一为等价 ASCII 日期
    full = {"trip_id": "t-full", "start_date": "２０２６-０３-１２"}
    full_interval = trip_interval(full)
    assert full_interval is not None
    assert full_interval[0].date() == date(2026, 3, 12)
    assert full_interval[0].date() == full_interval[1].date()

    # 非真实日历日仍按无日期处理
    assert trip_interval({"start_date": "2026-02-30"}) is None

    # 状态徽标 / 统计与区间同口径
    assert trip_state_label(slash, today=date(2026, 3, 13)) == "进行中"
    from utils.travel_stats import summarize_travel

    summary = summarize_travel([slash], today=date(2026, 1, 1))
    assert summary["cities"] == 1, summary
    assert summary["total_days"] == 3, summary
    print("✓ 斜杠 / 全角日期按身份口径参与区间、状态与统计")


def test_placeholder_end_counts_as_single_day():
    """9999-12-31 返程占位按出发当日计算，不再永久「进行中」/ 全民重叠。

    与对话写入路径 agents/orchestration_agent 丢弃 9999 返程的口径一致；
    存储与导出仍保真保留原值。
    """
    from utils.trip_conflicts import trip_conflicts, trip_state_label

    placeholder = {
        "trip_id": "t-9999",
        "destination": "上海",
        "start_date": "2020-01-01",
        "end_date": "9999-12-31",
    }
    interval = trip_interval(placeholder)
    assert interval is not None, interval
    assert interval[0].date() == date(2020, 1, 1)
    assert interval[1].date() == date(2020, 1, 1), "占位返程按出发当日计算"
    assert trip_state_label(placeholder, today=date(2026, 11, 28)) == "已结束"
    future = {
        "trip_id": "t-future",
        "destination": "杭州",
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
    }
    start, end = interval
    assert (
        trip_conflicts(
            [placeholder, future], start, end, exclude_trip=placeholder
        )
        == []
    ), "占位行程不应与未来行程重叠"
    print("✓ 9999-12-31 占位返程按单日参与状态 / 冲突")


def test_slash_dates_overlap_detected():
    """斜杠日期的行程与 ISO 行程的真实重叠不能被漏报。"""
    slash_trip = {
        "trip_id": "a",
        "origin": "北京",
        "destination": "杭州",
        "start_date": "2026/03/12",
        "end_date": "2026-03-14",
    }
    iso_trip = {
        "trip_id": "b",
        "origin": "北京",
        "destination": "南京",
        "start_date": "2026-03-13",
        "end_date": "2026-03-14",
    }
    views = trips_with_conflicts([slash_trip, iso_trip])
    assert views[0]["conflict_count"] == 1, views[0]
    assert views[1]["conflict_count"] == 1, views[1]
    assert [item["trip_id"] for item in views[0]["conflicts"]] == ["b"]
    print("✓ 斜杠日期行程参与日期重叠检测")


def test_conflict_order_uses_parsed_slash_dates():
    """斜杠日期按真实日期排序（不能因解析失败被排到最前）。"""
    target = {
        "trip_id": "target",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
    }
    early = {
        "trip_id": "early",
        "origin": "北京",
        "destination": "杭州",
        "start_date": "2026/1/2",
        "end_date": "2026/1/3",
    }
    late = {
        "trip_id": "late",
        "origin": "北京",
        "destination": "南京",
        "start_date": "2026-1-15",
        "end_date": "2026-1-16",
    }
    views = trips_with_conflicts([target, early, late])
    order = [item["trip_id"] for item in views[0]["conflicts"]]
    assert order == ["early", "late"], order
    print("✓ 冲突排序对斜杠日期按真实日期生效")


def test_unparsable_start_warns_once(caplog):
    """非法出发日期（非真实日历日）按无日期处理，但同值只告警一次。"""
    import logging

    from utils import trip_conflicts as module

    module._WARNED_TRIP_ISSUES.clear()
    trip = {"trip_id": "t-bad-start", "start_date": "2026-02-30"}
    with caplog.at_level(logging.WARNING, logger="utils.trip_conflicts"):
        for _ in range(3):
            assert module.trip_interval(trip) is None
    messages = [record.getMessage() for record in caplog.records]
    hits = [m for m in messages if "出发日期无法解析" in m]
    assert len(hits) == 1, messages
    module._WARNED_TRIP_ISSUES.clear()
    print("✓ 非法出发日期按无日期处理且同值只告警一次")


def test_warn_once_bounded_eviction_keeps_recent(monkeypatch, caplog):
    """警告去重集合到上限时逐出最早条目（而不是整体清空后重刷屏）。"""
    import logging

    from utils import trip_conflicts as module

    module._WARNED_TRIP_ISSUES.clear()
    monkeypatch.setattr(module, "_WARNED_TRIP_ISSUES_MAX", 2)
    try:
        with caplog.at_level(logging.WARNING, logger="utils.trip_conflicts"):
            module._warn_trip_issue_once("k", "v1", "警告 %s", "v1")
            module._warn_trip_issue_once("k", "v2", "警告 %s", "v2")
            module._warn_trip_issue_once("k", "v3", "警告 %s", "v3")  # 逐出 v1
            module._warn_trip_issue_once("k", "v2", "警告 %s", "v2")  # 仍去重
            module._warn_trip_issue_once("k", "v1", "警告 %s", "v1")  # 重新告警
        messages = [record.getMessage() for record in caplog.records]
        assert messages == ["警告 v1", "警告 v2", "警告 v3", "警告 v1"], messages
    finally:
        module._WARNED_TRIP_ISSUES.clear()
    print("✓ 警告去重集合超限逐出最早条目，不整体清空")


def test_iso_date_parse_cache_reuses_normalized_text():
    """归一后同一日期串只 strptime 一次（/api/status 三遍解析的公共热点）。"""
    from utils import trip_conflicts

    trip_conflicts._parse_iso_date.cache_clear()
    first = trip_conflicts._parse_trip_date("2026-03-12")
    second = trip_conflicts._parse_trip_date("2026/3/12")
    assert first == second
    info = trip_conflicts._parse_iso_date.cache_info()
    assert info.misses == 1, info
    assert info.hits >= 1, info
    print("✓ 行程日期解析按归一文本缓存")


def test_placeholder_query_interval_not_reported_as_overlap():
    """保存 / 编辑响应用原始 9999 返程查询时也不能误报全民重叠。"""
    from datetime import datetime

    from utils.trip_conflicts import trip_conflicts

    placeholder = {
        "trip_id": "t-9999",
        "destination": "上海",
        "start_date": "2020-01-01",
        "end_date": "9999-12-31",
    }
    future = {
        "trip_id": "t-future",
        "destination": "杭州",
        "start_date": "2026-12-01",
        "end_date": "2026-12-03",
    }
    # 与写侧调用方同形：end 是原始解析值 9999-12-31
    assert (
        trip_conflicts(
            [placeholder, future],
            datetime(2020, 1, 1),
            datetime(9999, 12, 31),
            exclude_trip=placeholder,
        )
        == []
    )
    # 倒置区间按单日：12-03 与 12-05 起的行程不重叠
    later = {
        "trip_id": "t-later",
        "destination": "南京",
        "start_date": "2026-12-05",
        "end_date": "2026-12-06",
    }
    assert trip_conflicts(
        [later], datetime(2026, 12, 3), datetime(2026, 12, 1)
    ) == []
    print("✓ 占位 / 倒置查询区间不误报冲突")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    test_optimized_matches_naive()
    test_large_history_fast_and_correct()
    test_conflict_order_uses_parsed_dates()
    test_conflicts_capped_with_exact_count()
    test_trips_with_conflicts_reuses_precomputed_intervals()
    test_find_saved_trip_normalizes_identity_fields()
    test_find_saved_trip_distinguishes_origin()
    test_trip_interval_parses_identity_dates()
    test_placeholder_end_counts_as_single_day()
    test_placeholder_query_interval_not_reported_as_overlap()
    test_slash_dates_overlap_detected()
    test_conflict_order_uses_parsed_slash_dates()
    test_iso_date_parse_cache_reuses_normalized_text()
    # 日志断言用例：直跑用 caplog 替身真正执行
    import _caplog_shim

    _shim = _caplog_shim.CaplogShim()
    try:
        test_inverted_date_range_is_logged(_shim)
        # 此前漏登记：同一文件的另一个日志用例在直跑时从未执行
        test_trip_interval_warns_once_per_bad_value(_shim)
        test_unparsable_start_warns_once(_shim)
        import pytest as _pytest

        _mp_warn = _pytest.MonkeyPatch()
        try:
            test_warn_once_bounded_eviction_keeps_recent(_mp_warn, _shim)
        finally:
            _mp_warn.undo()
    finally:
        _shim.close()
    print("\n全部通过")
