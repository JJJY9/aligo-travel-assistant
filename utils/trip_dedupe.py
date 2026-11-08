"""行程序列去重（CLI ``trips-dedupe`` 与 Web ``/api/trips/dedupe`` 共用）。

判定口径与存储层「同路线 + 同起止日期」去重一致：历史未补零日期先归一，
否则同一行程的两种写法（2026-1-5 / 2026-01-05）不会被识别为重复。

安全约束（与 CLI 原有实现保持）：
- 同路线 + 同起止日期为一组，保留最早写入的一条，删除其余；
- 删除前重新读取：保留项已被并发删除的组整组跳过，避免把仅剩副本也删掉；
- 同一 trip_id 出现在多条不同行程里时不自动处理（``delete_trip`` 按 id
  删最早一条，可能删错行），由调用方提示人工清理；
- 同 id 副本（旧版本给两条行程生成过同一个 trip_id）用
  ``delete_duplicate_trip`` 删除最后插入的一条。
"""
from typing import Any

from utils.trip_import import normalize_identity_date


def _identity(trip: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """与存储层去重同一口径的行程身份；缺出发日期返回 None。"""
    start_date = normalize_identity_date(trip.get("start_date"))
    if not start_date:
        return None
    return (
        str(trip.get("origin") or ""),
        str(trip.get("destination") or ""),
        start_date,
        normalize_identity_date(trip.get("end_date") or start_date),
    )


def _entry(trip_id: str, by_id: dict[str, dict[str, Any]]) -> dict[str, str]:
    trip = by_id.get(trip_id, {})
    return {
        "trip_id": trip_id,
        "origin": str(trip.get("origin") or ""),
        "destination": str(trip.get("destination") or ""),
        "start_date": str(trip.get("start_date") or ""),
        "end_date": str(trip.get("end_date") or ""),
    }


def plan_trip_dedupe(store: Any) -> dict[str, Any]:
    """读取全部行程并生成去重计划（只读，不处理任何记录）。

    Returns:
        ``total_trips`` / ``duplicates``（待删除条目的展示字段）/
        ``duplicate_entries``（(保留 id, 删除 id) 对，供执行）/
        ``ambiguous_ids``（同 id 多行程，需人工处理）。

    Raises:
        Exception: 读取行程失败（调用方负责映射为 503 / 退出码）。
    """
    trips = store.get_trip_history(limit=None)
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for trip in trips:
        key = _identity(trip)
        if key is None:
            continue
        groups.setdefault(key, []).append(str(trip.get("trip_id") or ""))
    by_id = {str(trip.get("trip_id") or ""): trip for trip in trips}

    # 同一 trip_id 出现在多条不同行程里时，delete_trip 可能删错行：
    # 这类 id 不自动处理，提示人工清理
    id_groups: dict[str, set[tuple[str, str, str, str]]] = {}
    for key, ids in groups.items():
        for trip_id in ids:
            id_groups.setdefault(trip_id, set()).add(key)
    ambiguous = {trip_id for trip_id, keys in id_groups.items() if len(keys) > 1}

    # 删除前重新读取：若某组保留的那条已被并发删除，跳过该组，避免把
    # 仅剩的副本也删掉。JSON 后端的 get_trip_history 返回进程启动时的
    # 快照，必须先 reload，否则复核永远是「未变化」
    reload = getattr(store, "reload", None)
    if callable(reload):
        reload()
    fresh_ids = {
        str(trip.get("trip_id") or "")
        for trip in store.get_trip_history(limit=None)
    }

    duplicate_entries = [
        (ids[0], trip_id)
        for ids in groups.values()
        for trip_id in ids[1:]
        if ids[0] in fresh_ids and trip_id not in ambiguous
    ]
    return {
        "total_trips": len(trips),
        "groups_with_duplicates": sum(
            1 for ids in groups.values() if len(ids) > 1
        ),
        "duplicates": [
            _entry(trip_id, by_id) for _, trip_id in duplicate_entries
        ],
        "duplicate_entries": duplicate_entries,
        "ambiguous_ids": sorted(ambiguous),
    }


def apply_trip_dedupe(
    store: Any, duplicate_entries: list[tuple[str, str]]
) -> tuple[int, str | None]:
    """执行去重计划，返回 ``(已删除数, 失败原因或 None)``。

    部分删除后失败要如实返回已删除数（数据已部分变更，调用方不能谎报
    完全失败）。
    """
    removed = 0
    try:
        for kept_id, trip_id in duplicate_entries:
            if not trip_id:
                continue
            # 与组内保留项同 id 的副本：delete_trip 删最早一条，会把要保留
            # 的记录删掉、留下后来的重复行——同 id 时必须删除最后插入的一条
            deleter = (
                store.delete_duplicate_trip
                if trip_id == kept_id and hasattr(store, "delete_duplicate_trip")
                else store.delete_trip
            )
            if deleter(trip_id):
                removed += 1
    except Exception as exc:  # noqa: BLE001 - 部分删除后失败要如实报告
        return removed, str(exc)
    return removed, None
