"""行程日期解析、重叠判断与状态标签（Web / CLI 共用）。"""
import heapq
import logging
import re
from collections import Counter
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _start_sort_value(trip: dict[str, Any]) -> tuple[datetime, str]:
    """冲突列表排序键：按解析后的出发日期（未补零日期不再按字符串错序）。

    历史数据支持 ``2026-1-5`` 这类未补零日期：字符串比较下
    ``2026-1-15`` 会排在 ``2026-1-2`` 前面；无法解析的值统一排在最前，
    以原始文本稳定次序。
    """
    raw = str(trip.get("start_date") or "")
    try:
        return datetime.strptime(raw, "%Y-%m-%d"), raw
    except ValueError:
        return datetime.min, raw


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
        logger.warning(
            "行程 %s 的返程日期早于出发日期（%r < %r），按出发当日计算",
            trip.get("trip_id"),
            raw_end,
            trip.get("start_date"),
        )
        end = start
    return start, end


def find_saved_trip(
    trips: list[dict[str, Any]], trip_id: str, record: dict[str, Any]
) -> dict[str, Any] | None:
    """在重新读取的行程列表里定位「本次保存的那条」。

    历史数据可能存在多条同 trip_id 的记录：按 id 整条排除会连带隐藏其它
    行程的真实重叠，按「id + 关键字段」匹配才是精确的。关键字段按存储去重
    同一口径归一（未补零日期 / CRLF 换行），否则命中未补零的历史行时定位
    不到本次记录，刚保存的行程会被报成与自己重叠。
    """
    from utils.trip_export import normalize_identity_date, normalize_identity_text

    want_start = normalize_identity_date(record.get("start_date"))
    want_end = (
        normalize_identity_date(record.get("end_date")) or want_start
    )
    want_destination = normalize_identity_text(record.get("destination"))
    want_origin = normalize_identity_text(record.get("origin"))
    for trip in trips:
        if str(trip.get("trip_id")) != str(trip_id):
            continue
        trip_start = normalize_identity_date(trip.get("start_date"))
        trip_end = normalize_identity_date(trip.get("end_date")) or trip_start
        if (
            trip_start != want_start
            or trip_end != want_end
            or normalize_identity_text(trip.get("destination")) != want_destination
        ):
            continue
        if (
            # 调用方未提供 origin 时保持旧口径（不参与匹配）；提供时
            # 用于区分「同 id / 同日 / 同目的地但出发地不同」的历史副本
            "origin" in record
            and normalize_identity_text(trip.get("origin")) != want_origin
        ):
            continue
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
    conflicts.sort(key=_start_sort_value)
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
    entries: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for trip in context:
        interval = trip_interval(trip)
        if interval is not None:
            entries.append((interval[0], interval[1], trip))
    entries.sort(key=lambda item: item[0])

    views: list[dict[str, Any]] = []
    view_indices: dict[int, list[int]] = {}
    for index, trip in enumerate(trips):
        views.append({**trip, "conflicts": []})
        view_indices.setdefault(id(trip), []).append(index)

    # 扫描线：按开始日期推进，维护仍在进行的行程堆——每对重叠只枚举一次，
    # O(n log n + 实际重叠对数)。此前对每个目标都遍历前缀切片，零重叠的
    # 常见场景仍是 Θ(n²)（几千条历史时 CLI 列表 / --json 明显卡顿）
    active: list[tuple[datetime, int, dict[str, Any]]] = []
    sequence = 0
    for start_dt, end_dt, trip in entries:
        while active and active[0][0] < start_dt:
            heapq.heappop(active)
        for _other_end, _seq, other in active:
            if other is trip:
                continue
            for view_index in view_indices.get(id(trip), ()):
                views[view_index]["conflicts"].append(other)
            for view_index in view_indices.get(id(other), ()):
                views[view_index]["conflicts"].append(trip)
        heapq.heappush(active, (end_dt, sequence, trip))
        sequence += 1

    for view in views:
        view["conflicts"].sort(key=_start_sort_value)
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
