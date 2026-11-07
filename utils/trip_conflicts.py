"""行程日期解析、重叠判断与状态标签（Web / CLI 共用）。"""
import logging
import re
from bisect import bisect_right
from collections import Counter
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


def trip_interval(trip: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """行程的日期区间（含首尾）；缺少或无法解析出发日期时返回 None。

    返程日期非法（历史脏数据）时按出发当日计算并记录警告——此前完全静默，
    行程区间被截成单日却无从察觉。
    """
    try:
        start = datetime.strptime(str(trip.get("start_date") or ""), "%Y-%m-%d")
    except ValueError:
        return None
    raw_end = trip.get("end_date")
    try:
        end = datetime.strptime(str(raw_end or ""), "%Y-%m-%d")
    except ValueError:
        if raw_end:
            logger.warning(
                "行程 %s 的返程日期无法解析（%r），按出发当日计算",
                trip.get("trip_id"),
                raw_end,
            )
        end = start
    if end < start:
        end = start
    return start, end


def find_saved_trip(
    trips: list[dict[str, Any]], trip_id: str, record: dict[str, Any]
) -> dict[str, Any] | None:
    """在重新读取的行程列表里定位「本次保存的那条」。

    历史数据可能存在多条同 trip_id 的记录：按 id 整条排除会连带隐藏其它
    行程的真实重叠，按「id + 关键字段」匹配才是精确的。
    """
    for trip in trips:
        if (
            str(trip.get("trip_id")) == str(trip_id)
            and str(trip.get("start_date") or "")
            == str(record.get("start_date") or "")
            and str(trip.get("end_date") or "")
            == str(record.get("end_date") or "")
            and str(trip.get("destination") or "")
            == str(record.get("destination") or "")
        ):
            return trip
    return None


def trip_conflicts(
    trips: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    exclude_id: str | None = None,
    exclude_trip: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """找出与给定日期区间重叠的其它行程（按出发日期排序）。

    ``exclude_trip`` 按对象身份排除本次保存的那条；``exclude_id`` 只在
    该 id 在历史里唯一时整条排除（重复 trip_id 时按 id 排除会连带隐藏
    其它同名记录的真实重叠）。
    """
    id_counts: Counter[str] | None = None
    conflicts = []
    for trip in trips:
        if exclude_trip is not None and trip is exclude_trip:
            continue
        if exclude_trip is None and exclude_id is not None:
            if id_counts is None:
                id_counts = Counter(str(item.get("trip_id")) for item in trips)
            if (
                str(trip.get("trip_id")) == exclude_id
                and id_counts[exclude_id] == 1
            ):
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
    用空格拼接后做包含匹配（大小写不敏感）；查询按任意空白（含全角空格）
    拆成多个词，所有词都出现在拼接文本中即命中（词序与字段顺序无关，
    如「上海 北京」与「北京 上海」等价）；空关键词原样返回。
    """
    needle = str(query or "").strip().casefold()
    if not needle:
        return list(trips)
    terms = [term for term in re.split(r"[\s　]+", needle) if term]
    matched = []
    for trip in trips:
        haystack = " ".join(
            str(trip.get(key) or "")
            for key in (
                "origin",
                "destination",
                "purpose",
                "start_date",
                "end_date",
            )
        ).casefold()
        if all(term in haystack for term in terms):
            matched.append(trip)
    return matched


def trips_with_conflicts(
    trips: list[dict[str, Any]], pool: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """为每条行程附上与之日期重叠的其它行程（不会修改传入的字典）。

    ``pool`` 用于在一页行程之外计算重叠（如 Web 列表分页时以完整历史为基准）。

    按开始日期排序后用二分定位候选（start ≤ 目标结束），再以结束日期过滤
    ——全量行程的冲突计算是 O(n log n + 实际重叠对数)，此前逐对比较是
    O(n²)（几千条历史行程时 CLI 列表 / --json 导出明显卡顿）。
    """
    context = pool if pool is not None else trips
    entries: list[tuple[datetime, datetime, int, dict[str, Any]]] = []
    for index, trip in enumerate(context):
        interval = trip_interval(trip)
        if interval is not None:
            entries.append((interval[0], interval[1], index, trip))
    entries.sort(key=lambda item: item[0])
    starts = [item[0] for item in entries]

    views = []
    for trip in trips:
        interval = trip_interval(trip)
        conflicts: list[dict[str, Any]] = []
        if interval is not None:
            start_dt, end_dt = interval
            # 只有开始日期不晚于目标结束的行程才可能重叠；再按结束日期过滤
            limit = bisect_right(starts, end_dt)
            for other_start, other_end, _, other in entries[:limit]:
                if other is trip or other_end < start_dt:
                    continue
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
