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

        assert memory.delete_preference("hotel_brands") is True
        assert memory.get_preference("hotel_brands") is None
        memory.save_preference("hotel_brands", ["汉庭", "如家"])

        memory.add_chat_message("user", "你好", "s1")
        memory.add_chat_message("assistant", "您好", "s1")
        memory.add_chat_message("user", "其他会话", "s2")
        assert len(memory.get_chat_history()) == 3
        assert len(memory.get_chat_history(session_id="s1")) == 2
        assert memory.get_chat_history(limit=1)[0]["content"] == "其他会话"

        memory.set_session_title("s1", "杭州出差")
        assert memory.get_session_titles() == {"s1": "杭州出差"}

        memory.set_session_pinned("s1", True)
        assert memory.get_pinned_sessions() == ["s1"]

        memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-11-09",
                "purpose": "出差",
                "session_id": "sess-pg",
            }
        )
        memory.save_trip_history({"origin": "北京", "destination": "上海", "purpose": "出差"})
        trips = memory.get_trip_history()
        assert len(trips) == 2
        assert trips[0]["trip_id"] == "trip_1"
        assert trips[0]["session_id"] == "sess-pg"
        assert trips[1]["session_id"] is None
        assert memory.get_frequent_destinations(1) == [("上海", 2)]

        # 同路线 + 同起止日期的重复保存：更新会话关联，不新增记录
        dedup_id = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-11-09",
                "purpose": "出差",
                "session_id": "sess-pg-dup",
            }
        )
        assert dedup_id == "trip_1", "去重保存返回既有 trip_id"
        deduped = memory.get_trip_history(limit=None)
        assert len(deduped) == 2, "同一行程不应重复保存"
        assert deduped[0]["session_id"] == "sess-pg-dup"

        # 更新行程字段；改成与另一条重复时拒绝
        assert memory.update_trip("trip_2", {"purpose": "会议"}) is True
        trip_2 = next(
            item for item in memory.get_trip_history(limit=None) if item["trip_id"] == "trip_2"
        )
        assert trip_2["purpose"] == "会议"
        with pytest.raises(ValueError):
            memory.update_trip("trip_2", {"start_date": "2026-11-09"})
        assert memory.update_trip("trip_99", {"purpose": "x"}) is False

        memory.increment_query_count()
        memory.increment_query_count()
        stats = memory.get_statistics()
        assert stats["total_trips"] == 2
        assert stats["total_messages"] == 3
        assert stats["total_queries"] == 2
        assert stats["frequent_destinations"] == {"上海": 2}

        assert memory.delete_trip("trip_1") is True
        assert memory.delete_trip("trip_1") is False
        assert memory.get_frequent_destinations(1) == [("上海", 1)]
        memory.save_trip_history({"origin": "上海", "destination": "杭州", "purpose": "出差"})
        newest = memory.get_trip_history(1)[0]
        assert newest["trip_id"] == "trip_3", "删除后新增按现有最大 id（trip_2）递增"
        assert memory.get_statistics()["total_trips"] == 2

        assert memory.delete_chat_session("s1") == 2
        assert memory.get_chat_history(session_id="s1") == []
        assert memory.get_session_titles() == {}
        assert memory.get_pinned_sessions() == []
        assert len(memory.get_chat_history()) == 1

        memory.set_session_title("s2", "残留标题")
        memory.set_session_pinned("s2", True)
        memory.clear_history()
        cleared = memory.get_statistics()
        assert cleared["total_trips"] == 0 and cleared["total_messages"] == 0
        assert memory.get_session_titles() == {}
        assert memory.get_pinned_sessions() == []

        memory.set_session_title("s3", "标题")
        memory.set_session_pinned("s3", True)
        memory.delete_all()
        assert memory.get_preference() == {}
        assert memory.get_session_titles() == {}
        assert memory.get_pinned_sessions() == []
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

    # PostgreSQL 无内存快照：多轮对话前不应重建连接
    live_store = manager.long_term
    manager.reload_long_term()
    assert manager.long_term is live_store

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
