"""行程日期解析、重叠判断与状态标签（Web / CLI 共用）。"""
from datetime import date, datetime
from typing import Any


def trip_interval(trip: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """行程的日期区间（含首尾）；缺少或无法解析出发日期时返回 None。"""
    try:
        start = datetime.strptime(str(trip.get("start_date") or ""), "%Y-%m-%d")
    except ValueError:
        return None
    try:
        end = datetime.strptime(str(trip.get("end_date") or ""), "%Y-%m-%d")
    except ValueError:
        end = start
    if end < start:
        end = start
    return start, end


def trip_conflicts(
    trips: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    exclude_id: str | None = None,
) -> list[dict[str, Any]]:
    """找出与给定日期区间重叠的其它行程（按出发日期排序）。"""
    conflicts = []
    for trip in trips:
        if exclude_id is not None and str(trip.get("trip_id")) == exclude_id:
            continue
        interval = trip_interval(trip)
        if interval is None:
            continue
        other_start, other_end = interval
        if other_start <= end and start <= other_end:
            conflicts.append(trip)
    conflicts.sort(key=lambda item: str(item.get("start_date") or ""))
    return conflicts


def filter_trips_by_query(
    trips: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """按关键词过滤行程（面板 / Web API / CLI 同一口径）。

    五个展示字段（origin / destination / purpose / start_date / end_date）
    用空格拼接后做整体包含匹配（大小写不敏感），支持跨字段多词查询
    （如「北京 上海」）；空关键词原样返回。
    """
    needle = str(query or "").strip().casefold()
    if not needle:
        return list(trips)
    return [
        trip
        for trip in trips
        if needle
        in " ".join(
            str(trip.get(key) or "")
            for key in (
                "origin",
                "destination",
                "purpose",
                "start_date",
                "end_date",
            )
        ).casefold()
    ]


def trips_with_conflicts(
    trips: list[dict[str, Any]], pool: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """为每条行程附上与之日期重叠的其它行程（不会修改传入的字典）。

    ``pool`` 用于在一页行程之外计算重叠（如 Web 列表分页时以完整历史为基准）。
    """
    context = pool if pool is not None else trips
    intervals = [(trip, trip_interval(trip)) for trip in context]
    views = []
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
        views.append({**trip, "conflicts": conflicts})
    return views


def trip_state_label(trip: dict[str, Any], today: date | None = None) -> str:
    """行程相对今天的状态：进行中 / 即将出行 / 已结束；日期缺失时返回空串。"""
    interval = trip_interval(trip)
    if interval is None:
        return ""
    start, end = interval
    current = today if today is not None else datetime.now().date()
    if start.date() <= current <= end.date():
        return "进行中"
    if start.date() > current:
        return "即将出行"
    return "已结束"
