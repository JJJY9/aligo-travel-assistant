"""
行程统计（Web /api/status 与 CLI stats 共用）

与记忆面板同口径：出行天数含首尾两天；到访城市按目的地去重；进行中 /
即将出行以自然日判断。日期缺失或非法的行程只计入总行程数。
"""
from datetime import datetime
from typing import Any


def summarize_travel(
    trips: list[dict[str, Any]], today: Any = None
) -> dict[str, int]:
    """汇总行程统计：总数 / 出行天数 / 城市数 / 进行中 / 即将出行。"""
    from utils.trip_conflicts import trip_interval

    current = today or datetime.now().date()
    total_days = 0
    cities: set[str] = set()
    active = 0
    upcoming = 0
    for trip in trips:
        if not isinstance(trip, dict):
            continue
        interval = trip_interval(trip)
        if interval is None:
            continue
        start_dt, end_dt = interval
        total_days += (end_dt.date() - start_dt.date()).days + 1
        destination = str(trip.get("destination") or "").strip()
        if destination:
            cities.add(destination)
        if start_dt.date() <= current <= end_dt.date():
            active += 1
        elif start_dt.date() > current:
            upcoming += 1
    return {
        "total_trips": len(trips),
        "total_days": total_days,
        "cities": len(cities),
        "active": active,
        "upcoming": upcoming,
    }
