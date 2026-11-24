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

    def __init__(self, snapshot_based=False, events=None):
        self.snapshot_based = snapshot_based
        self.reads = 0
        self.events = events

    def get_preference(self):
        self.reads += 1
        if self.events is not None:
            self.events.append("read")
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


class _FakeRedis:
    """最小 Redis 替身：只实现偏好版本用到的 GET / INCR / EXPIRE。"""

    def __init__(self):
        self.data = {}
        self.get_calls = 0
        self.incr_calls = []
        self.expire_calls = []
        self.events = None  # 可选：与 _FakeStore 共用的调用序列

    def get(self, key):
        self.get_calls += 1
        if self.events is not None:
            self.events.append("version")
        return self.data.get(key)

    def incr(self, key):
        self.incr_calls.append(key)
        if self.events is not None:
            self.events.append("incr")
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        return True


class _BrokenRedis:
    """模拟 Redis 故障：读写都抛连接错误。"""

    def __init__(self):
        self.get_calls = 0
        self.incr_calls = []

    def get(self, key):
        self.get_calls += 1
        raise ConnectionError("redis down")

    def incr(self, key):
        self.incr_calls.append(key)
        raise ConnectionError("redis down")


def _with_redis(monkeypatch, client):
    from context import storage as storage_module

    # 冷却状态跨用例共享：每个用例从干净的冷却状态开始
    storage_module.reset_pref_version_failure()
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: client
    )


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


def test_inflight_read_does_not_repopulate_after_invalidate(monkeypatch):
    """读库期间发生失效：写前快照不得回填缓存（下次读取必须回源）。"""
    import threading

    _with_pg(monkeypatch)
    store = _FakeStore(snapshot_based=False)
    manager, storage = _manager()
    try:
        manager.long_term = store
        gate = threading.Event()
        started = threading.Event()
        real_get = store.get_preference

        def blocking_get():
            started.set()
            gate.wait(5)
            return real_get()

        store.get_preference = blocking_get
        result = {}
        reader = threading.Thread(
            target=lambda: result.update(manager.get_preferences())
        )
        reader.start()
        assert started.wait(5), "读线程未进入读库"
        # 面板写入的全局失效发生在读库与回填之间
        manager.invalidate_preference_cache()
        gate.set()
        reader.join(5)
        assert result["hotel_brands"] == ["汉庭"], result

        reads_before = store.reads
        manager.get_preferences()
        assert store.reads == reads_before + 1, "失效后的在途读不能回填缓存"
    finally:
        shutil.rmtree(storage, ignore_errors=True)
    print("✓ 读库期间失效：在途读不回填缓存")


def test_instance_registry_and_invalidate_all_share_lock(monkeypatch):
    """实例注册与全局失效共享类级锁（WeakSet 迭代期间注册不打断失效）。"""
    import weakref

    class _ProbeSet(weakref.WeakSet):
        def add(self, item):
            assert MemoryManager._preference_cache_instances_lock.locked(), (
                "实例注册必须持类级锁"
            )
            super().add(item)

        def __iter__(self):
            assert MemoryManager._preference_cache_instances_lock.locked(), (
                "全局失效的快照迭代必须持类级锁"
            )
            return super().__iter__()

    monkeypatch.setattr(
        MemoryManager, "_preference_cache_instances", _ProbeSet()
    )
    first, first_storage = _manager()
    second, second_storage = _manager()
    try:
        first._preference_cache = (0.0, None, {"a": 1})
        second._preference_cache = (0.0, None, {"a": 2})
        MemoryManager.invalidate_all_preference_caches()
        assert first._preference_cache is None
        assert second._preference_cache is None
    finally:
        shutil.rmtree(first_storage, ignore_errors=True)
        shutil.rmtree(second_storage, ignore_errors=True)
    print("✓ 实例注册 / 全局失效的类级锁快照")


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


def test_shared_version_invalidates_cross_process_cache(monkeypatch):
    """其它进程写入偏好（版本 INCR）后本进程立即重读，不等满 TTL。"""
    from utils.metrics import REQUEST_METRICS

    def pref_metrics():
        return REQUEST_METRICS.snapshot()["preference_cache"]

    _with_pg(monkeypatch)
    redis = _FakeRedis()
    _with_redis(monkeypatch, redis)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        baseline = pref_metrics()
        manager.get_preferences()
        manager.get_preferences()
        assert store.reads == 1, "版本未变应命中缓存"
        after_hit = pref_metrics()
        assert after_hit["hits"] - baseline["hits"] == 1
        assert after_hit["misses"] - baseline["misses"] == 1
        assert (
            after_hit["version_invalidations"]
            - baseline["version_invalidations"]
            == 0
        )
        redis.incr("aligo:prefver:pref_cache_user")  # 模拟其它实例 / CLI 写入
        manager.get_preferences()
        assert store.reads == 2, "版本变化后应立即重新读取"
        after_change = pref_metrics()
        assert after_change["misses"] - after_hit["misses"] == 1
        assert (
            after_change["version_invalidations"]
            - after_hit["version_invalidations"]
            == 1
        )
        print("✓ 共享版本变化立即失效跨进程偏好缓存（计入指标）")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_shared_version_failure_keeps_ttl_cache(monkeypatch):
    """Redis 故障时退回原有 TTL 缓存语义，并进入 60 秒本地冷却。"""
    _with_pg(monkeypatch)
    redis = _BrokenRedis()
    _with_redis(monkeypatch, redis)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        manager.get_preferences()
        manager.get_preferences()
        assert store.reads == 1, "Redis 故障时按 TTL 命中缓存"
        # 操作级冷却：故障期不能每次读都等满 socket 超时
        assert redis.get_calls == 1, "冷却期内不应再触达 Redis"
        print("✓ Redis 故障时偏好缓存按 TTL 兜底并进入冷却")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_version_protocol_order(monkeypatch):
    """锁死读侧顺序：先取版本再读偏好；缓存命中时只取版本不读库。"""
    _with_pg(monkeypatch)
    events = []
    redis = _FakeRedis()
    redis.events = events
    _with_redis(monkeypatch, redis)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False, events=events)
        manager.long_term = store
        manager.get_preferences()
        assert events == ["version", "read"], events
        events.clear()
        manager.get_preferences()
        assert events == ["version"], "命中缓存不应再读库"
        print("✓ 版本先于偏好读取，缓存命中只取版本")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_version_invalidation_requires_fresh_snapshot(monkeypatch):
    """TTL 已过期的 miss 不算版本失效（无论如何都要回源）。"""
    from utils.metrics import REQUEST_METRICS

    _with_pg(monkeypatch)
    redis = _FakeRedis()
    _with_redis(monkeypatch, redis)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        now = [1000.0]
        monkeypatch.setattr(
            memory_manager_module.time, "monotonic", lambda: now[0]
        )
        manager.get_preferences()
        baseline = REQUEST_METRICS.snapshot()["preference_cache"]
        redis.incr("aligo:prefver:pref_cache_user")  # 其它进程写入
        now[0] = 1006.0  # 快照已过 5 秒 TTL
        manager.get_preferences()
        latest = REQUEST_METRICS.snapshot()["preference_cache"]
        assert latest["misses"] - baseline["misses"] == 1
        assert (
            latest["version_invalidations"]
            == baseline["version_invalidations"]
        ), "TTL 过期导致的回源不能计为版本失效"
        print("✓ TTL 过期的回源不计入版本失效")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_version_recovery_from_none_not_counted(monkeypatch):
    """版本从不可读（None）恢复不算版本变化：TTL 内继续命中且不误计。"""
    from context import storage as storage_module
    from utils.metrics import REQUEST_METRICS

    _with_pg(monkeypatch)
    broken = _BrokenRedis()
    _with_redis(monkeypatch, broken)
    manager, storage = _manager()
    try:
        store = _FakeStore(snapshot_based=False)
        manager.long_term = store
        manager.get_preferences()  # 版本不可读：快照记录 None
        assert manager._preference_cache[1] is None
        # Redis 恢复（期间没有任何偏好写入），冷却结束
        healthy = _FakeRedis()
        monkeypatch.setattr(
            storage_module, "get_shared_redis_client", lambda *a, **k: healthy
        )
        storage_module.reset_pref_version_failure()
        baseline = REQUEST_METRICS.snapshot()["preference_cache"]
        manager.get_preferences()
        latest = REQUEST_METRICS.snapshot()["preference_cache"]
        assert latest["hits"] - baseline["hits"] == 1, "TTL 内应继续命中"
        assert (
            latest["version_invalidations"]
            == baseline["version_invalidations"]
        ), "版本从不可读恢复不是版本变化"
        print("✓ 版本从不可读恢复不误计为版本变化")
    finally:
        shutil.rmtree(storage, ignore_errors=True)


def test_version_bump_uses_atomic_lua_when_available(monkeypatch):
    """支持 EVAL 的客户端用 Lua 原子完成 INCR+EXPIRE（不留无 TTL 的版本键）。"""
    from context import storage as storage_module
    from context.postgres_long_term_memory import PostgresLongTermMemory

    calls = []

    class _EvalRedis(_FakeRedis):
        def eval(self, script, numkeys, *keys_and_args):
            calls.append((script, numkeys, keys_and_args))
            return 1

    redis = _EvalRedis()
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: redis
    )
    storage_module.reset_pref_version_failure()
    store = PostgresLongTermMemory.__new__(PostgresLongTermMemory)
    store.user_id = "u-eval"
    store._bump_shared_preference_version()
    assert len(calls) == 1, calls
    script, numkeys, keys_and_args = calls[0]
    assert numkeys == 1 and keys_and_args[0] == "aligo:prefver:u-eval"
    assert "INCR" in script and "EXPIRE" in script, "INCR 与 EXPIRE 必须同一脚本原子执行"
    assert redis.incr_calls == [], "EVAL 成功时不得再走两命令回退"
    print("✓ 偏好版本推进优先 Lua 原子执行")


def test_postgres_preference_writes_bump_shared_version(monkeypatch):
    """PG 存储层在偏好写入 / 删除 / 清空后推进跨进程版本键。"""
    import contextlib
    import json

    from context import storage as storage_module
    from context.postgres_long_term_memory import PostgresLongTermMemory

    events = []
    redis = _FakeRedis()
    redis.events = events
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: redis
    )
    storage_module.reset_pref_version_failure()
    store = PostgresLongTermMemory.__new__(PostgresLongTermMemory)
    store.user_id = "pref_cache_user"
    store._conn_generation = 0
    monkeypatch.setattr(
        store, "_preference_lock", lambda pref_type: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        store, "_preference_cap_lock", lambda: contextlib.nullcontext()
    )
    monkeypatch.setattr(store, "_enforce_preference_cap", lambda pref_type: None)
    monkeypatch.setattr(
        store, "_abort_if_generation_changed", lambda generation: None
    )
    monkeypatch.setattr(
        store, "_execute", lambda *a, **k: events.append("execute")
    )
    monkeypatch.setattr(
        store, "_json", lambda value: json.dumps(value, ensure_ascii=False)
    )

    store.save_preference("hotel_brands", "汉庭")
    assert redis.incr_calls == ["aligo:prefver:pref_cache_user"]
    # 顺序不变量：写库提交之后才推进版本（提前 INCR 会让别的进程把旧数据
    # 记到新版本号下，快照此后一直命中）
    assert events == ["execute", "incr"], events
    # 版本键带 TTL：不能给每个写过偏好的用户永久留键
    assert redis.expire_calls[-1][0] == "aligo:prefver:pref_cache_user"
    assert redis.expire_calls[-1][1] >= 3600

    class _Cursor:
        rowcount = 1

        def execute(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

    store._conn = _Conn()
    monkeypatch.setattr(store, "_run_with_retry", lambda fn, **kwargs: fn())
    assert store.delete_preference("hotel_brands") is True
    assert len(redis.incr_calls) == 2, "删除应推进版本"

    store.delete_all()
    assert len(redis.incr_calls) == 3, "整表清空应推进版本"

    # 版本推进失败不能影响写入本身，且冷却期内不再触达 Redis
    broken = _BrokenRedis()
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: broken
    )
    storage_module.reset_pref_version_failure()
    store.save_preference("airlines", "东航")
    assert broken.incr_calls == ["aligo:prefver:pref_cache_user"]
    store.save_preference("airlines", "东航")
    assert broken.incr_calls == ["aligo:prefver:pref_cache_user"], (
        "冷却期内不应再触达 Redis"
    )
    print("✓ PG 偏好写入 / 删除 / 清空推进共享版本（提交后 INCR + TTL）")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    import pytest

    for _case in (
        test_json_backend_does_not_cache,
        test_pg_backend_cache_hit_and_invalidation,
        test_inflight_read_does_not_repopulate_after_invalidate,
        test_instance_registry_and_invalidate_all_share_lock,
        test_invalidate_all_preference_caches,
        test_pg_cache_ttl_expiry,
        test_shared_version_invalidates_cross_process_cache,
        test_shared_version_failure_keeps_ttl_cache,
        test_version_protocol_order,
        test_version_invalidation_requires_fresh_snapshot,
        test_version_recovery_from_none_not_counted,
        test_version_bump_uses_atomic_lua_when_available,
        test_postgres_preference_writes_bump_shared_version,
    ):
        _mp = pytest.MonkeyPatch()
        try:
            _case(_mp)
        finally:
            _mp.undo()
    test_orchestration_invalidates_after_preference_write()
    print("\n全部通过")
