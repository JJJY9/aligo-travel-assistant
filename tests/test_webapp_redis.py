"""
Web API + Redis 短期记忆集成测试（需要真实 Redis）

CI 中通过 `TEST_REDIS_URL` 自动启用；未提供时跳过。验证 Web 端在 Redis
短期记忆下的完整路径：对话写入 Redis List、状态接口显示 redis 后端、
删除会话时清理 Redis 键（长期记忆仍使用默认 JSON 并清理测试文件）。

运行：
    TEST_REDIS_URL=redis://localhost:6379/0 \\
        pytest tests/test_webapp_redis.py -q
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from fastapi.testclient import TestClient

from config_agentscope import UsageTrackingModel
from context import storage
from context.short_term_memory import RedisShortTermMemory, session_key
from webapp.server import MEMORY_DIR, create_app

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_REDIS_URL, reason="未设置 TEST_REDIS_URL"
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


def test_webapp_with_redis_short_term(monkeypatch):
    user_id = f"webredis-user-{uuid.uuid4().hex[:8]}"
    session_id = f"webredis-{uuid.uuid4().hex[:8]}"
    try:
        import redis

        client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 测试环境无 Redis 时跳过
        pytest.skip(f"Redis 不可用: {exc}")

    monkeypatch.setitem(storage.STORAGE_CONFIG, "redis_url", TEST_REDIS_URL)
    storage.reset_failure_cache()

    # 确认工厂确实返回 Redis 实现（而不是连接失败后回退进程内实现）
    probe = storage.create_short_term_memory(session_id)
    try:
        assert isinstance(
            probe, RedisShortTermMemory
        ), "配置 Redis 后应使用 Redis 短期记忆"
    finally:
        probe.clear()

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    http = TestClient(app)
    memory_file = Path(MEMORY_DIR) / f"{user_id}.json"
    try:
        chat = http.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": session_id},
        )
        assert chat.status_code == 200

        status = http.get("/api/status", params={"user_id": user_id}).json()
        assert status["storage"]["short_term"] == "redis"

        key = session_key(session_id, user_id)
        assert client.exists(key) == 1, "对话应写入 Redis 短期记忆"
        assert client.llen(key) >= 2, "至少包含用户与助手两条消息"

        second = http.post(
            "/api/chat",
            json={"message": "第二问", "user_id": user_id, "session_id": session_id},
        )
        assert second.status_code == 200
        assert client.llen(key) >= 4

        # 删除最后一轮对话 → Redis 短期记忆与长期记忆同步清理（走 RTRIM 分支）
        before = client.llen(key)
        turn = http.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "第二问"},
        )
        assert turn.status_code == 200
        assert turn.json()["removed"] == 2
        assert client.llen(key) == before - 2
        first = json.loads(client.lindex(key, 0))
        assert first["content"] == "你好", "应保留第一轮消息，只裁掉最后一轮"

        # 未缓存会话（重启 / LRU 淘汰）也能清理 Redis（用户级键）
        third = http.post(
            "/api/chat",
            json={"message": "第三问", "user_id": user_id, "session_id": session_id},
        )
        assert third.status_code == 200
        before_uncached = client.llen(key)
        app.state.aligo.sessions.clear()
        uncached = http.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "第三问"},
        )
        assert uncached.status_code == 200
        assert uncached.json()["removed"] == 2
        assert client.llen(key) == before_uncached - 2

        # Lua 脚本的内容校验：不一致时不动列表，匹配时删除整轮
        probe = RedisShortTermMemory(
            client, f"redis-expect-{uuid.uuid4().hex[:8]}", user_id=user_id
        )
        try:
            probe.add_message("user", "Q1")
            probe.add_message("assistant", "A1")
            assert probe.truncate_last_turn("其它内容") == 0
            assert client.llen(probe.key) == 2
            assert probe.truncate_last_turn("Q1") == 2
            assert client.exists(probe.key) == 0
        finally:
            client.delete(probe.key)

        # UTF-8 空白归一：全角空格 / 不间断空格视为一致（Python strip 与
        # Lua 裁剪必须同一字符集）；以 0xA0 / 0x80 等字节结尾的汉字不能被
        # 误裁（旧实现把字节写进 Lua 字符类，会截断最后一个字节）
        utf8_probe = RedisShortTermMemory(
            client, f"redis-utf8-{uuid.uuid4().hex[:8]}", user_id=user_id
        )
        try:
            utf8_probe.add_message("user", "全角空格结尾　")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("全角空格结尾") == 2

            utf8_probe.add_message("user", "不间断空格结尾 ")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("不间断空格结尾") == 2

            # 丠 U+4E20 → UTF-8 E4 B8 A0（以 0xA0 结尾）
            utf8_probe.add_message("user", "提问丠")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("提问丠") == 2
            assert client.exists(utf8_probe.key) == 0
        finally:
            client.delete(utf8_probe.key)

        # 同一 session_id 的不同用户使用不同键，互不影响
        shared_session = f"redis-shared-{uuid.uuid4().hex[:8]}"
        alice = RedisShortTermMemory(client, shared_session, user_id="alice")
        bob = RedisShortTermMemory(client, shared_session, user_id="bob")
        try:
            alice.add_message("user", "alice 的消息")
            bob.add_message("user", "bob 的消息")
            assert alice.key != bob.key
            assert client.llen(alice.key) == 1 and client.llen(bob.key) == 1
            assert alice.truncate_last_turn("alice 的消息") == 1
            assert client.exists(alice.key) == 0
            assert client.llen(bob.key) == 1, "不应清理其它用户的会话"
        finally:
            client.delete(alice.key, bob.key)

        # 删除会话 → 同时清理 Redis 短期记忆
        deleted = http.delete(
            f"/api/sessions/{session_id}", params={"user_id": user_id}
        )
        assert deleted.status_code == 200
        assert client.exists(key) == 0
        print("✓ Web API 在 Redis 短期记忆下的读写与清理")
    finally:
        client.delete(session_key(session_id), session_key(session_id, user_id))
        memory_file.unlink(missing_ok=True)
        storage.reset_failure_cache()


if __name__ == "__main__":
    print("该测试需要 TEST_REDIS_URL，请通过 pytest 运行")
