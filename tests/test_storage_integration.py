"""
Redis / PostgreSQL 后端集成测试（需要真实服务）

本地运行（先启动服务）：
    docker compose up -d
    TEST_REDIS_URL=redis://localhost:6379/0 \\
    TEST_POSTGRES_DSN=postgresql://aligo:aligo@localhost:5432/aligo \\
    pytest tests/test_storage_integration.py -q

CI 中通过 service 容器 + TEST_REDIS_URL / TEST_POSTGRES_DSN 自动启用；
未提供环境变量时相关用例自动跳过。测试只认 TEST_* 变量：不会误连
开发 / 生产环境里配置的 REDIS_URL / DATABASE_URL（用例会清空用户数据）。

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
    """PG delete_last_turn：expect 不匹配不删；删空会话清理标题 / 置顶 / 归档。"""
    memory, _ = _pg_memory_or_skip()
    try:
        memory.add_chat_messages(
            [
                {"role": "user", "content": "Q1", "session_id": "s-lt"},
                {"role": "assistant", "content": "A1", "session_id": "s-lt"},
            ]
        )
        assert memory.set_session_archived("s-lt", True)
        assert memory.set_session_title("s-lt", "旧标题")
        assert memory.set_session_pinned("s-lt", True)
        assert memory.delete_last_turn("s-lt", expect="不匹配") == 0
        assert len(memory.get_chat_history(session_id="s-lt")) == 2
        assert "s-lt" in memory.get_archived_sessions()
        assert memory.delete_last_turn("s-lt", expect="Q1") == 2
        assert memory.get_chat_history(session_id="s-lt") == []
        assert "s-lt" not in memory.get_archived_sessions()
        # 删空会话必须同步清理标题 / 置顶，否则同 ID 重启的新会话沿用旧元数据
        assert "s-lt" not in memory.get_session_titles()
        assert "s-lt" not in memory.get_pinned_sessions()
        print("✓ PG delete_last_turn expect 校验与标题 / 置顶 / 归档清理")
    finally:
        memory.close()


def test_pg_batch_save_matches_sequential():
    """PG 批量保存与逐条保存的最终结果一致（去重 / 未补零日期 / 缺省目的）。"""
    batch, _ = _pg_memory_or_skip()
    from context.postgres_long_term_memory import PostgresLongTermMemory

    sequential_user = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        sequential = PostgresLongTermMemory(sequential_user, TEST_POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001 - 无 PG 时跳过
        batch.close()
        pytest.skip(f"PostgreSQL 不可用: {exc}")
    rows = [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-1",
            "end_date": "2026-12-3",
            "purpose": "客户会议",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-10",
            "purpose": "培训",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-10",
            "end_date": "2026-12-10",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
            "purpose": "会议",
            "session_id": "s-1",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
    ]
    try:
        batch_counts = batch.save_trip_histories([dict(row) for row in rows])
        sequential_counts = [0, 0]
        for row in rows:
            before = len(sequential.get_trip_history(limit=None))
            sequential.save_trip_history(dict(row))
            created = len(sequential.get_trip_history(limit=None)) > before
            sequential_counts[0 if created else 1] += 1

        keys = (
            "trip_id",
            "origin",
            "destination",
            "start_date",
            "end_date",
            "purpose",
            "session_id",
        )

        def snapshot(store):
            return [
                {key: trip.get(key) for key in keys}
                for trip in store.get_trip_history(limit=None)
            ]

        assert batch_counts == tuple(sequential_counts), (
            batch_counts,
            sequential_counts,
        )
        assert snapshot(batch) == snapshot(sequential)
        assert (
            batch.get_statistics()["total_trips"]
            == sequential.get_statistics()["total_trips"]
        )
        print("✓ PG 批量保存与逐条保存结果一致")
    finally:
        batch.close()
        sequential.close()


def test_pg_and_json_trip_saves_match():
    """同一批行程在 PostgreSQL 与 JSON 后端的批量保存结果一致。"""
    memory, _ = _pg_memory_or_skip()
    import tempfile

    from context.long_term_memory import LongTermMemory

    json_store = LongTermMemory("json-parity", tempfile.mkdtemp())
    rows = [
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-1",
            "end_date": "2026-12-3",
            "purpose": "客户会议",
        },
        {
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-10",
            "purpose": "培训",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
            "purpose": "会议",
            "session_id": "s-1",
        },
        {
            "origin": "",
            "destination": "广州",
            "start_date": "2026-12-20",
            "end_date": "2026-12-22",
            "purpose": "会议",
            "session_id": "s-1",
        },
    ]
    try:
        pg_counts = memory.save_trip_histories([dict(row) for row in rows])
        json_counts = json_store.save_trip_histories([dict(row) for row in rows])
        assert pg_counts == json_counts, (pg_counts, json_counts)

        keys = (
            "trip_id",
            "origin",
            "destination",
            "start_date",
            "end_date",
            "purpose",
            "session_id",
        )

        def snapshot(store):
            return [
                {key: trip.get(key) for key in keys}
                for trip in store.get_trip_history(limit=None)
            ]

        assert snapshot(memory) == snapshot(json_store)
        assert (
            memory.get_statistics()["total_trips"]
            == json_store.get_statistics()["total_trips"]
        )
        # 同数量的城市顺序取决于数据库排序规则：按语义（城市 → 次数）比较
        assert dict(memory.get_frequent_destinations(10)) == dict(
            json_store.get_frequent_destinations(10)
        )
        # JSON 后端同数量按城市名排序（与 PostgreSQL ORDER BY 同口径）
        json_frequent = json_store.get_frequent_destinations(10)
        assert json_frequent == sorted(
            json_frequent, key=lambda item: (-item[1], item[0])
        ), json_frequent
        print("✓ JSON / PG 批量保存结果一致")
    finally:
        memory.close()


def test_pg_update_trip_normalizes_unpadded_dates():
    """update_trip 的重复检测与 save_trip_history 同一口径（未补零日期）。"""
    memory, _ = _pg_memory_or_skip()
    try:
        trip_1 = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "purpose": "出差",
            }
        )
        trip_2 = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-05",
                "purpose": "出差",
            }
        )
        assert trip_1 and trip_2 and trip_1 != trip_2
        # 历史行可能是未补零写法：写入后用补零写法更新另一条为同一天，
        # 必须判重复（SQL 等值比较会漏掉这种写法差异）
        assert memory.update_trip(trip_2, {"start_date": "2026-12-8"}) is True
        with pytest.raises(ValueError):
            memory.update_trip(
                trip_1, {"destination": "杭州", "start_date": "2026-12-08"}
            )
        print("✓ PG update_trip 未补零日期的重复检测")
    finally:
        memory.close()


def test_pg_add_chat_turn_guard_vetoes_write():
    """PG add_chat_turn：落库守卫返回 False（会话刚被删除）时整轮不写入。"""
    memory, _ = _pg_memory_or_skip()
    try:
        assert (
            memory.add_chat_turn(
                "已删除会话的提问", "回答", "s-veto", should_commit=lambda: False
            )
            is False
        )
        assert memory.get_chat_history(session_id="s-veto") == []
        print("✓ PG add_chat_turn 落库守卫否决整轮写入")
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


def test_pg_add_chat_turn_waits_for_session_meta_lock():
    """并发删除持会话元数据锁时，add_chat_turn 阻塞到删除提交后才判守卫。"""
    memory_a, user_id = _pg_memory_or_skip()
    from context.postgres_long_term_memory import PostgresLongTermMemory

    memory_b = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
    lock_key = f"aligo:session_meta:{user_id}"
    session_id = "s-lock-order"
    try:
        assert memory_a.add_chat_turn("旧问", "旧答", session_id) is True

        b_locked = threading.Event()
        allow_delete = threading.Event()
        a_done = threading.Event()
        state = {"deleted": False}
        result: list[bool] = []

        def delete_with_lock():
            with memory_b._conn.transaction(), memory_b._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,)
                )
                b_locked.set()
                assert allow_delete.wait(10)
                cursor.execute(
                    "DELETE FROM aligo_chat_messages "
                    "WHERE user_id = %s AND session_id = %s",
                    (user_id, session_id),
                )
                state["deleted"] = True
            # 事务在此提交：会话元数据锁释放，阻塞的 add_chat_turn 才能继续

        def guard():
            return not state["deleted"]

        def write_turn():
            result.append(
                memory_a.add_chat_turn(
                    "新问", "新答", session_id, should_commit=guard
                )
            )
            a_done.set()

        delete_thread = threading.Thread(target=delete_with_lock)
        delete_thread.start()
        assert b_locked.wait(10), "删除线程未拿到会话元数据锁"
        write_thread = threading.Thread(target=write_turn)
        write_thread.start()
        # 删除事务持锁期间落库必须阻塞：守卫不能在删除提交前执行
        assert not a_done.wait(0.5), "add_chat_turn 必须在会话元数据锁上等待"
        allow_delete.set()
        write_thread.join(10)
        delete_thread.join(10)
        assert not write_thread.is_alive() and not delete_thread.is_alive()
        assert result == [False], "会话已删除，在途落库必须被否决"
        assert memory_a.get_chat_history(session_id=session_id) == []
        print("✓ PG add_chat_turn 阻塞到并发删除提交后")
    finally:
        memory_a.close()
        memory_b.close()


def test_pg_delete_all_resets_trip_counters():
    """PG delete_all：行程编号计数器一并删除（重新从 trip_1 开始）。"""
    memory, _ = _pg_memory_or_skip()
    try:
        first = memory.save_trip_history(
            {"origin": "北京", "destination": "上海", "start_date": "2027-01-01"}
        )
        assert first, "首次保存应返回 trip_id"
        memory.delete_all()
        second = memory.save_trip_history(
            {"origin": "北京", "destination": "上海", "start_date": "2027-01-05"}
        )
        assert second == first, "delete_all 后编号计数器必须清零（与 JSON 后端一致）"
        print("✓ PG delete_all 清零行程编号计数器")
    finally:
        memory.close()


def test_pg_batch_save_session_id_none_semantics():
    """批量恢复：session_id 键存在且为 None 时清除关联，键缺失保留原值（与 JSON 同口径）。"""
    memory, _ = _pg_memory_or_skip()
    try:
        base = {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-05",
            "session_id": "s-dead",
        }
        memory.save_trip_history(dict(base))
        created, updated = memory.save_trip_histories(
            [{**base, "session_id": None}]
        )
        assert (created, updated) == (0, 1), (created, updated)
        trips = memory.get_trip_history(limit=None)
        assert len(trips) == 1, trips
        assert trips[0]["session_id"] is None, (
            "显式 None 必须清除被删除会话的关联（COALESCE 会把它当未提供）"
        )

        keep = {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-06",
            "session_id": "s-keep",
        }
        memory.save_trip_history(dict(keep))
        without_key = {
            key: value for key, value in keep.items() if key != "session_id"
        }
        created, updated = memory.save_trip_histories([without_key])
        assert (created, updated) == (0, 1), (created, updated)
        by_date = {
            trip["start_date"]: trip for trip in memory.get_trip_history(limit=None)
        }
        assert by_date["2026-12-06"]["session_id"] == "s-keep", (
            "键缺失时必须保留原关联"
        )
        print("✓ PG 批量保存 session_id 显式 None / 缺失语义与 JSON 一致")
    finally:
        memory.close()


def test_pg_export_snapshot_matches_lists():
    """PG 导出一致性快照：统计与列表来自同一事务版本。"""
    memory, _ = _pg_memory_or_skip()
    try:
        memory.add_chat_message("user", "Q", "s-snap")
        memory.add_chat_message("assistant", "A", "s-snap")
        memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-30",
                "end_date": "2026-12-31",
            }
        )
        snapshot = memory.export_snapshot()
        assert len(snapshot["chat_history"]) == 2
        assert snapshot["statistics"]["total_messages"] == 2
        assert len(snapshot["trip_history"]) == 1
        assert snapshot["statistics"]["total_trips"] == 1
        assert snapshot["statistics"]["frequent_destinations"] == {"上海": 1}
        print("✓ PG 导出快照与列表一致")
    finally:
        memory.close()


def test_pg_batch_save_preserves_trip_id_and_timestamp():
    """PG 批量恢复沿用备份 trip_id / timestamp；编号冲突时退回新编号。"""
    memory, _ = _pg_memory_or_skip()
    try:
        base = {
            "origin": "北京",
            "destination": "济南",
            "start_date": "2026-12-11",
            "end_date": "2026-12-12",
            "purpose": "会议",
            "trip_id": "trip_7",
            "timestamp": "2025-06-01T08:30:00+08:00",
        }
        assert memory.save_trip_histories([dict(base)]) == (1, 0)
        trips = {
            trip["destination"]: trip
            for trip in memory.get_trip_history(limit=None)
        }
        assert trips["济南"]["trip_id"] == "trip_7", "未占用的备份编号要沿用"
        assert "2025-06-01" in trips["济南"]["timestamp"], "备份时间戳要保留"

        clash = {
            "origin": "北京",
            "destination": "青岛",
            "start_date": "2026-12-13",
            "end_date": "2026-12-14",
            "trip_id": "trip_7",
        }
        assert memory.save_trip_histories([clash]) == (1, 0)
        trips = {
            trip["destination"]: trip
            for trip in memory.get_trip_history(limit=None)
        }
        assert trips["青岛"]["trip_id"] != "trip_7", "冲突的备份编号不能复用"
        ids = [trip["trip_id"] for trip in memory.get_trip_history(limit=None)]
        assert len(ids) == len(set(ids)), ids

        new_id = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "银川",
                "start_date": "2026-12-20",
                "end_date": "2026-12-21",
            }
        )
        assert int(new_id.split("_")[1]) > 7, "编号水位按沿用的备份编号抬高"
        print("✓ PG 批量恢复保留 trip_id / 时间戳并拒绝编号冲突")
    finally:
        memory.close()


def test_pg_save_trip_histories_counts_mixed_batch():
    """PG 批量保存：created/updated 计数正确，批内去重且缺省目的保留原值。"""
    memory, _ = _pg_memory_or_skip()
    try:
        base = {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "purpose": "客户会议",
        }
        memory.save_trip_history(dict(base))
        without_purpose = {key: value for key, value in base.items() if key != "purpose"}
        new_trip = {
            **base,
            "destination": "杭州",
            "start_date": "2026-12-02",
            "purpose": "会议",
        }
        new_without_purpose = {
            key: value for key, value in new_trip.items() if key != "purpose"
        }
        created, updated = memory.save_trip_histories(
            [without_purpose, new_trip, new_without_purpose]
        )
        assert (created, updated) == (1, 2), (created, updated)
        trips = {
            trip["destination"]: trip for trip in memory.get_trip_history(limit=None)
        }
        assert trips["上海"]["purpose"] == "客户会议", "缺省目的不能覆盖已有目的"
        assert trips["杭州"]["purpose"] == "会议", "批内重复命中刚插入的行并保留目的"
        assert memory.get_statistics()["total_trips"] == 2
        print("✓ PG 批量保存 created/updated 计数与批内去重")
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
        # 无出发日期的行程不计入常去目的地（与 JSON 保存 / 迁移口径一致）
        assert memory.get_frequent_destinations(1) == [("上海", 1)]

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
        assert stats["frequent_destinations"] == {"上海": 1}

        assert memory.delete_trip("trip_1") is True
        assert memory.delete_trip("trip_1") is False
        # 仅剩无日期行程：与 JSON 同口径不计入常去目的地
        assert memory.get_frequent_destinations(1) == []
        memory.save_trip_history({"origin": "上海", "destination": "杭州", "purpose": "出差"})
        newest = memory.get_trip_history(1)[0]
        assert newest["trip_id"] == "trip_3", "删除后新增按现有最大 id（trip_2）递增"
        assert memory.get_statistics()["total_trips"] == 2

        # 旧版本写入的消息 session_id 为 NULL：排除某会话时必须保留
        memory.add_chat_message("user", "无会话旧消息", None)
        excluded = memory.get_chat_history(limit=10, exclude_session_id="s1")
        assert [item["content"] for item in excluded] == ["其他会话", "无会话旧消息"], (
            "IS DISTINCT FROM 才会保留 NULL 会话旧消息（!= 会漏掉）"
        )

        assert memory.delete_chat_session("s1") == 2
        assert memory.get_chat_history(session_id="s1") == []
        assert memory.get_session_titles() == {}
        assert memory.get_pinned_sessions() == []
        assert len(memory.get_chat_history()) == 2

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


def test_add_script_marks_applied_only_after_successful_push():
    """RPUSH 失败时 op_id 不能先进入 applied：否则恢复重放会永久跳过该消息。"""
    client = _redis_client_or_skip()
    from context.short_term_memory import RedisShortTermMemory, applied_key

    session_id = f"pytest-{uuid.uuid4().hex[:8]}"
    memory = RedisShortTermMemory(
        client, session_id, max_turns=2, ttl_sec=60, user_id="pytest-user"
    )
    try:
        # messages 键设成字符串：脚本里的 RPUSH 报 WRONGTYPE，追加入队待重放
        client.set(memory.key, "wrong-type")
        memory.add_message("user", "pending")

        assert client.smembers(applied_key(memory.key)) == set(), (
            "RPUSH 失败时不能把 op_id 记为已追加"
        )

        # 修复键类型后再次读取：重放必须把排队消息补写进 Redis
        client.delete(memory.key)
        contents = [
            item["content"] for item in memory.get_recent_context(n_turns=5)
        ]
        assert contents == ["pending"], "恢复后重放必须补写失败时丢失的消息"
    finally:
        client.delete(memory.key, applied_key(memory.key))
    print("✓ 追加脚本先 RPUSH 后记账（失败消息可重放补写）")


if __name__ == "__main__":
    test_redis_short_term_memory()
    test_postgres_long_term_memory()
    test_postgres_trip_id_concurrent()
    # 本地脚本运行时手动设置环境变量才能跑第三项
    print("\n完成（未配置服务时相关用例跳过）")
