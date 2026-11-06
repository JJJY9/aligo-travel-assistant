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
from context.short_term_memory import (
    RedisShortTermMemory,
    applied_key,
    session_key,
)
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

            # 提问内部的全角空格必须原样保留：只裁两端空白，不能把内部
            # U+3000 归一半角，否则与 Python strip() 后的 expected 对不上
            utf8_probe.add_message("user", "明天去上海　还是后天")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("明天去上海　还是后天") == 2

            # 内部 NBSP 同样保留
            utf8_probe.add_message("user", "内部 空格")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("内部 空格") == 2

            # 两端其它 Unicode 空白（U+2003 与 U+202F）与 Python strip() 同集合
            utf8_probe.add_message("user", "窄空格结尾 ")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("窄空格结尾") == 2

            # 混合、多轮剥离：半角空格 + NBSP + 全角空格叠加
            utf8_probe.add_message("user", "　  混合空白结尾　")
            utf8_probe.add_message("assistant", "A")
            assert utf8_probe.truncate_last_turn("混合空白结尾") == 2
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


class _OutageProxy:
    """包装真实 Redis 客户端：模拟连接故障与「脚本已执行但响应丢失」。"""

    def __init__(self, client):
        self._client = client
        self.fail = False
        self.lose_eval_response = False

    def __getattr__(self, name):
        if self.fail:
            raise ConnectionError("redis down")
        return getattr(self._client, name)

    def eval(self, *args, **kwargs):
        if self.fail:
            raise ConnectionError("redis down")
        result = self._client.eval(*args, **kwargs)
        if self.lose_eval_response:
            self.lose_eval_response = False
            raise ConnectionError("response lost after script")
        return result


def test_redis_short_term_replays_after_outage(monkeypatch):
    """真实 Redis：故障期新增 / 截断 / 清空在恢复后按序重放（执行真实 Lua）。"""
    from context import short_term_memory as short_term_module

    try:
        import redis

        client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 测试环境无 Redis 时跳过
        pytest.skip(f"Redis 不可用: {exc}")

    session_id = f"redis-outage-{uuid.uuid4().hex[:8]}"
    user_id = f"redis-outage-user-{uuid.uuid4().hex[:8]}"
    key = session_key(session_id, user_id)
    client.delete(key)
    proxy = _OutageProxy(client)

    def reset_degraded():
        monkeypatch.setattr(short_term_module, "_redis_degraded_until", 0.0)

    def contents(memory):
        return [item["content"] for item in memory.get_recent_context()]

    try:
        memory = RedisShortTermMemory(
            proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
        )
        for role, content in [
            ("user", "Q1"),
            ("assistant", "A1"),
            ("user", "Q2"),
            ("assistant", "A2"),
        ]:
            memory.add_message(role, content)
        assert client.llen(key) == 4

        # 故障期新增（孤立的用户消息）+ 截断：重放后 Redis 与影子缓存一致
        proxy.fail = True
        reset_degraded()
        memory.add_message("user", "Q3")
        reset_degraded()
        assert memory.truncate_last_turn("Q3") == 1
        assert contents(memory) == ["Q1", "A1", "Q2", "A2"]

        proxy.fail = False
        reset_degraded()
        assert contents(memory) == ["Q1", "A1", "Q2", "A2"]
        assert client.llen(key) == 4

        # 未命中会话缓存的临时对象：排队后即丢弃，后续实例用真实 Lua 重放
        proxy.fail = True
        reset_degraded()
        transient = RedisShortTermMemory(
            proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
        )
        assert transient.truncate_last_turn("Q2") == 0
        del transient

        proxy.fail = False
        reset_degraded()
        follower = RedisShortTermMemory(
            proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
        )
        assert contents(follower) == ["Q1", "A1"], "丢弃临时对象后排队的截断仍要生效"

        # 追加消息的脚本已执行但响应丢失：重放凭 applied 集合去重，不重复追加
        proxy.lose_eval_response = True
        reset_degraded()
        memory.add_message("user", "Q4")
        assert client.llen(key) == 3, "消息已在服务端写入"
        proxy.lose_eval_response = False
        reset_degraded()
        contents = [item["content"] for item in follower.get_recent_context()]
        assert contents == ["Q1", "A1", "Q4"], "响应丢失后重放不能重复追加"
        assert client.llen(key) == 3

        # 在线截断已执行但响应丢失：重放用同一 op_id，凭一次性标记跳过
        proxy.lose_eval_response = True
        reset_degraded()
        transient2 = RedisShortTermMemory(
            proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
        )
        assert transient2.truncate_last_turn("Q4") == 0
        assert client.llen(key) == 2, "在线截断已在服务端生效（只剩第一轮）"
        proxy.lose_eval_response = False
        reset_degraded()
        follower = RedisShortTermMemory(
            proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
        )
        assert contents(follower) == ["Q1", "A1"], "重放凭标记跳过，不会重复删除"

        # 故障期清空：恢复后由后续实例重放删除
        proxy.fail = True
        reset_degraded()
        follower.clear()
        proxy.fail = False
        reset_degraded()
        assert contents(
            RedisShortTermMemory(
                proxy, session_id, max_turns=5, user_id=user_id, ttl_sec=60
            )
        ) == []
        print("✓ 真实 Redis 上故障期变更恢复后按序重放")
    finally:
        proxy.fail = False
        client.delete(key, applied_key(key))


if __name__ == "__main__":
    print("该测试需要 TEST_REDIS_URL，请通过 pytest 运行")
