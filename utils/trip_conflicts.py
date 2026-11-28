"""行程日期解析、重叠判断与状态标签（Web / CLI 共用）。"""
import functools
import heapq
import logging
import re
from collections import Counter
from datetime import date, datetime
from itertools import islice
from typing import Any

from utils.trip_export import normalize_identity_date

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=16384)
def _parse_iso_date(text: str) -> datetime | None:
    """解析归一后的 ``YYYY-MM-DD``；同一日期串只 strptime 一次。

    ``/api/status`` 对全量行程做三遍日期解析、``/api/trips`` 每页对全池算
    冲突，真实账号上限 5000 条（起止两种日期串）：缓存键恒为 str，覆盖
    2×5000 后留有余量，顺序扫描也不会互相 thrash。
    """
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_trip_date(value: Any) -> datetime | None:
    """按身份口径解析行程日期：补零 / 斜杠 / 全角数字先归一再 strptime。

    只认 ``%Y-%m-%d`` 会让备份恢复按保真保留的斜杠日期（2026/03/12，
    存储身份 / 导出 / 导入都视为合法日期）在冲突检测、状态徽标与统计里
    静默消失；非真实日历日（2026-02-30）归一后仍被 strptime 拒绝。
    """
    text = normalize_identity_date(value)
    if not text:
        return None
    return _parse_iso_date(text)


@functools.lru_cache(maxsize=8192)
def _start_sort_key(raw: str) -> tuple[datetime, str]:
    """冲突列表排序键（按解析后的出发日期），同一日期字符串只解析一次。

    同一条行程会出现在多达 20 个视图的冲突列表里，逐视图 strptime 是
    O(n×20) 的重复解析；缓存有界且只存日期字符串。
    """
    parsed = _parse_trip_date(raw)
    return (parsed if parsed is not None else datetime.min, raw)


def _start_sort_value(trip: dict[str, Any]) -> tuple[datetime, str]:
    """冲突列表排序键：按解析后的出发日期（未补零日期不再按字符串错序）。

    历史数据支持 ``2026-1-5`` / ``2026/3/12`` 这类写法：字符串比较下
    ``2026-1-15`` 会排在 ``2026-1-2`` 前面；无法解析的值统一排在最前，
    以原始文本稳定次序。
    """
    return _start_sort_key(str(trip.get("start_date") or ""))


_WARNED_TRIP_ISSUES: set[tuple[str, str]] = set()
# 去重集合上限：脏值种类有界，超过后整体清空重新计数，避免长期运行无界增长
_WARNED_TRIP_ISSUES_MAX = 256


def _warn_trip_issue_once(
    kind: str, raw_value: Any, message: str, *args: Any
) -> None:
    """同一类脏日期只警告一次（有界去重集合）。

    ``/api/status`` 对全量历史每条约调用 3 次 ``trip_interval``：备份恢复
    保留的坏日期此前每次面板刷新都会放大成数万条 WARNING，把有价值的日志
    全部冲掉（截断 / 早于出发仍按原语义处理，只是不再逐次记录）。
    """
    token = (kind, repr(raw_value))
    if token in _WARNED_TRIP_ISSUES:
        return
    if len(_WARNED_TRIP_ISSUES) >= _WARNED_TRIP_ISSUES_MAX:
        _WARNED_TRIP_ISSUES.clear()
    _WARNED_TRIP_ISSUES.add(token)
    logger.warning(message, *args)


def trip_interval(trip: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """行程的日期区间（含首尾）；缺少或无法解析出发日期时返回 None。

    日期按身份口径归一后解析：备份恢复按保真保留的斜杠日期（2026/03/12）
    与未补零日期（2026-1-5）同口径参与冲突 / 状态 / 统计。返程日期非法
    （历史脏数据）时按出发当日计算并记录警告（同一脏值只记一次）——此前
    完全静默，行程区间被截成单日却无从察觉。
    """
    raw_start = trip.get("start_date")
    start = _parse_trip_date(raw_start)
    if start is None:
        if raw_start:
            _warn_trip_issue_once(
                "unparsable-start",
                raw_start,
                "行程 %s 的出发日期无法解析（%r），不参与冲突 / 状态统计",
                trip.get("trip_id"),
                raw_start,
            )
        return None
    raw_end = trip.get("end_date")
    end = _parse_trip_date(raw_end)
    if end is None:
        if raw_end:
            _warn_trip_issue_once(
                "unparsable-end",
                raw_end,
                "行程 %s 的返程日期无法解析（%r），按出发当日计算",
                trip.get("trip_id"),
                raw_end,
            )
        end = start
    elif end.year == 9999:
        # 9999-12-31 是模型常用的「返程未定」占位（与对话写入路径
        # agents/orchestration_agent 丢弃 9999 返程的口径一致）：按真实
        # 区间会把出行天数算成数百万天、行程永久「进行中」并与所有未来
        # 行程互相重叠。读侧统一按出发当日计算，存储与导出仍保真保留。
        _warn_trip_issue_once(
            "placeholder-end",
            (raw_end, trip.get("start_date")),
            "行程 %s 的返程日期为 9999-12-31 占位（%r），按出发当日计算",
            trip.get("trip_id"),
            raw_end,
        )
        end = start
    if end < start:
        _warn_trip_issue_once(
            "inverted",
            (raw_end, trip.get("start_date")),
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
    # 查询区间与 trip_interval 同一口径：9999-12-31 是「返程未定」占位、
    # 倒置区间按单日。调用方（Web 新增 / 编辑、CLI trip-add / trip-edit）
    # 传的是原始解析值，不归一会让占位行程与之后所有行程误报重叠。
    if end < start or end.year == 9999:
        end = start
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


# 每条行程最多物化的冲突条目：同一日期导入数千条行程时，逐对物化是
# O(n²) 引用与序列化体积（5000 条同日行程约 1250 万对，--json 直接 OOM）。
# 真实重叠数量仍由 conflict_count 精确给出，展示取前 N 条即可。
_MAX_CONFLICT_ENTRIES = 20


def trips_with_conflicts(
    trips: list[dict[str, Any]],
    pool: list[dict[str, Any]] | None = None,
    *,
    intervals: dict[int, Any] | None = None,
) -> list[dict[str, Any]]:
    """为每条行程附上与之日期重叠的其它行程（不会修改传入的字典）。

    ``pool`` 用于在一页行程之外计算重叠（如 Web 列表分页时以完整历史为基准）。
    ``intervals`` 可选传入按 ``id(trip)`` 预计算的日期区间表：调用方在同一
    请求内已解析过这些行程的日期时（如 /api/status）直接复用，避免对上限
    5000 条行程再全量解析一遍。

    返回的每条视图含 ``conflicts``（重叠行程，至多 ``_MAX_CONFLICT_ENTRIES``
    条，按出发日期排序）与 ``conflict_count``（真实重叠总数）。按开始日期
    排序后用扫描线统计：每个实例记录插入时已重叠的数量与仍在活动集中的
    推送数，冲突总数 O(n log n) 精确求出，无需枚举 O(n²) 的每一对；物化
    只取前 N 条。此前逐对 append 到双方视图，几千条同日行程时列表 / JSON
    直接卡死或 OOM。
    """
    context = pool if pool is not None else trips
    entries: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for trip in context:
        if isinstance(trip, dict) and intervals is not None and id(trip) in intervals:
            interval = intervals[id(trip)]
        else:
            interval = trip_interval(trip)
        if interval is not None:
            entries.append((interval[0], interval[1], trip))
    entries.sort(key=lambda item: item[0])

    views: list[dict[str, Any]] = []
    view_indices: dict[int, list[int]] = {}
    for index, trip in enumerate(trips):
        views.append({**trip, "conflicts": [], "conflict_count": 0})
        view_indices.setdefault(id(trip), []).append(index)

    # 扫描线：active_rows 按推送顺序保存仍在进行的行程（取前 N 条即
    # 「最早开始的重叠行程」）；heap 按结束日期弹出已结束的实例
    active: list[tuple[datetime, int, dict[str, Any]]] = []
    active_rows: dict[int, dict[str, Any]] = {}
    active_counts: dict[int, int] = {}
    insertion_overlap: dict[int, int] = {}
    counts: dict[int, int] = {}
    push_epoch = 0
    for start_dt, end_dt, trip in entries:
        while active and active[0][0] < start_dt:
            _other_end, seq, popped = heapq.heappop(active)
            active_rows.pop(seq, None)
            remaining_active = active_counts.get(id(popped), 1) - 1
            if remaining_active:
                active_counts[id(popped)] = remaining_active
            else:
                active_counts.pop(id(popped), None)
            # 插入时已重叠的 + 插入后仍在活动集期间推送的 = 本实例的冲突数
            instance_overlap = insertion_overlap.pop(seq, 0) + (push_epoch - seq)
            counts[id(popped)] = counts.get(id(popped), 0) + instance_overlap

        overlap_now = len(active_rows) - active_counts.get(id(trip), 0)
        sampled: list[dict[str, Any]] = []
        if overlap_now > 0:
            for other in islice(
                active_rows.values(),
                _MAX_CONFLICT_ENTRIES + active_counts.get(id(trip), 0),
            ):
                if other is trip:
                    continue
                sampled.append(other)
                if len(sampled) >= _MAX_CONFLICT_ENTRIES:
                    break
        for other in sampled:
            for view_index in view_indices.get(id(trip), ()):
                conflict_list = views[view_index]["conflicts"]
                if len(conflict_list) < _MAX_CONFLICT_ENTRIES:
                    conflict_list.append(other)
            for view_index in view_indices.get(id(other), ()):
                conflict_list = views[view_index]["conflicts"]
                if len(conflict_list) < _MAX_CONFLICT_ENTRIES:
                    conflict_list.append(trip)

        push_epoch += 1
        insertion_overlap[push_epoch] = overlap_now
        active_rows[push_epoch] = trip
        active_counts[id(trip)] = active_counts.get(id(trip), 0) + 1
        heapq.heappush(active, (end_dt, push_epoch, trip))

    # 仍在活动集中的实例：统计到扫描结束时的推送数
    while active:
        _other_end, seq, trip = heapq.heappop(active)
        instance_overlap = insertion_overlap.pop(seq, 0) + (push_epoch - seq)
        counts[id(trip)] = counts.get(id(trip), 0) + instance_overlap

    for index, view in enumerate(views):
        view["conflict_count"] = counts.get(id(trips[index]), 0)
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
