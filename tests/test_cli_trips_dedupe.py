"""
`python cli.py trips-dedupe` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_trips_dedupe.py
"""
import contextlib
import io
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


class _FakeStore:
    def __init__(self, trips, fail_get=False):
        self.trips = list(trips)
        self.deleted = []
        self.fail_get = fail_get
        self.closed = False

    def get_trip_history(self, limit=None):
        if self.fail_get:
            raise RuntimeError("db down")
        return list(self.trips)

    def delete_trip(self, trip_id):
        if not any(trip["trip_id"] == trip_id for trip in self.trips):
            return False
        self.trips = [trip for trip in self.trips if trip["trip_id"] != trip_id]
        self.deleted.append(trip_id)
        return True

    def close(self):
        self.closed = True


def _trip(trip_id, origin="北京", destination="上海", start="2026-11-09", end="2026-11-11"):
    return {
        "trip_id": trip_id,
        "origin": origin,
        "destination": destination,
        "start_date": start,
        "end_date": end,
    }


def _run(user_id):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_trips_dedupe_standalone(user_id)
    return code, buf.getvalue()


def test_dedupes_and_keeps_first(monkeypatch):
    fake = _FakeStore(
        [_trip("trip_1"), _trip("trip_2"), _trip("trip_3", destination="杭州")]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == ["trip_2"], "同路线同日期只保留最早一条"
    assert "删除 1 条重复行程" in out
    assert fake.closed
    print("✓ 删除完全重复的行程（保留最早）")


def test_undated_trips_not_deduped(monkeypatch):
    fake = _FakeStore(
        [_trip("trip_1", start=None, end=None), _trip("trip_2", start=None, end=None)]
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0
    assert fake.deleted == []
    assert "没有重复行程（共 2 条）" in out
    print("✓ 缺少出发日期的行程不参与去重")


def test_no_trips(monkeypatch):
    fake = _FakeStore([])
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 0 and "共 0 条" in out
    print("✓ 无行程时正常返回")


def test_invalid_user_id(monkeypatch):
    called = {"n": 0}

    def fake_factory(user_id, path):
        called["n"] += 1
        return _FakeStore([])

    monkeypatch.setattr(storage, "create_long_term_memory", fake_factory)
    code, out = _run("../etc/passwd")
    assert code == 1
    assert "用户 ID" in out
    assert called["n"] == 0, "非法 ID 不应打开存储"
    print("✓ 非法用户 ID 被拒绝")


def test_read_failure(monkeypatch):
    fake = _FakeStore([], fail_get=True)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda user_id, path: fake
    )
    code, out = _run("u1")
    assert code == 1 and "读取行程失败" in out
    assert fake.closed
    print("✓ 读取失败返回 1")


def test_store_open_failure(monkeypatch):
    def boom(user_id, path):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run("u1")
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ 存储打开失败返回 1")


if __name__ == "__main__":
    for case in (
        test_dedupes_and_keeps_first,
        test_undated_trips_not_deduped,
        test_no_trips,
        test_invalid_user_id,
        test_read_failure,
        test_store_open_failure,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
