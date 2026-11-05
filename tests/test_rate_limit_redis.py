"""
Redis 共享限流集成测试（需要真实 Redis）

CI 中通过 `TEST_REDIS_URL` 自动启用；未提供时跳过。验证两件事：
1. 两个 ``RedisRateLimiter`` 实例（模拟两个 Web 实例）共享同一份额度；
2. Web 服务在配置 REDIS_URL 后自动使用 Redis 限流，健康接口报告 backend。

运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_rate_limit_redis.py -q
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from fastapi.testclient import TestClient

from config import RATE_LIMIT_CONFIG
from config_agentscope import UsageTrackingModel
from context import storage
from utils.rate_limit import REDIS_KEY_PREFIX, RedisRateLimiter
from webapp.server import MEMORY_DIR, create_app

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(not TEST_REDIS_URL, reason="未设置 TEST_REDIS_URL")

_INTENT = {
    "reasoning": "测试",
    "intents": ["greeting"],
    "key_entities": {},
    "rewritten_query": "你好",
    "agent_schedule": [],
}


class _FakeModel:
    async def __call__(self, messages, **kwargs):
        return {
            "content": [
                {"type": "text", "text": json.dumps(_INTENT, ensure_ascii=False)}
            ],
            "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.05},
        }


def _client():
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.ping()
    return client


def _cleanup(name: str, client=None) -> None:
    client = client or _client()
    keys = list(
        client.scan_iter(match=f"{REDIS_KEY_PREFIX}:{name}:*", count=500)
    )
    if keys:
        client.delete(*keys)


def test_shared_window_across_instances():
    """同一 Redis 上的两个限流器实例共享计数（多实例部署的关键）。"""
    name = f"it-{uuid.uuid4().hex[:8]}"
    client = _client()
    try:
        first = RedisRateLimiter(client, name, limit=2, window_sec=60)
        second = RedisRateLimiter(client, name, limit=2, window_sec=60)

        assert first.check("k") is None
        assert second.check("k") is None
        blocked = first.check("k")
        assert blocked is not None and 0 < blocked <= 60
        assert second.peek("k") is not None, "另一个实例应看到已用满的额度"
        remaining, reset = second.status("k")
        assert remaining == 0 and 0 <= reset <= 60
        print("✓ 两个实例共享滑动窗口计数")
    finally:
        _cleanup(name, client)


def test_concurrent_checks_share_atomic_quota():
    """多线程同时 check（模拟多实例并发）时，Lua 脚本应严格只放行 limit 次。"""
    name = f"it-conc-{uuid.uuid4().hex[:8]}"
    client = _client()
    limit = 5
    try:
        limiters = [
            RedisRateLimiter(client, name, limit=limit, window_sec=60) for _ in range(2)
        ]
        allowed: list[bool] = []
        result_lock = threading.Lock()

        def worker(limiter):
            local = [limiter.check("k") is None for _ in range(10)]
            with result_lock:
                allowed.extend(local)

        threads = [
            threading.Thread(target=worker, args=(limiters[index % len(limiters)],))
            for index in range(10)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(allowed) == limit, f"应恰好放行 {limit} 次，实际 {sum(allowed)}"
        assert limiters[0].status("k")[0] == 0
        print("✓ 并发 check 严格按额度放行（Lua 原子）")
    finally:
        _cleanup(name, client)


def test_peek_does_not_record_and_window_expires():
    name = f"it-{uuid.uuid4().hex[:8]}"
    client = _client()
    try:
        limiter = RedisRateLimiter(client, name, limit=1, window_sec=0.6)
        assert limiter.peek("k") is None
        assert limiter.check("k") is None
        assert limiter.status("k")[0] == 0
        assert limiter.peek("k") is not None
        assert limiter.check("k") is not None, "超限的 check 不应再记录事件"
        time.sleep(0.8)
        assert limiter.peek("k") is None, "窗口过期后应恢复额度"
        assert limiter.check("k") is None
        print("✓ peek 不消耗额度，窗口按 Redis 时间滑动")
    finally:
        _cleanup(name, client)


def test_bucket_names_isolated_and_reset():
    client = _client()
    name_a = f"it-a-{uuid.uuid4().hex[:8]}"
    name_b = f"it-b-{uuid.uuid4().hex[:8]}"
    try:
        bucket_a = RedisRateLimiter(client, name_a, limit=1, window_sec=60)
        bucket_b = RedisRateLimiter(client, name_b, limit=1, window_sec=60)
        assert bucket_a.check("k") is None
        assert bucket_b.check("k") is None, "不同桶（用户 / IP）互不影响"
        assert bucket_a.check("k") is not None

        bucket_a.reset()
        assert bucket_a.check("k") is None, "reset 应清空该桶的 Redis 计数"
        assert bucket_b.check("k") is not None, "reset 不应影响其它桶"
        print("✓ 桶名隔离，reset 只清理本桶")
    finally:
        _cleanup(name_a, client)
        _cleanup(name_b, client)


def test_webapp_uses_redis_rate_limit_across_instances(monkeypatch):
    """两个 Web 实例共享用户额度：实例 A 用掉的次数实例 B 能看到。"""
    client = _client()
    _cleanup("user", client)
    _cleanup("ip", client)
    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", TEST_REDIS_URL)
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "chat_requests", 1)
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "ip_requests", 100)
    storage.reset_failure_cache()

    user_id = f"webredis-limit-{uuid.uuid4().hex[:8]}"
    memory_file = Path(MEMORY_DIR) / f"{user_id}.json"
    try:
        app_a = create_app(model=UsageTrackingModel(_FakeModel()))
        app_b = create_app(model=UsageTrackingModel(_FakeModel()))
        assert app_a.state.aligo.rate_limiter.backend == "redis"
        assert app_a.state.aligo.ip_limiter.backend == "redis"

        first = TestClient(app_a).post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "shared-1"},
        )
        assert first.status_code == 200

        limited = TestClient(app_b).post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "shared-2"},
        )
        assert limited.status_code == 429, "另一实例应共享已用额度"
        assert limited.headers["x-ratelimit-remaining"] == "0"
        assert int(limited.headers["retry-after"]) >= 1

        health = TestClient(app_b).get("/api/health").json()
        assert health["rate_limit"] == "redis"
        assert health["rate_limit_configured"] is True
        assert health["rate_limit_degraded"] is False
        print("✓ Web 两实例共享 Redis 限流，/api/health 报告 redis")
    finally:
        _cleanup("user", client)
        _cleanup("ip", client)
        storage.reset_failure_cache()
        if memory_file.exists():
            memory_file.unlink()


if __name__ == "__main__":
    test_shared_window_across_instances()
    test_concurrent_checks_share_atomic_quota()
    test_peek_does_not_record_and_window_expires()
    test_bucket_names_isolated_and_reset()
    print("\n全部通过（Web 集成用例需 pytest 的 monkeypatch，未在 __main__ 中运行）")
