"""
滑动窗口限流器单元测试（无需 API Key）

运行：python tests/test_rate_limit.py
"""
import os
import sys
import threading
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.rate_limit import (
    RateLimiter,
    RedisRateLimiter,
    create_rate_limiter,
)


def test_window_and_keys():
    limiter = RateLimiter(limit=2, window_sec=0.3)
    assert limiter.check("user-a") is None
    assert limiter.check("user-a") is None
    retry_after = limiter.check("user-a")
    assert retry_after is not None and retry_after > 0

    # 不同 key 互不影响
    assert limiter.check("user-b") is None

    # 窗口过期后恢复
    time.sleep(0.35)
    assert limiter.check("user-a") is None
    print("✓ 窗口计数 / 多 key / 窗口恢复")


def test_reset():
    limiter = RateLimiter(limit=1, window_sec=60)
    assert limiter.check("k") is None
    assert limiter.check("k") is not None
    limiter.reset()
    assert limiter.check("k") is None
    print("✓ 重置限流状态")


def test_key_eviction_bounds_memory():
    """大量随机 key（如轮换用户 ID）不应导致内存无限增长。"""
    limiter = RateLimiter(limit=1, window_sec=0.2, max_keys=8)
    for i in range(50):
        limiter.check(f"user-{i}")
    assert len(limiter) <= 8, f"key 数量应被限制在上限内，实际 {len(limiter)}"

    # 窗口过期后再触发一次扫描，过期 key 应被清空
    time.sleep(0.25)
    limiter.check("fresh")
    assert len(limiter) == 1, f"过期 key 应被清理，实际 {len(limiter)}"
    print("✓ key 数量有上限且过期 key 会被清理")


def test_status_reports_remaining():
    limiter = RateLimiter(limit=2, window_sec=60)
    assert limiter.status("k") == (2, 0.0)
    assert limiter.check("k") is None
    remaining, reset = limiter.status("k")
    assert remaining == 1 and 0 < reset <= 60
    limiter.check("k")
    assert limiter.status("k")[0] == 0
    print("✓ status 报告剩余次数 / 重置时间")


def test_peek_does_not_record():
    """peek 只检查是否会被限流，不消耗配额。"""
    limiter = RateLimiter(limit=1, window_sec=60)
    assert limiter.peek("k") is None
    assert limiter.check("k") is None
    assert limiter.peek("k") is not None
    assert limiter.peek("k") is not None
    assert limiter.status("k")[0] == 0, "peek 不应改变剩余次数"
    print("✓ peek 只检查不记录")


def test_thread_safe_checks():
    """线程池并发调用时严格放行 limit 次（内部锁保护 deque）。"""
    limiter = RateLimiter(limit=50, window_sec=60)
    allowed: list[bool] = []
    result_lock = threading.Lock()

    def worker():
        local = [limiter.check("k") is None for _ in range(20)]
        with result_lock:
            allowed.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(allowed) == 50
    assert limiter.status("k")[0] == 0
    print("✓ 并发 check 严格按额度放行（内存桶线程安全）")


class _UnavailableRedis:
    """模拟 Redis 运行中故障：所有命令抛连接错误。"""

    def __init__(self) -> None:
        self.eval_calls = 0

    def eval(self, *args, **kwargs):
        self.eval_calls += 1
        raise ConnectionError("redis down")

    def scan_iter(self, **kwargs):
        raise ConnectionError("redis down")

    def delete(self, *keys):
        raise ConnectionError("redis down")


def test_redis_limiter_falls_back_when_unavailable():
    """Redis 故障时回退进程内限流，并在冷却期内不再尝试 Redis。"""
    client = _UnavailableRedis()
    limiter = RedisRateLimiter(client, "unit-fallback", limit=2, window_sec=60)
    assert limiter.degraded is False
    assert limiter.check("k") is None
    assert limiter.degraded is True
    assert client.eval_calls == 1
    assert limiter.check("k") is None
    assert limiter.check("k") is not None, "回退实现也应执行限流"
    assert client.eval_calls == 1, "冷却期内不应再次调用 Redis"
    assert limiter.peek("k") is not None
    assert limiter.status("k")[0] == 0

    limiter.reset()
    assert limiter.check("k") is None, "reset 后回退计数清零"
    assert client.eval_calls == 2, "reset 后应立即重试 Redis"
    print("✓ Redis 故障回退进程内限流并进入冷却")


def test_create_rate_limiter_backends():
    memory = create_rate_limiter("unit", 1, 60)
    assert isinstance(memory, RateLimiter)
    assert memory.backend == "memory"

    shared = create_rate_limiter("unit", 1, 60, redis_client=object())
    assert isinstance(shared, RedisRateLimiter)
    assert shared.backend == "redis"

    # 只有工厂（配置了 Redis 但启动时不可达）：先回退，恢复后自动接入
    pending = create_rate_limiter("unit", 1, 60, client_factory=lambda: None)
    assert isinstance(pending, RedisRateLimiter)
    assert pending.degraded is True
    print("✓ 限流器工厂按是否提供 Redis 客户端 / 工厂选择后端")


class _RecoverableRedis:
    def __init__(self) -> None:
        self.calls = 0

    def eval(self, *args, **kwargs):
        self.calls += 1
        return "-1"


def test_redis_limiter_reconnects_via_factory():
    """启动时没有客户端：工厂可用后限流器自动重新接入共享计数。"""
    client = _RecoverableRedis()
    limiter = RedisRateLimiter(
        None, "unit-late", limit=2, window_sec=60, client_factory=lambda: client
    )
    assert limiter.degraded is True
    assert limiter.check("k") is None
    assert client.calls == 1
    assert limiter.degraded is False, "成功调用一次后应恢复共享计数"
    print("✓ Redis 恢复后限流器通过工厂重新接入")


def test_degraded_survives_cooldown_without_success():
    """冷却到期但从未成功重试成功时仍应报告降级（监控不能误报健康）。"""
    client = _UnavailableRedis()
    limiter = RedisRateLimiter(client, "unit-persist", limit=2, window_sec=60)
    assert limiter.check("k") is None
    assert limiter.degraded is True
    limiter._degraded_until = 0.0  # noqa: SLF001 - 模拟冷却到期
    assert limiter.degraded is True, "没有一次成功调用前应保持降级"
    print("✓ 冷却到期未恢复时仍报告降级")


class _BytesRedis:
    """模拟未开启 decode_responses 的 Redis 客户端：返回 bytes。"""

    def __init__(self, result) -> None:
        self.result = result

    def eval(self, *args, **kwargs):
        return self.result


def test_redis_limiter_handles_bytes_replies():
    peek = RedisRateLimiter(_BytesRedis(b"1.5"), "unit-bytes", limit=2, window_sec=60)
    assert peek.peek("k") == 1.5

    allowed = RedisRateLimiter(_BytesRedis(b"-1"), "unit-bytes", limit=2, window_sec=60)
    assert allowed.check("k") is None

    status = RedisRateLimiter(
        _BytesRedis([b"1", b"2.5"]), "unit-bytes", limit=2, window_sec=60
    )
    assert status.status("k") == (1, 2.5)
    print("✓ 兼容未解码（bytes）的 Redis 返回值")


if __name__ == "__main__":
    test_window_and_keys()
    test_reset()
    test_key_eviction_bounds_memory()
    test_status_reports_remaining()
    test_peek_does_not_record()
    test_thread_safe_checks()
    test_redis_limiter_falls_back_when_unavailable()
    test_create_rate_limiter_backends()
    test_redis_limiter_reconnects_via_factory()
    test_degraded_survives_cooldown_without_success()
    test_redis_limiter_handles_bytes_replies()
    print("\n全部通过")
