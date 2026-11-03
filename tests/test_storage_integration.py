"""
Redis / PostgreSQL 后端集成测试（需要真实服务）

本地运行（先启动服务）：
    docker compose up -d
    REDIS_URL=redis://localhost:6379/0 \\
    DATABASE_URL=postgresql://aligo:aligo@localhost:5432/aligo \\
    pytest tests/test_storage_integration.py -q

CI 中通过 service 容器 + TEST_REDIS_URL / TEST_POSTGRES_DSN 自动启用；
未提供环境变量时相关用例自动跳过。

运行：python tests/test_storage_integration.py
"""
import os
import sys
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
TEST_POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


def _redis_client_or_skip():
    if not TEST_REDIS_URL:
        pytest.skip("未设置 TEST_REDIS_URL")
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可用: {exc}")
    return client


def test_redis_short_term_memory():
    client = _redis_client_or_skip()
    from context.short_term_memory import RedisShortTermMemory

    session_id = f"pytest-{uuid.uuid4().hex[:8]}"
    memory = RedisShortTermMemory(client, session_id, max_turns=2, ttl_sec=60)
    for index in range(6):
        role = "user" if index % 2 == 0 else "assistant"
        memory.add_message(role, f"m{index}")

    assert memory.get_statistics()["total_messages"] == 4, "滑动窗口应只保留 2 轮"
    contents = [item["content"] for item in memory.get_recent_context(n_turns=2)]
    assert contents == ["m2", "m3", "m4", "m5"]
    assert client.ttl(memory.key) > 0, "应设置 TTL"

    memory.clear()
    assert memory.get_statistics()["total_messages"] == 0
    print("✓ Redis 短期记忆（滑动窗口 / TTL / 清空）")


def test_postgres_long_term_memory():
    if not TEST_POSTGRES_DSN:
        pytest.skip("未设置 TEST_POSTGRES_DSN")
    from context.postgres_long_term_memory import PostgresLongTermMemory

    user_id = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        memory = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用: {exc}")

    try:
        memory.save_preference("hotel_brands", ["汉庭", "如家"])
        memory.save_preference("home_location", "北京")
        assert memory.get_preference() == {"hotel_brands": ["汉庭", "如家"], "home_location": "北京"}
        assert memory.get_preference("home_location") == "北京"

        memory.add_chat_message("user", "你好", "s1")
        memory.add_chat_message("assistant", "您好", "s1")
        memory.add_chat_message("user", "其他会话", "s2")
        assert len(memory.get_chat_history()) == 3
        assert len(memory.get_chat_history(session_id="s1")) == 2
        assert memory.get_chat_history(limit=1)[0]["content"] == "其他会话"

        memory.save_trip_history(
            {"origin": "北京", "destination": "上海", "start_date": "2026-11-09", "purpose": "出差"}
        )
        memory.save_trip_history({"origin": "北京", "destination": "上海", "purpose": "出差"})
        trips = memory.get_trip_history()
        assert len(trips) == 2
        assert trips[0]["trip_id"] == "trip_1"
        assert memory.get_frequent_destinations(1) == [("上海", 2)]

        memory.increment_query_count()
        memory.increment_query_count()
        stats = memory.get_statistics()
        assert stats["total_trips"] == 2
        assert stats["total_messages"] == 3
        assert stats["total_queries"] == 2
        assert stats["frequent_destinations"] == {"上海": 2}

        assert memory.delete_chat_session("s1") == 2
        assert memory.get_chat_history(session_id="s1") == []
        assert len(memory.get_chat_history()) == 1

        memory.clear_history()
        cleared = memory.get_statistics()
        assert cleared["total_trips"] == 0 and cleared["total_messages"] == 0

        memory.delete_all()
        assert memory.get_preference() == {}
        print("✓ PostgreSQL 长期记忆（偏好 / 对话 / 行程 / 统计 / 清理）")
    finally:
        memory.close()


def test_memory_manager_uses_configured_backends(monkeypatch):
    if not TEST_REDIS_URL or not TEST_POSTGRES_DSN:
        pytest.skip("需要同时配置 TEST_REDIS_URL 与 TEST_POSTGRES_DSN")
    from config import STORAGE_CONFIG
    from context.memory_manager import MemoryManager
    from context.postgres_long_term_memory import PostgresLongTermMemory
    from context.short_term_memory import RedisShortTermMemory

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", TEST_REDIS_URL)
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", TEST_POSTGRES_DSN)
    monkeypatch.setitem(STORAGE_CONFIG, "redis_ttl_sec", 60)

    user_id = f"pytest-mm-{uuid.uuid4().hex[:8]}"
    session_id = f"pytest-sess-{uuid.uuid4().hex[:8]}"
    manager = MemoryManager(user_id, session_id)
    assert isinstance(manager.short_term, RedisShortTermMemory)
    assert isinstance(manager.long_term, PostgresLongTermMemory)

    manager.add_message("user", "hello")
    assert manager.short_term.get_statistics()["total_messages"] == 1
    assert len(manager.long_term.get_chat_history()) == 1
    assert manager.long_term.get_chat_history()[0]["content"] == "hello"
    manager.short_term.clear()
    manager.long_term.delete_all()
    print("✓ MemoryManager 按配置启用 Redis + PostgreSQL")


if __name__ == "__main__":
    test_redis_short_term_memory()
    test_postgres_long_term_memory()
    # 本地脚本运行时手动设置环境变量才能跑第三项
    print("\n完成（未配置服务时相关用例跳过）")
