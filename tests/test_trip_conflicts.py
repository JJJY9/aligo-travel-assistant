"""行程冲突计算测试（无需 API Key、不访问网络）。

运行：python tests/test_trip_conflicts.py
"""
import os
import sys
from datetime import date, timedelta

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


def test_inverted_date_range_is_logged(caplog=None):
    """返程早于出发的历史数据静默截成单日，但必须留下告警。"""
    import logging

    trip = {
        "trip_id": "trip_inv",
        "start_date": "2026-03-18",
        "end_date": "2026-03-11",
    }
    if caplog is not None:
        with caplog.at_level(logging.WARNING):
            interval = trip_interval(trip)
        assert "早于出发日期" in caplog.text
    else:
        interval = trip_interval(trip)
    assert interval is not None
    assert interval[0] == interval[1]
    print("✓ 倒置日期区间记录告警")


if __name__ == "__main__":
    test_optimized_matches_naive()
    test_large_history_fast_and_correct()
    test_conflict_order_uses_parsed_dates()
    test_find_saved_trip_normalizes_identity_fields()
    test_find_saved_trip_distinguishes_origin()
    test_inverted_date_range_is_logged()
    print("\n全部通过")
