"""
Web API + PostgreSQL 后端集成测试（需要真实 PostgreSQL）

CI 中通过 `TEST_POSTGRES_DSN` 自动启用；未提供时跳过。验证 Web 端在
PostgreSQL 长期记忆下的完整读写路径（偏好 / 行程 / 会话元数据 / 状态 / 导出 / 删除），
并确认配置 DSN 后不会静默回退到本地 JSON。

运行：
    TEST_POSTGRES_DSN=postgresql://aligo:aligo@localhost:5432/aligo \\
        pytest tests/test_webapp_postgres.py -q
"""
import json
import os
import sys
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from fastapi.testclient import TestClient

from config_agentscope import UsageTrackingModel
from context import storage
from context.postgres_long_term_memory import PostgresLongTermMemory
from webapp.server import create_app

TEST_POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN, reason="未设置 TEST_POSTGRES_DSN"
)

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


def test_webapp_with_postgres_backend(monkeypatch):
    user_id = f"webpg-{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(storage.STORAGE_CONFIG, "postgres_dsn", TEST_POSTGRES_DSN)
    storage.reset_failure_cache()

    # 先确认 PostgreSQL 可用（不可用时按集成测试惯例跳过）
    try:
        direct = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        direct.close()
    except Exception as exc:  # noqa: BLE001 - 测试环境无 PG 时跳过
        pytest.skip(f"PostgreSQL 不可用: {exc}")

    # 确认工厂确实返回 PostgreSQL 实现（而不是连接失败后回退 JSON）
    probe = storage.create_long_term_memory(user_id, "data/memory")
    try:
        assert isinstance(
            probe, PostgresLongTermMemory
        ), "配置 DSN 后应使用 PostgreSQL 后端"
    finally:
        close = getattr(probe, "close", None)
        if callable(close):
            close()

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    try:
        # 偏好
        preference = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "北京"},
        )
        assert preference.status_code == 200

        # 行程（手动添加，写入 PG）
        trip = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-11-20",
                "end_date": "2026-11-22",
                "purpose": "出差",
            },
        )
        assert trip.status_code == 201
        trip_id = trip.json()["trip_id"]

        # 对话（写入 PG 聊天记录与执行元数据）
        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-pg-web"},
        )
        assert chat.status_code == 200

        # 会话重命名 / 置顶（PG 元数据表）
        assert (
            client.put(
                "/api/sessions/sess-pg-web/title",
                json={"user_id": user_id, "title": "PG 对话"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/sessions/sess-pg-web/pin",
                json={"user_id": user_id, "pinned": True},
            ).status_code
            == 200
        )
        # 重复置顶必须幂等：ON CONFLICT DO NOTHING 不返回行，不能误判 404
        assert (
            client.put(
                "/api/sessions/sess-pg-web/pin",
                json={"user_id": user_id, "pinned": True},
            ).status_code
            == 200
        )

        # 归档 / 恢复（PG 归档表）
        assert (
            client.put(
                "/api/sessions/sess-pg-web/archive",
                json={"user_id": user_id, "archived": True},
            ).status_code
            == 200
        )
        # 重复归档同样幂等
        assert (
            client.put(
                "/api/sessions/sess-pg-web/archive",
                json={"user_id": user_id, "archived": True},
            ).status_code
            == 200
        )
        archived_list = client.get(
            "/api/sessions", params={"user_id": user_id, "archived": True}
        ).json()
        assert [item["session_id"] for item in archived_list["sessions"]] == [
            "sess-pg-web"
        ]
        assert archived_list["sessions"][0]["archived"] is True
        active_list = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert active_list["sessions"] == []
        assert active_list["archived_count"] == 1
        assert (
            client.put(
                "/api/sessions/sess-pg-web/archive",
                json={"user_id": user_id, "archived": False},
            ).status_code
            == 200
        )

        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["storage"]["long_term"] == "postgres"
        assert status["preferences"]["home_location"] == "北京"
        assert status["trip_count"] == 1
        assert status["trips"][0]["destination"] == "上海"

        sessions = client.get(
            "/api/sessions", params={"user_id": user_id}
        ).json()["sessions"]
        assert sessions
        assert sessions[0]["session_id"] == "sess-pg-web"
        assert sessions[0]["title"] == "PG 对话"
        assert sessions[0]["pinned"] is True

        # 导出与删除
        exported = client.get(
            "/api/sessions/sess-pg-web/export", params={"user_id": user_id}
        )
        assert exported.status_code == 200
        assert "PG 对话" in exported.text
        assert (
            client.delete(f"/api/trips/{trip_id}", params={"user_id": user_id}).status_code
            == 200
        )
        assert (
            client.get("/api/status", params={"user_id": user_id}).json()["trip_count"]
            == 0
        )

        # 删除最后一轮对话（按 id 倒序识别用户 + 助手两条消息）
        turn = client.delete(
            "/api/sessions/sess-pg-web/last-turn", params={"user_id": user_id}
        )
        assert turn.status_code == 200
        assert turn.json()["removed"] == 2
        remaining = client.get(
            "/api/sessions/sess-pg-web", params={"user_id": user_id}
        ).json()["messages"]
        assert remaining == []

        # 删除会话时同步清理归档表
        cleanup_store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        try:
            cleanup_store.add_chat_message("user", "临时", "sess-arch-cleanup")
            cleanup_store.set_session_archived("sess-arch-cleanup", True)
            assert "sess-arch-cleanup" in cleanup_store.get_archived_sessions()
            cleanup_store.delete_chat_session("sess-arch-cleanup")
            assert "sess-arch-cleanup" not in cleanup_store.get_archived_sessions()
            # clear_history / delete_all 同样清理归档标记
            cleanup_store.add_chat_message("user", "临时2", "sess-arch-clear")
            cleanup_store.set_session_archived("sess-arch-clear", True)
            cleanup_store.clear_history()
            assert cleanup_store.get_archived_sessions() == []
            cleanup_store.add_chat_message("user", "临时3", "sess-arch-delete")
            cleanup_store.set_session_archived("sess-arch-delete", True)
            cleanup_store.delete_all()
            assert cleanup_store.get_archived_sessions() == []
        finally:
            cleanup_store.close()
        print("✓ Web API 在 PostgreSQL 后端下的读写路径")
    finally:
        try:
            cleanup = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
            try:
                cleanup.delete_all()
            finally:
                cleanup.close()
        except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
            pass
        storage.reset_failure_cache()


def test_advisory_lock_reacquired_after_reconnect():
    """连接中断重连后，进程内持有中的 advisory lock 必须在新连接上重新获取。

    会话级锁随旧连接由服务端释放；若重连后不重新获取，重试的读改写会在
    没有互斥保护的情况下执行（并发重复生成 trip_N / 覆盖偏好）。
    """
    user_id = f"pglock-{uuid.uuid4().hex[:8]}"
    store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)

    def held_lock_count() -> int:
        row = store._fetchone(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        )
        return int(row[0]) if row else 0

    try:
        with store._advisory_lock(f"aligo-test-{user_id}"):
            assert held_lock_count() >= 1
            # 模拟连接被服务端终止：下一次操作会触发重连与重试
            store._conn.close()
            store.get_preference("home_location")
            assert held_lock_count() >= 1, "重连后未重新获取 advisory lock"
        # 释放后不再持有
        assert held_lock_count() == 0
        print("✓ advisory lock 在重连后重新获取")
    finally:
        store.close()


def test_advisory_lock_reentrant_reacquire_after_reconnect():
    """嵌套持有同一把 advisory lock 时，重连必须按重入计数恢复服务端持有。

    会话级 advisory lock 可重入：重连后只恢复一次时，内层解锁会把整把锁
    释放，外层临界区失去互斥。用另一条连接尝试加锁来验证（持有时拿不到）。
    """
    import psycopg

    user_id = f"pglock-nested-{uuid.uuid4().hex[:8]}"
    lock_name = f"aligo-test-nested-{user_id}"
    store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
    other = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True, connect_timeout=3)

    def other_can_acquire() -> bool:
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (lock_name,)
            )
            acquired = bool(cursor.fetchone()[0])
            if acquired:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (lock_name,)
                )
            return acquired

    try:
        with store._advisory_lock(lock_name):
            with store._advisory_lock(lock_name):
                assert not other_can_acquire()
                store._conn.close()
                store.get_preference("home_location")  # 触发重连 + 按计数重取
                assert not other_can_acquire(), "重连后锁应仍然持有"
            assert not other_can_acquire(), "内层解锁不能把整把锁释放"
        assert other_can_acquire(), "外层解锁后应可被其它连接获取"
        print("✓ advisory lock 重连后按重入计数恢复")
    finally:
        other.close()
        store.close()


if __name__ == "__main__":
    print("该测试需要 TEST_POSTGRES_DSN，请通过 pytest 运行")
