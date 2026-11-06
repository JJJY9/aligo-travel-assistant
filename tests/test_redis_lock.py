"""
Redis 互斥锁测试

- 不依赖 Redis：客户端故障时回退进程内锁；
- 需要真实 Redis：两个 DistributedLock 实例共享同一把锁（模拟两个 Web 实例）。

运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_redis_lock.py -q
"""
import os
import sys
import threading
import time
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.redis_lock import DistributedLock

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")


class _BrokenRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")


class _FlakyRedis:
    """可切换故障的假客户端：模拟 SET NX / DEL / EXPIRE。"""

    def __init__(self):
        self.fail = False
        self.store: dict[str, str] = {}
        self.eval_scripts: list[str] = []

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis blip")
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, *args):
        self.eval_scripts.append(script)
        if self.fail:
            raise ConnectionError("redis blip")
        if self.store.get(key) != args[0]:
            return 0
        if "DEL" in script:
            del self.store[key]
            return 1
        if "EXPIRE" in script:
            return 1
        return 0


class _ApplyThenTimeout:
    """SET 已在服务端生效但客户端等到超时。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        self.store[key] = value
        raise TimeoutError("set response timeout")

    def eval(self, script, numkeys, key, *args):
        if self.store.get(key) == args[0]:
            del self.store[key]
            return 1
        return 0


def test_fallback_when_redis_unavailable():
    lock = DistributedLock(_BrokenRedis(), "aligo:test:fallback", ttl_sec=30)
    token = lock.acquire()
    assert token
    assert lock.acquire() is None, "回退锁也应互斥"
    lock.release(token)
    again = lock.acquire()
    assert again
    lock.release(again)
    print("✓ Redis 故障回退进程内互斥锁")


def test_local_mutex_blocks_even_if_redis_errors():
    client = _FlakyRedis()
    lock = DistributedLock(client, "aligo:test:mixed", ttl_sec=30)
    first = lock.acquire()
    assert first and first != "__local__"

    client.fail = True
    assert lock.acquire() is None, "同进程已持有锁时必须拒绝，即使 Redis 抖动"

    client.fail = False
    lock.release(first)
    print("✓ Redis 抖动时同进程仍互斥")


def test_redis_path_recovers_after_transient_failure():
    client = _FlakyRedis()
    lock = DistributedLock(client, "aligo:test:recover", ttl_sec=30)

    client.fail = True
    token = lock.acquire()
    assert token and token != "__local__"
    assert lock.degraded is True
    lock.release(token)

    client.fail = False
    token2 = lock.acquire()
    assert token2 and token2 != "__local__"
    assert lock.degraded is False, "Redis 恢复后应重新走共享锁"
    lock.release(token2)
    assert "aligo:test:recover" not in client.store
    print("✓ 瞬时故障后恢复 Redis 共享锁")


def test_release_cleans_key_after_set_timeout():
    client = _ApplyThenTimeout()
    lock = DistributedLock(client, "aligo:test:ambiguous", ttl_sec=30)
    token = lock.acquire()
    assert token and token != "__local__"
    assert "aligo:test:ambiguous" in client.store, "SET 可能已在服务端生效"

    lock.release(token)
    assert "aligo:test:ambiguous" not in client.store, "释放应尽力清理已生效的锁键"
    print("✓ SET 超时后释放仍清理锁键")


def test_heartbeat_renews_lock_ttl():
    client = _FlakyRedis()
    lock = DistributedLock(client, "aligo:test:renew", ttl_sec=1)
    token = lock.acquire()
    assert token

    time.sleep(1.5)
    assert any("EXPIRE" in script for script in client.eval_scripts), "持有期间应续期"

    lock.release(token)
    assert "aligo:test:renew" not in client.store
    print("✓ 长任务持有期间自动续期")


@pytest.mark.skipif(not TEST_REDIS_URL, reason="未设置 TEST_REDIS_URL")
def test_cross_instance_lock():
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.ping()
    key = f"aligo:test:eval-lock:{uuid.uuid4().hex[:8]}"
    try:
        first = DistributedLock(client, key, ttl_sec=30)
        second = DistributedLock(client, key, ttl_sec=30)

        token = first.acquire()
        assert token
        assert second.acquire() is None, "另一个实例应看到同一个锁"

        first.release(token)
        other = second.acquire()
        assert other, "释放后另一个实例应能加锁"
        second.release(other)
        print("✓ 两个实例通过 Redis 共享互斥锁")
    finally:
        client.delete(key)


class _LostLockRedis:
    """续期时锁已过期并被其它实例拿走（EXPIRE 返回 0）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, *args):
        if "DEL" in script:
            self.store.pop(key, None)
            return 1
        if "EXPIRE" in script:
            return 0
        return 0


def test_heartbeat_detects_lost_lock():
    """续期发现锁已被别人拿走时必须置 lost，不能继续假定自己独占。"""
    client = _LostLockRedis()
    lock = DistributedLock(client, "aligo:test:lost", ttl_sec=1)
    token = lock.acquire()
    assert token
    assert lock.lost is False

    time.sleep(1.5)
    assert lock.lost is True, "续期失败（锁已失效）应标记 lost"
    lock.release(token)
    print("✓ 续期失败时标记锁已丢失")


class _StalledRenewRedis:
    """第一次续期被卡住（模拟释放时仍在途的旧续期），之后续期正常。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.renew_calls = 0
        self.renew_entered = threading.Event()
        self.finish_renew = threading.Event()

    def set(self, key, value, nx=False, ex=None):
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, *args):
        if "EXPIRE" in script:
            self.renew_calls += 1
            if self.renew_calls == 1:
                self.renew_entered.set()
                self.finish_renew.wait(timeout=10)
                return 0
            return 1
        if "DEL" in script:
            self.store.pop(key, None)
            return 1
        return 0


def test_stale_renewal_does_not_mark_new_hold_lost():
    """释放时卡住的旧续期返回 0，不能把重新加锁后的新持有点标记为 lost。"""
    client = _StalledRenewRedis()
    lock = DistributedLock(client, "aligo:test:stale-renew", ttl_sec=1)
    first = lock.acquire()
    assert first
    assert client.renew_entered.wait(timeout=5), "续期线程应启动"

    lock.release(first)  # join 超时后返回，本地锁释放
    second = lock.acquire()
    assert second and second != first
    assert lock.lost is False

    client.finish_renew.set()  # 旧续期此刻才返回 0
    time.sleep(0.3)
    assert lock.lost is False, "旧续期不能污染新的持有"
    lock.release(second)
    print("✓ 旧续期返回 0 不污染新的持有点")


class _OutageRenewRedis:
    """SET 成功后续期一直抛异常（Redis 断连，无法用「返回 0」发现易主）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, *args):
        if "EXPIRE" in script:
            raise ConnectionError("redis down")
        if "DEL" in script:
            self.store.pop(key, None)
            return 1
        return 0


def test_heartbeat_marks_lost_after_renew_outage():
    """续期连续失败超过 TTL：键必然已过期，保守标记互斥丢失。"""
    client = _OutageRenewRedis()
    lock = DistributedLock(client, "aligo:test:outage", ttl_sec=1)
    token = lock.acquire()
    assert token
    assert lock.lost is False

    time.sleep(1.6)
    assert lock.lost is True, "连续续期失败超过 TTL 应标记 lost"
    lock.release(token)
    print("✓ 续期持续失败超过 TTL 时标记锁已丢失")


class _TransientOutageRedis:
    """第一次续期抛异常（瞬时抖动），之后恢复正常。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.renew_calls = 0

    def set(self, key, value, nx=False, ex=None):
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, *args):
        if "EXPIRE" in script:
            self.renew_calls += 1
            if self.renew_calls == 1:
                raise ConnectionError("redis blip")
            return 1
        if "DEL" in script:
            self.store.pop(key, None)
            return 1
        return 0


def test_transient_renew_failure_keeps_lock():
    """一次续期抖动（未超过 TTL）不标记丢失，恢复后续期继续生效。"""
    client = _TransientOutageRedis()
    lock = DistributedLock(client, "aligo:test:blip", ttl_sec=3)
    token = lock.acquire()
    assert token

    time.sleep(2.4)  # t≈1.0 第一次续期抖动，t≈2.0 恢复成功
    assert client.renew_calls >= 2
    assert lock.lost is False, "TTL 内的续期抖动不能标记丢失"
    lock.release(token)
    print("✓ 瞬时续期抖动不标记丢失")


if __name__ == "__main__":
    test_fallback_when_redis_unavailable()
    test_local_mutex_blocks_even_if_redis_errors()
    test_redis_path_recovers_after_transient_failure()
    test_release_cleans_key_after_set_timeout()
    test_heartbeat_renews_lock_ttl()
    test_heartbeat_detects_lost_lock()
    test_heartbeat_marks_lost_after_renew_outage()
    test_transient_renew_failure_keeps_lock()
    test_stale_renewal_does_not_mark_new_hold_lost()
    print("\n全部通过（Redis 集成用例需 TEST_REDIS_URL）")
