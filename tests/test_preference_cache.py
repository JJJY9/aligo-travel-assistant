"""偏好快照缓存测试（v2.0，纯 Python）。

覆盖：JSON 后端不缓存、PG 后端命中缓存、深拷贝隔离、显式失效、TTL 过期、
编排层写入后失效。

运行：python tests/test_preference_cache.py
"""
import os
import shutil
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context import memory_manager as memory_manager_module
from context.memory_manager import MemoryManager


class _FakeStore:
    snapshot_based = False

    def __init__(self, snapshot_based=False):
        self.snapshot_based = snapshot_based
        self.reads = 0

    def get_preference(self):
        self.reads += 1
        return {"hotel_brands": ["汉庭"]}


def _manager():
    storage = tempfile.mkdtemp()
    manager = MemoryManager(
        user_id="pref_cache_user",
        session_id="pref_cache_session",
        storage_path=storage,
        llm_model=None,
    )
    return manager, storage


def _with_pg(monkeypatch):
    from config import STORAGE_CONFIG

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://dummy")


def test_json_backend_does_not_cache(monkeypatch):
    """JSON 后端读取本身在内存里，不启用快照缓存。"""
    from config import STORAGE_CONFIG

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=True)
        manager.long_term = store
        manager.get_preferences()
        manager.get_preferences()
        assert store.reads == 2
        print("✓ JSON 后端不缓存偏好")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_pg_backend_cache_hit_and_invalidation(monkeypatch):
    """PG 后端命中缓存；返回值是深拷贝；显式失效后重新读取。"""
    _with_pg(monkeypatch)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        first = manager.get_preferences()
        second = manager.get_preferences()
        assert store.reads == 1, "5 秒内应命中快照缓存"
        assert second == first
        first["hotel_brands"].append("如家")
        assert manager.get_preferences()["hotel_brands"] == ["汉庭"], "返回值必须是深拷贝"
        manager.invalidate_preference_cache()
        manager.get_preferences()
        assert store.reads == 2, "失效后应重新读取"
        print("✓ PG 偏好缓存命中 / 深拷贝 / 显式失效")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_invalidate_all_preference_caches(monkeypatch):
    """面板 / 导入直写时一次清空本进程所有实例的偏好快照。"""
    _with_pg(monkeypatch)
    first, first_storage = _manager()
    second, second_storage = _manager()
    try:
        first_store = _FakeStore(snapshot_based=False)
        second_store = _FakeStore(snapshot_based=False)
        first.long_term = first_store
        second.long_term = second_store
        first.get_preferences()
        second.get_preferences()
        assert first_store.reads == second_store.reads == 1

        MemoryManager.invalidate_all_preference_caches()

        first.get_preferences()
        second.get_preferences()
        assert first_store.reads == 2, "全局失效后第一个实例应重新读取"
        assert second_store.reads == 2, "全局失效后第二个实例应重新读取"
        print("✓ 全局失效清空所有实例偏好缓存")
    finally:
        shutil.rmtree(first_storage, ignore_errors=True)
        shutil.rmtree(second_storage, ignore_errors=True)


def test_pg_cache_ttl_expiry(monkeypatch):
    """超过 TTL 后重新读取。"""
    _with_pg(monkeypatch)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        now = [1000.0]
        monkeypatch.setattr(
            memory_manager_module.time, "monotonic", lambda: now[0]
        )
        manager.get_preferences()
        now[0] = 1004.0
        manager.get_preferences()
        assert store.reads == 1
        now[0] = 1006.0
        manager.get_preferences()
        assert store.reads == 2, "超过 5 秒 TTL 应重新读取"
        print("✓ 偏好缓存 TTL 过期")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_orchestration_invalidates_after_preference_write():
    """编排层写入偏好后立即失效缓存。"""
    from agents.orchestration_agent import OrchestrationAgent

    class _LongTerm:
        def __init__(self):
            self.saved = []

        def save_preference(self, pref_type, value):
            self.saved.append((pref_type, value))

        def append_preference(self, pref_type, value):
            self.saved.append((pref_type, value))

    class _Memory:
        def __init__(self):
            self.long_term = _LongTerm()
            self.invalidated = 0

        def invalidate_preference_cache(self):
            self.invalidated += 1

    memory = _Memory()
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=memory)
    orchestrator._apply_single_preference("hotel_brands", "汉庭", "replace")
    assert memory.long_term.saved == [("hotel_brands", "汉庭")]
    assert memory.invalidated == 1
    orchestrator._apply_single_preference("airlines", "东航", "append")
    assert memory.invalidated == 2
    print("✓ 偏好写入后立即失效快照缓存")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    import pytest

    for _case in (
        test_json_backend_does_not_cache,
        test_pg_backend_cache_hit_and_invalidation,
        test_invalidate_all_preference_caches,
        test_pg_cache_ttl_expiry,
    ):
        _mp = pytest.MonkeyPatch()
        try:
            _case(_mp)
        finally:
            _mp.undo()
    test_orchestration_invalidates_after_preference_write()
    print("\n全部通过")
