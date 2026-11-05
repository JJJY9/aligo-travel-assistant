"""
Redis 互斥锁测试

- 不依赖 Redis：客户端故障时回退进程内锁；
- 需要真实 Redis：两个 DistributedLock 实例共享同一把锁（模拟两个 Web 实例）。

运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_redis_lock.py -q
"""
import os
import sys
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


if __name__ == "__main__":
    test_fallback_when_redis_unavailable()
    print("\n全部通过（Redis 集成用例需 TEST_REDIS_URL）")
