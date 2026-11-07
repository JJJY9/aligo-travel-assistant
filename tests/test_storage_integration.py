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
import threading
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
    memory = RedisShortTermMemory(
        client, session_id, max_turns=2, ttl_sec=60, user_id="pytest-user"
    )
    for index in range(6):
        role = "user" if index % 2 == 0 else "assistant"
        memory.add_message(role, f"m{index}")

    assert memory.get_statistics()["total_messages"] == 4, "滑动窗口应只保留 2 轮"
    contents = [item["content"] for item in memory.get_recent_context(n_turns=2)]
    assert contents == ["m2", "m3", "m4", "m5"]
    assert client.ttl(memory.key) > 0, "应设置 TTL"

    memory.clear()
    assert memory.get_statistics()["total_messages"] == 0

    # Python strip() 与 Lua %s 的空白字符集不同：全角空格结尾的提问也必须
    # 能按「去掉首尾空白后一致」删除，否则长期记忆已删、短期记忆残留
    memory2 = RedisShortTermMemory(
        client,
        f"pytest-{uuid.uuid4().hex[:8]}",
        max_turns=2,
        ttl_sec=60,
        user_id="pytest-user",
    )
    memory2.add_message("user", "报销标准是多少　")
    memory2.add_message("assistant", "回答")
    assert memory2.truncate_last_turn(expect="报销标准是多少") == 2
    assert memory2.get_statistics()["total_messages"] == 0
    print("✓ Redis 短期记忆（滑动窗口 / TTL / 清空 / 全角空格裁剪）")


def _pg_memory_or_skip():
    if not TEST_POSTGRES_DSN:
        pytest.skip("未设置 TEST_POSTGRES_DSN")
    from context.postgres_long_term_memory import PostgresLongTermMemory

    user_id = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        return PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN), user_id
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用: {exc}")


def test_redis_truncate_replay_keeps_newer_messages():
    """故障期截断按 cutoff 重放：发起时刻之后其它实例写入的消息不能误删。"""
    client = _redis_client_or_skip()
    from context import short_term_memory as module

    session_id = f"pytest-{uuid.uuid4().hex[:8]}"
    memory = module.RedisShortTermMemory(
        client, session_id, max_turns=10, ttl_sec=60, user_id="pytest-user"
    )
    memory.add_message("user", "Q1")
    memory.add_message("assistant", "A1")

    real_client = memory.client

    class _Toggle:
        def __init__(self):
            self.fail = True

        def __getattr__(self, name):
            attr = getattr(real_client, name)
            if not callable(attr):
                return attr

            def wrapper(*args, **kwargs):
                if self.fail:
                    raise ConnectionError("redis down")
                return attr(*args, **kwargs)

            return wrapper

    toggle = _Toggle()
    memory.client = toggle  # type: ignore[assignment]
    assert memory.truncate_last_turn(expect="Q1") == 2, "故障期先按影子缓存即时生效"
    kinds = [kind for kind, _ in memory._pending_ops]
    # 带 expect 的截断按「删除具体消息」(remove) 登记，不带 expect 才用 cutoff
    assert kinds and kinds[0] in {"truncate", "remove"}, kinds

    import json as _json
    from datetime import datetime, timedelta

    future = (datetime.now() + timedelta(minutes=1)).isoformat()

    real_client.lpush(
        memory.key,
        _json.dumps(
            {
                "role": "user",
                "content": "C-new",
                "timestamp": future,
                "metadata": {},
            },
            ensure_ascii=False,
        ),
    )

    toggle.fail = False
    module._redis_degraded_until = 0.0
    contents = [item["content"] for item in memory.get_recent_context()]
    assert contents == ["C-new"], contents
    assert memory.get_statistics()["total_messages"] == 1
    print("✓ Redis 截断重放保留 cutoff 之后的新消息")


def test_pg_delete_last_turn_expect_and_archived_cleanup():
    """PG delete_last_turn：expect 不匹配不删；删空会话清理归档标记。"""
    memory, _ = _pg_memory_or_skip()
    try:
        memory.add_chat_messages(
            [
                {"role": "user", "content": "Q1", "session_id": "s-lt"},
                {"role": "assistant", "content": "A1", "session_id": "s-lt"},
            ]
        )
        assert memory.set_session_archived("s-lt", True)
        assert memory.delete_last_turn("s-lt", expect="不匹配") == 0
        assert len(memory.get_chat_history(session_id="s-lt")) == 2
        assert "s-lt" in memory.get_archived_sessions()
        assert memory.delete_last_turn("s-lt", expect="Q1") == 2
        assert memory.get_chat_history(session_id="s-lt") == []
        assert "s-lt" not in memory.get_archived_sessions()
        print("✓ PG delete_last_turn expect 校验与归档清理")
    finally:
        memory.close()


def test_pg_add_chat_turn_writes_pair_in_one_transaction():
    """PG add_chat_turn：用户 + 助手同一事务写入，会话历史完整成对。"""
    memory, _ = _pg_memory_or_skip()
    try:
        memory.add_chat_turn("去北京出差", "已为你规划行程", "s-turn")
        history = memory.get_chat_history(session_id="s-turn")
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert [item["content"] for item in history] == [
            "去北京出差",
            "已为你规划行程",
        ]
        print("✓ PG add_chat_turn 整轮事务写入")
    finally:
        memory.close()


def test_pg_save_trip_histories_counts_mixed_batch():
    """PG 批量保存：同批「已存在 + 新建」的 created/updated 计数正确。"""
    memory, _ = _pg_memory_or_skip()
    try:
        base = {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "出差",
        }
        memory.save_trip_history(dict(base))
        created, updated = memory.save_trip_histories(
            [
                dict(base),
                {**base, "destination": "杭州", "start_date": "2026-12-02"},
            ]
        )
        assert (created, updated) == (1, 1), (created, updated)
        assert memory.get_statistics()["total_trips"] == 2
        print("✓ PG 批量保存 created/updated 计数")
    finally:
        memory.close()


def test_pg_update_trip_concurrent_instances():
    """两个连接并发更新同一条行程的不同字段：advisory lock 下都生效。"""
    memory_a, user_id = _pg_memory_or_skip()
    from context.postgres_long_term_memory import PostgresLongTermMemory

    memory_b = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
    try:
        trip_id = memory_a.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "purpose": "出差",
            }
        )
        errors = []

        def _update(memory, fields):
            try:
                memory.update_trip(trip_id, fields)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_update, args=(memory_a, {"purpose": "会议"})),
            threading.Thread(
                target=_update, args=(memory_b, {"end_date": "2026-12-03"})
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        trip = memory_a.get_trip_history(limit=None)[0]
        assert trip["purpose"] == "会议" and trip["end_date"] == "2026-12-03"
        print("✓ PG 并发更新不同字段都生效")
    finally:
        memory_a.close()
        memory_b.close()


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

        # 原子追加：合并去重，且基于当前库内值
        memory.save_preference("hotel_brands", ["汉庭"])
        assert memory.append_preference("hotel_brands", "如家") == ["汉庭", "如家"]
        assert memory.append_preference("hotel_brands", "如家") == ["汉庭", "如家"]
        assert memory.get_preference("hotel_brands") == ["汉庭", "如家"]

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

        # 再次保存时不带 purpose / session_id：与 JSON 后端一致，保留原值
        memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-11-09",
            }
        )
        preserved = next(
            item
            for item in memory.get_trip_history(limit=None)
            if item["trip_id"] == "trip_1"
        )
        assert preserved["purpose"] == "出差", "未提供 purpose 时不能被默认值覆盖"
        assert preserved["session_id"] == "sess-pg-dup", "未提供 session_id 时不能置空"

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


def test_postgres_trip_id_concurrent():
    """并发保存行程时 trip_id 不重复（advisory lock）。"""
    if not TEST_POSTGRES_DSN:
        pytest.skip("未设置 TEST_POSTGRES_DSN")
    from context.postgres_long_term_memory import PostgresLongTermMemory

    user_id = f"pytest-conc-{uuid.uuid4().hex[:8]}"
    try:
        probe = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001 - 无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可用: {exc}")
    probe.close()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[Exception] = []

    def worker(destination: str) -> None:
        store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        try:
            barrier.wait(timeout=5)
            results.append(
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": "2026-12-05",
                        "end_date": "2026-12-07",
                        "purpose": "出差",
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001 - 线程内错误收集
            errors.append(exc)
        finally:
            store.close()

    threads = [
        threading.Thread(target=worker, args=(destination,))
        for destination in ("上海", "杭州")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    try:
        assert not errors, errors
        assert len(results) == 2
        assert len(set(results)) == 2, f"trip_id 重复：{results}"
        store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        try:
            trips = store.get_trip_history(limit=None)
            assert len(trips) == 2
            assert len({trip["trip_id"] for trip in trips}) == 2
        finally:
            store.close()
    finally:
        cleanup = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        try:
            cleanup.delete_all()
        finally:
            cleanup.close()
    print("✓ PostgreSQL 并发保存行程 trip_id 不重复")


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
    test_postgres_trip_id_concurrent()
    # 本地脚本运行时手动设置环境变量才能跑第三项
    print("\n完成（未配置服务时相关用例跳过）")
