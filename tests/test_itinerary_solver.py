"""行程必去项分配求解测试（纯 Python；OR-Tools 未安装时走贪心）。

覆盖：单日分配、跨日溢出、容量不足未安排、零天兜底、渲染文本。

运行：python tests/test_itinerary_solver.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.itinerary_solver import render_schedule, schedule_must_see


def test_single_day_schedule():
    """两个必去项在单日内按 2 小时参观 + 30 分钟通勤排开。"""
    schedule = schedule_must_see(["西湖", "灵隐寺"], days=1)
    assert schedule["solver"] in {"cp-sat", "greedy"}
    day = schedule["days"][0]
    assert [item["name"] for item in day["items"]] == ["西湖", "灵隐寺"]
    assert day["items"][0]["start"] == "09:00"
    assert day["items"][0]["end"] == "11:00"
    assert day["items"][1]["start"] == "11:30"
    assert day["items"][1]["end"] == "13:30"
    assert schedule["unscheduled"] == []
    print("✓ 单日必去项分配")


def test_overflow_to_next_day():
    """单日时间窗不足时溢出到下一天。"""
    schedule = schedule_must_see(
        ["西湖", "灵隐寺", "雷峰塔"],
        days=2,
        day_start=9 * 60,
        day_end=12 * 60,
        visit_min=120,
        travel_min=30,
    )
    first_day = [item["name"] for item in schedule["days"][0]["items"]]
    second_day = [item["name"] for item in schedule["days"][1]["items"]]
    assert first_day == ["西湖"], schedule
    assert "灵隐寺" in second_day or "雷峰塔" in second_day, schedule
    print("✓ 时间窗不足时跨日分配")


def test_unscheduled_when_capacity_insufficient():
    """所有天都排不下时返回未安排列表，不伪造时间。"""
    schedule = schedule_must_see(
        ["西湖", "灵隐寺"],
        days=1,
        day_start=9 * 60,
        day_end=10 * 60,
        visit_min=120,
    )
    assert schedule["days"][0]["items"] == []
    assert schedule["unscheduled"] == ["西湖", "灵隐寺"]
    print("✓ 容量不足返回未安排项")


def test_zero_days():
    """天数为 0 时全部未安排。"""
    schedule = schedule_must_see(["西湖"], days=0)
    assert schedule["days"] == []
    assert schedule["unscheduled"] == ["西湖"]
    print("✓ 零天兜底")


def test_render_schedule_text():
    """渲染文本包含日期、时间与未安排提示。"""
    schedule = schedule_must_see(
        ["西湖", "灵隐寺"],
        days=1,
        day_start=9 * 60,
        day_end=11 * 60,
        visit_min=120,
    )
    text = render_schedule(schedule)
    assert "Day 1" in text and "西湖" in text
    assert "时间窗不足" in text and "灵隐寺" in text
    assert render_schedule({"days": [], "unscheduled": []}) == ""
    print("✓ 分配结果渲染")


def test_tight_window_counts_travel_only_between_items():
    """紧凑窗口下当天第一项不需要通勤：仍能安排一个必去项。"""
    schedule = schedule_must_see(
        ["西湖", "灵隐寺"],
        days=1,
        day_start=9 * 60,
        day_end=11 * 60,
        visit_min=120,
        travel_min=30,
    )
    items = schedule["days"][0]["items"]
    assert len(items) == 1, schedule
    assert len(schedule["unscheduled"]) == 1, schedule
    print("✓ 紧凑窗口按项间通勤计算容量")


def test_days_upper_bound_clamped():
    """异常大的天数被钳到 366，避免为每天建列表 / CP-SAT 变量。"""
    schedule = schedule_must_see(["西湖"], days=100000)
    assert len(schedule["days"]) == 366, len(schedule["days"])
    assert [item["name"] for item in schedule["days"][0]["items"]] == ["西湖"]
    print("✓ 天数上限钳制")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_single_day_schedule()
    test_overflow_to_next_day()
    test_unscheduled_when_capacity_insufficient()
    test_zero_days()
    test_render_schedule_text()
    test_tight_window_counts_travel_only_between_items()
    test_days_upper_bound_clamped()
    print("\n全部通过")
