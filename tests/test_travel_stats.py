"""行程统计（utils/travel_stats.py）测试（无需 API Key）。"""
import os
import sys
from datetime import date

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.travel_stats import summarize_travel


def test_summarize_mixed_trips():
    """天数含首尾、城市去重；无日期行程只计入总行程数。"""
    trips = [
        {"destination": "上海", "start_date": "2099-01-01", "end_date": "2099-01-03"},
        {"destination": "杭州", "start_date": "2099-02-01", "end_date": "2099-02-01"},
        {"destination": "上海", "start_date": "2099-03-01", "end_date": "2099-03-02"},
        {"destination": "无日期"},
        "not-a-dict",
    ]
    summary = summarize_travel(trips, today=date(2026, 11, 6))
    assert summary == {
        "total_trips": 5,
        "total_days": 6,
        "cities": 2,
        "active": 0,
        "upcoming": 3,
    }
    print("✓ 行程统计（天数含首尾 / 城市去重 / 无日期只计总数）")


def test_summarize_active_and_past():
    today = date(2026, 11, 6)
    trips = [
        {"destination": "北京", "start_date": "2026-11-05", "end_date": "2026-11-07"},
        {"destination": "上海", "start_date": "2026-11-06", "end_date": "2026-11-06"},
        {"destination": "广州", "start_date": "2026-10-01", "end_date": "2026-10-02"},
        {"destination": "深圳", "start_date": "2026-12-01", "end_date": "2026-12-02"},
    ]
    summary = summarize_travel(trips, today=today)
    assert summary["active"] == 2, "进行中：跨今天 + 当天往返"
    assert summary["upcoming"] == 1
    assert summary["total_days"] == 3 + 2 + 2, "重叠日只计一次"
    print("✓ 进行中 / 已结束 / 即将出行判断")


def test_summarize_overlapping_days_counted_once():
    """重叠行程的同一天不能重复计入出行天数。"""
    trips = [
        {"destination": "上海", "start_date": "2026-01-01", "end_date": "2026-01-10"},
        {"destination": "北京", "start_date": "2026-01-05", "end_date": "2026-01-06"},
        {"destination": "杭州", "start_date": "2026-01-10", "end_date": "2026-01-11"},
    ]
    summary = summarize_travel(trips, today=date(2026, 1, 7))
    assert summary["total_days"] == 11, summary
    print("✓ 重叠行程按日期并集计天数")


def test_summarize_empty_and_invalid():
    assert summarize_travel([]) == {
        "total_trips": 0,
        "total_days": 0,
        "cities": 0,
        "active": 0,
        "upcoming": 0,
    }
    assert summarize_travel([{"start_date": "not-a-date", "destination": "上海"}])[
        "total_trips"
    ] == 1
    print("✓ 空列表 / 非法日期")


def test_summarize_tolerates_date_max_end():
    """终点为 9999-12-31 的行程不能使统计 OverflowError（面板 / CLI 永久 500）。"""
    long_trip = {
        "destination": "上海",
        "start_date": "2020-01-01",
        "end_date": "9999-12-31",
    }
    trips = [
        long_trip,
        {"destination": "杭州", "start_date": "2026-03-01", "end_date": "2026-03-05"},
    ]
    summary = summarize_travel(trips, today=date(2026, 11, 6))
    assert summary["total_trips"] == 2
    assert summary["total_days"] == (date.max - date(2020, 1, 1)).days + 1

    single = summarize_travel([long_trip], today=date(2026, 11, 6))
    assert single["total_days"] == (date.max - date(2020, 1, 1)).days + 1
    print("✓ date.max 终点的区间合并不再溢出")


if __name__ == "__main__":
    test_summarize_mixed_trips()
    test_summarize_active_and_past()
    test_summarize_overlapping_days_counted_once()
    test_summarize_empty_and_invalid()
    test_summarize_tolerates_date_max_end()
    print("\n全部通过")
