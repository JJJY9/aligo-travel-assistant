"""
Redis 共享会话删除墓碑集成测试（需要真实 Redis）

CI 中通过 `TEST_REDIS_URL` 自动启用；未提供时跳过。
验证实例 A 删除会话后，实例 B 能通过 Redis 墓碑拒绝旧 session_id 的新请求。

运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/test_tombstone_redis.py -q
"""
import json
import os
import sys
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from config_agentscope import UsageTrackingModel
from context import storage
from webapp.server import create_app

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(not TEST_REDIS_URL, reason="未设置 TEST_REDIS_URL")


class _FakeModel:
    async def __call__(self, messages, **kwargs):
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "reasoning": "测试",
                            "intents": ["greeting"],
                            "key_entities": {},
                            "rewritten_query": "你好",
                            "agent_schedule": [],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.05},
        }


def test_shared_tombstone_with_redis(monkeypatch):
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    client.ping()
    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", TEST_REDIS_URL)
    storage.reset_failure_cache()

    user = f"tomb-{uuid.uuid4().hex[:8]}"
    session = f"s-{uuid.uuid4().hex[:8]}"
    key = f"aligo:deleted:{user}:{session}"
    try:
        app_a = create_app(model=UsageTrackingModel(_FakeModel()))
        app_b = create_app(model=UsageTrackingModel(_FakeModel()))
        state_a = app_a.state.aligo
        state_b = app_b.state.aligo
        assert state_a._redis_client is not None

        state_a.mark_deleted(user, session)
        assert client.exists(key) == 1
        ttl = client.ttl(key)
        assert 0 < ttl <= int(state_a.tombstone_ttl_sec), "共享墓碑 TTL 应与本地一致"
        assert state_b.is_recently_deleted(user, session) is True
        print("✓ 删除墓碑通过 Redis 跨实例共享（含 TTL）")
    finally:
        client.delete(key)
        storage.reset_failure_cache()
