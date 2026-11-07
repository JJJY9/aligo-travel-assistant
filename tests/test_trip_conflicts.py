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
            conflicts.sort(key=lambda item: str(item.get("start_date") or ""))
        results.append([item.get("trip_id") for item in conflicts])
    return results


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


if __name__ == "__main__":
    test_optimized_matches_naive()
    test_large_history_fast_and_correct()
    print("\n全部通过")
