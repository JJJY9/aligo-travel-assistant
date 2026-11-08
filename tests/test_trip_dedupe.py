"""行程去重共享逻辑（utils/trip_dedupe）单元测试（无网络 / 无真实存储）。

CLI ``trips-dedupe`` 与 Web ``POST /api/trips/dedupe`` 共用本模块：
覆盖身份归一、组内保留最早、并发删除复核、同 id 副本处理与部分失败。
运行：python tests/test_trip_dedupe.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.trip_dedupe import apply_trip_dedupe, plan_trip_dedupe


def _trip(trip_id, destination="上海", start="2026-01-05", end="2026-01-06", **extra):
    row = {
        "trip_id": trip_id,
        "origin": "北京",
        "destination": destination,
        "start_date": start,
        "end_date": end,
    }
    row.update(extra)
    return row


class _Store:
    def __init__(self, trips, fresh=None, fail_delete_at=None):
        self.trips = list(trips)
        self.fresh = fresh
        self.deleted = []
        self.reloads = 0
        self.fail_delete_at = fail_delete_at
        self.delete_calls = 0

    def get_trip_history(self, limit=None):
        return list(self.trips)

    def reload(self):
        self.reloads += 1
        if self.fresh is not None:
            self.trips = list(self.fresh)

    def _remove(self, trip_id, last=False):
        self.delete_calls += 1
        if self.fail_delete_at is not None and self.delete_calls >= self.fail_delete_at:
            raise RuntimeError("store offline")
        indices = [
            index
            for index, trip in enumerate(self.trips)
            if str(trip.get("trip_id")) == trip_id
        ]
        if not indices:
            return False
        index = indices[-1] if last else indices[0]
        self.trips.pop(index)
        self.deleted.append(trip_id)
        return True

    def delete_trip(self, trip_id):
        return self._remove(trip_id, last=False)

    def delete_duplicate_trip(self, trip_id):
        return self._remove(trip_id, last=True)


def test_plan_keeps_earliest_and_normalizes_dates():
    """同路线 + 同起止日期为一组（未补零日期归一），保留最早一条。"""
    store = _Store(
        [
            _trip("t1", start="2026-1-5", end="2026-1-6"),
            _trip("t2", start="2026-01-05", end="2026-01-06"),
            _trip("t3", destination="杭州", start="2026-02-01", end="2026-02-02"),
        ]
    )
    plan = plan_trip_dedupe(store)
    assert plan["total_trips"] == 3
    assert plan["groups_with_duplicates"] == 1
    assert plan["duplicate_entries"] == [("t1", "t2")]
    assert [entry["trip_id"] for entry in plan["duplicates"]] == ["t2"]
    assert store.reloads == 1, "删除前必须重新读取"
    print("✓ 去重计划保留最早并归一日期的身份")


def test_plan_skips_group_when_kept_row_vanished():
    """保留项已被并发删除：整组跳过，避免把仅剩副本也删掉。"""
    store = _Store(
        [_trip("t1"), _trip("t2")],
        fresh=[_trip("t2")],
    )
    plan = plan_trip_dedupe(store)
    assert plan["groups_with_duplicates"] == 1
    assert plan["duplicate_entries"] == [], "保留项不存在时不能删除其余副本"
    print("✓ 保留项并发消失时整组跳过")


def test_plan_reports_ambiguous_ids_separately():
    """同一 trip_id 属于多条不同行程：不自动删除，单独报告。"""
    store = _Store(
        [
            _trip("dup2", destination="上海"),
            _trip("dup", destination="上海"),
            _trip("dup", destination="杭州"),
            _trip("dup3", destination="上海"),
        ]
    )
    plan = plan_trip_dedupe(store)
    assert plan["ambiguous_ids"] == ["dup"]
    assert plan["duplicate_entries"] == [("dup2", "dup3")]
    print("✓ 歧义 trip_id 不自动处理")


def test_plan_ignores_rows_without_start_date():
    store = _Store(
        [
            _trip("t1", start=None),
            _trip("t2", start=None),
            _trip("t3"),
        ]
    )
    plan = plan_trip_dedupe(store)
    assert plan["total_trips"] == 3
    assert plan["groups_with_duplicates"] == 0
    print("✓ 缺出发日期的行程不参与去重")


def test_apply_uses_duplicate_deleter_for_same_kept_id():
    """组内同 id 副本：删除最后插入的一条，不能删掉要保留的最早记录。"""
    store = _Store([])
    entries = [("t1", "t1")]
    removed, failure = apply_trip_dedupe(store, entries)
    assert (removed, failure) == (0, None)
    assert store.delete_calls == 1, "同 id 副本仍要通过 delete_duplicate_trip 处理"
    print("✓ 同 id 副本使用 delete_duplicate_trip")


def test_apply_reports_partial_failure():
    """删除中途失败：如实返回已删除数与原因。"""
    store = _Store([_trip("t1"), _trip("t2")], fail_delete_at=1)
    removed, failure = apply_trip_dedupe(store, [("k", "t1"), ("k2", "t2")])
    assert removed == 0
    assert failure and "store offline" in failure
    print("✓ 部分删除失败如实返回")


if __name__ == "__main__":
    test_plan_keeps_earliest_and_normalizes_dates()
    test_plan_skips_group_when_kept_row_vanished()
    test_plan_reports_ambiguous_ids_separately()
    test_plan_ignores_rows_without_start_date()
    test_apply_uses_duplicate_deleter_for_same_kept_id()
    test_apply_reports_partial_failure()
    print("\n全部通过")
