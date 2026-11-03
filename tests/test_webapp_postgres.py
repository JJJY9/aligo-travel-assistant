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


if __name__ == "__main__":
    print("该测试需要 TEST_POSTGRES_DSN，请通过 pytest 运行")
