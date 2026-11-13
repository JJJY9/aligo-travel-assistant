"""
行程统计（Web /api/status 与 CLI stats 共用）

与记忆面板同口径：出行天数含首尾两天；到访城市按目的地去重；进行中 /
即将出行以自然日判断。日期缺失或非法的行程只计入总行程数（城市与常去目的地同口径）。
"""
from datetime import datetime
from typing import Any


def summarize_travel(
    trips: list[dict[str, Any]], today: Any = None
) -> dict[str, int]:
    """汇总行程统计：总数 / 出行天数 / 城市数 / 进行中 / 即将出行。"""
    from utils.trip_conflicts import trip_interval

    current = today or datetime.now().date()
    cities: set[str] = set()
    active = 0
    upcoming = 0
    intervals: list[tuple[Any, Any]] = []
    for trip in trips:
        if not isinstance(trip, dict):
            continue
        interval = trip_interval(trip)
        if interval is None:
            # 日期缺失 / 非法的行程只计入总行程数（与常去目的地统计一致：
            # 两者都按可解析日期的行程统计，避免面板一边显示城市一边 cities=0）
            continue
        destination = str(trip.get("destination") or "").strip()
        if destination:
            cities.add(destination)
        start_dt, end_dt = interval
        intervals.append((start_dt.date(), end_dt.date()))
        if start_dt.date() <= current <= end_dt.date():
            active += 1
        elif start_dt.date() > current:
            upcoming += 1

    # 出行天数按日期区间并集：重叠 / 重复行程的同一天不能重复计数
    # （长差旅中顺路加一趟、历史重复 id 等场景，面板本来就提供「重叠」徽标）
    intervals.sort()
    total_days = 0
    if intervals:
        span_start, span_end = intervals[0]
        for day_start, day_end in intervals[1:]:
            # 用序数比较而不是 span_end + timedelta(days=1)：终点是
            # date.max（9999-12-31，模型常用的「长期 / 未定」占位）时
            # 加一天会 OverflowError，/api/status 与 CLI stats 对该用户
            # 永久 500（trips 接口本身接受该日期）
            if day_start.toordinal() <= span_end.toordinal() + 1:
                if day_end > span_end:
                    span_end = day_end
                continue
            total_days += (span_end - span_start).days + 1
            span_start, span_end = day_start, day_end
        total_days += (span_end - span_start).days + 1
    return {
        "total_trips": len(trips),
        "total_days": total_days,
        "cities": len(cities),
        "active": active,
        "upcoming": upcoming,
    }
