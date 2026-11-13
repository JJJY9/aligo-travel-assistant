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

    # 窗口过期后再触发一次扫描，过期 key 应被清空（清理限频 0.5 秒一次）
    time.sleep(0.6)
    limiter.check("fresh")
    assert len(limiter) == 1, f"过期 key 应被清理，实际 {len(limiter)}"
    print("✓ key 数量有上限且过期 key 会被清理")


def test_capacity_is_fail_closed_without_resetting_victims():
    """容量满时拒绝新 key，不能淘汰活跃桶把受害者的限流计数清零。"""
    limiter = RateLimiter(limit=1, window_sec=60, max_keys=2)
    assert limiter.check("victim-a") is None
    assert limiter.check("victim-a") is not None, "a 已被限流"
    assert limiter.check("victim-b") is None
    assert limiter.check("attacker-new") is not None, "满员时新 key 直接拒绝"
    assert len(limiter) == 2
    # 受害者的限流状态必须保留（旧实现淘汰最旧事件会把 a 的计数清零）
    assert limiter.check("victim-a") is not None
    print("✓ 容量满时 fail-closed，不重置受害桶")


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
    """线程池并发调用时严格放行 limit 次，且不同键之间不串额度。

    barrier 让所有线程同时进入 check，最大化读写交错窗口；额外用第二个键
    验证「一个键打满不影响另一个键」。GIL 下 deque 的追加本身是原子的，
    这里的核心目标是把「读写交错导致误计 / 串桶」这类逻辑错误暴露出来。
    """
    limiter = RateLimiter(limit=50, window_sec=60)
    allowed: list[bool] = []
    result_lock = threading.Lock()
    barrier = threading.Barrier(10, timeout=10)

    def worker():
        barrier.wait()
        local = [limiter.check("k") is None for _ in range(20)]
        with result_lock:
            allowed.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(allowed) == 50, f"应恰好放行 50 次，实际 {sum(allowed)}"
    assert limiter.status("k")[0] == 0
    # 不同键的桶互不影响：另一个键仍然满额
    assert limiter.check("other") is None
    assert limiter.status("other")[0] == 49
    print("✓ 并发 check 严格按额度放行（栅栏并发 + 桶隔离）")


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


def test_redis_limiter_recovers_after_cooldown(monkeypatch):
    """运行中故障 → 冷却期内走回退 → 冷却到期自动重试 Redis 并恢复。"""
    import utils.rate_limit as rate_limit_module

    times = {"now": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: times["now"])

    class _Recoverable(_UnavailableRedis):
        def __init__(self):
            super().__init__()
            self.fail = True

        def eval(self, *args, **kwargs):
            self.eval_calls += 1
            if self.fail:
                raise ConnectionError("redis down")
            return -1  # 放行（Retry-After 为负 → None）

    client = _Recoverable()
    limiter = RedisRateLimiter(client, "unit-recover", limit=5, window_sec=60)
    assert limiter.check("k") is None
    assert limiter.degraded is True and client.eval_calls == 1

    times["now"] += 59  # 冷却期内：不再碰 Redis
    assert limiter.check("k") is None
    assert client.eval_calls == 1

    client.fail = False
    times["now"] += 2  # 冷却到期：自动重试成功并恢复共享计数
    assert limiter.check("k") is None
    assert client.eval_calls == 2
    assert limiter.degraded is False
    print("✓ Redis 限流冷却到期后自动恢复")


def test_probe_recovers_idle_bucket_after_cooldown(monkeypatch):
    """低频桶在冷却到期后由健康探测恢复（不需要该桶再收到请求）。"""
    import utils.rate_limit as rate_limit_module

    times = {"now": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: times["now"])

    class _Pingable(_UnavailableRedis):
        def __init__(self):
            super().__init__()
            self.up = False

        def eval(self, *args, **kwargs):
            self.eval_calls += 1
            if not self.up:
                raise ConnectionError("redis down")
            return -1

    client = _Pingable()
    limiter = RedisRateLimiter(client, "unit-probe", limit=2, window_sec=60)
    assert limiter.check("k") is None
    assert limiter.degraded is True
    fail_calls = client.eval_calls

    # 冷却期内不探测（沿用上次失败的结论）
    assert limiter.probe() is False
    assert client.eval_calls == fail_calls

    times["now"] += 61
    assert limiter.probe() is False, "探测失败继续报降级"
    assert client.eval_calls == fail_calls + 1, "冷却到期后应真实调用一次 EVAL"
    assert limiter.degraded is True

    client.up = True
    times["now"] += 61
    assert limiter.probe() is True
    assert limiter.degraded is False
    assert limiter.check("k") is None, "恢复后重新走 Redis 共享计数"
    print("✓ 冷却到期后健康探测恢复低频限流桶")


def test_probe_is_throttled_per_second(monkeypatch):
    """健康探针同秒内不重复访问 Redis（未限流的健康接口不能被洪泛放大）。"""
    import utils.rate_limit as rate_limit_module

    times = {"now": 1000.0}
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: times["now"])

    class _Client(_UnavailableRedis):
        def __init__(self):
            super().__init__()
            self.up = False

        def eval(self, *args, **kwargs):
            self.eval_calls += 1
            if not self.up:
                raise ConnectionError("redis down")
            return -1

    client = _Client()
    limiter = RedisRateLimiter(
        client, "unit-probe-throttle", limit=2, window_sec=60
    )
    limiter.check("k")  # 进入降级
    times["now"] += 61
    assert limiter.probe() is False
    calls = client.eval_calls
    assert limiter.probe() is False
    assert client.eval_calls == calls, "1 秒内重复探测不应再访问 Redis"
    times["now"] += 61
    client.up = True
    assert limiter.probe() is True
    print("✓ 健康探针按秒节流")


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


def test_peek_and_status_expired_events():
    """peek / status 对未知 key 与过期事件的边界分支。"""
    limiter = RateLimiter(limit=1, window_sec=0.25)
    assert limiter.peek("unknown") is None
    assert limiter.status("unknown") == (1, 0.0)

    assert limiter.check("k1") is None
    assert limiter.check("k2") is None
    assert limiter.peek("k1") is not None, "已达上限应返回 Retry-After"

    time.sleep(0.3)
    # 两条路径分别在独立的 key 上覆盖「过期事件清理」：
    # peek 的 while-pop 清理，以及 status 的 while-pop + 全空分支
    assert limiter.peek("k1") is None, "事件过期后应清理并放行"
    assert limiter.status("k2") == (1, 0.0)
    print("✓ peek / status 的未知 key 与过期清理分支")


def test_redis_factory_failure_degrades_then_reset_without_client():
    """工厂抛异常 / 返回 None：限流器回退但不崩溃；reset 在无客户端时为空操作。"""

    def broken_factory():
        raise RuntimeError("factory boom")

    broken = RedisRateLimiter(
        None, "unit-broken", limit=2, window_sec=60, client_factory=broken_factory
    )
    assert broken.check("k") is None, "前端回退实现应放行"
    assert broken.degraded is True
    broken.reset()

    no_client = RedisRateLimiter(
        None, "unit-none", limit=2, window_sec=60, client_factory=lambda: None
    )
    no_client.reset()
    assert no_client.degraded is True
    print("✓ Redis 工厂失败回退与无客户端 reset")


def test_redis_cooldown_short_circuit_and_scan_failure():
    """冷却期内的请求不再打 Redis；reset 的扫描/删除失败被吞掉。"""

    class _CountingDownRedis:
        def __init__(self):
            self.eval_calls = 0

        def eval(self, *args, **kwargs):
            self.eval_calls += 1
            raise RuntimeError("down")

        def scan_iter(self, *args, **kwargs):
            raise RuntimeError("scan down")

    client = _CountingDownRedis()
    limiter = RedisRateLimiter(client, "unit-cooldown", limit=2, window_sec=60)
    assert limiter.check("k") is None
    assert client.eval_calls == 1
    assert limiter.check("k") is None
    assert client.eval_calls == 1, "冷却期内不应再次访问 Redis"
    assert limiter.degraded is True

    # scan_iter 抛异常：reset 不传播（清理失败不影响服务）
    limiter.reset()

    # 正常路径：扫描到键后逐个删除
    class _ScannableRedis:
        def __init__(self):
            self.deleted = []

        def eval(self, *args, **kwargs):
            return 0

        def scan_iter(self, *args, **kwargs):
            return iter([b"aligo:rl:unit:unit-scan:k1", b"aligo:rl:unit:unit-scan:k2"])

        def delete(self, *keys):
            self.deleted.extend(keys)

    scannable = _ScannableRedis()
    RedisRateLimiter(scannable, "unit-scan", limit=2, window_sec=60).reset()
    assert len(scannable.deleted) == 2, "reset 应删除扫描到的键"
    print("✓ 冷却短路、reset 清理失败容错与正常删除")


def test_rate_window_non_positive_is_clamped():
    """CHAT_RATE_WINDOW_SEC<=0 会形同不限流：导入时按下限 1 秒处理。"""
    import subprocess

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.RATE_LIMIT_CONFIG['window_sec'])",
        ],
        cwd=project_root,
        env=dict(os.environ, CHAT_RATE_WINDOW_SEC="0"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "1", completed.stdout
    print("✓ 非法限流窗口按下限 1 秒钳制")


if __name__ == "__main__":
    test_window_and_keys()
    test_reset()
    test_key_eviction_bounds_memory()
    test_capacity_is_fail_closed_without_resetting_victims()
    test_status_reports_remaining()
    test_peek_does_not_record()
    test_thread_safe_checks()
    test_redis_limiter_falls_back_when_unavailable()
    test_create_rate_limiter_backends()
    test_redis_limiter_reconnects_via_factory()
    test_degraded_survives_cooldown_without_success()
    test_redis_limiter_handles_bytes_replies()
    test_peek_and_status_expired_events()
    test_redis_factory_failure_degrades_then_reset_without_client()
    test_redis_cooldown_short_circuit_and_scan_failure()
    test_rate_window_non_positive_is_clamped()
    print("\n全部通过")
