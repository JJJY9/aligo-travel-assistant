"""
Web 界面 API 测试（无需 API Key、不访问网络）

通过注入假模型走通完整接口：页面、健康检查、状态、对话（含 SSE 流式进度）、
会话历史 / 删除、导出。真实大模型链路由其它测试覆盖。

运行：python tests/test_webapp.py
"""
import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime

import pytest
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from fastapi.testclient import TestClient

from config_agentscope import UsageTrackingModel
from webapp.server import MEMORY_DIR, create_app

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
            "content": [{"type": "text", "text": json.dumps(_INTENT, ensure_ascii=False)}],
            "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.05},
        }


class _FailingModel:
    async def __call__(self, messages, **kwargs):
        raise ConnectionError("connection reset")


def _client() -> TestClient:
    return TestClient(create_app(model=UsageTrackingModel(_FakeModel())))


def _cleanup(user_id: str) -> None:
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    if path.exists():
        path.unlink()


def _unique_user(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_index_page():
    response = _client().get("/")
    assert response.status_code == 200
    assert "Aligo" in response.text
    assert "下一站，去哪里" in response.text
    # 入口页必须每次重新验证：缓存的旧 index.html 搭配新 app.js 会让整页脚本中止
    assert response.headers.get("cache-control") == "no-cache"
    print("✓ 首页可访问（新版界面）")


def test_health_endpoint():
    data = _client().get("/api/health").json()
    assert data["status"] == "ok"
    assert data["version"]
    assert data["storage"]["short_term"] in {"memory", "redis"}
    assert data["storage"]["long_term"] in {"json", "postgres"}
    assert data["rate_limit"] in {"memory", "redis"}
    assert data["rate_limit_configured"] is False
    assert data["rate_limit_degraded"] is False
    assert data["skills"]["count"] >= 6
    assert data["skills"]["dir_found"] is True
    print("✓ /api/health 返回版本 / 模型 / 存储后端")


def test_health_marks_configured_redis_fallback_degraded(monkeypatch):
    """配置 REDIS_URL 但限流器为进程内（启动时 Redis 不可用）时标记降级。

    在 create_app 之前写入配置，构造出真正的「启动即配置了 Redis」状态；
    并断言限流器仍是 Redis 实现且带工厂（换成纯内存实现会在类型断言失败）。
    """
    from context import storage
    from utils.rate_limit import RedisRateLimiter
    from webapp import server as server_module

    monkeypatch.setitem(
        storage.STORAGE_CONFIG, "redis_url", "redis://127.0.0.1:1/0"
    )
    reset = getattr(server_module, "reset_shared_redis_state", None)
    if callable(reset):
        reset()
    storage.reset_failure_cache()
    try:
        client = _client()
        state = client.app.state.aligo
        assert isinstance(state.rate_limiter, RedisRateLimiter), (
            "配置了 Redis 时桶应保留 Redis 实现（仅降级回退）"
        )
        assert state.rate_limiter.degraded is True
        data = client.get("/api/health").json()
        assert data["rate_limit"] == "redis", (
            "限流器保留 Redis 实现时 backend 应报告 redis（回退由 degraded 表示）"
        )
        assert data["rate_limit_configured"] is True
        assert data["rate_limit_degraded"] is True
    finally:
        storage.reset_failure_cache()
    print("✓ 配置 Redis 但回退进程内限流时 health 标记降级（限流器仍为 Redis 实现）")


def test_health_reports_any_degraded_limiter():
    """用户桶未降级、知识库桶降级时也应上报 degraded。"""

    class _DegradedLimiter:
        backend = "redis"
        limit = 1
        window_sec = 60
        degraded = True

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    app.state.aligo.kb_limiter = _DegradedLimiter()
    data = TestClient(app).get("/api/health").json()
    assert data["rate_limit_degraded"] is True
    print("✓ 任一限流桶降级都会上报 degraded")


def test_weather_api(monkeypatch):
    from utils.weather import WeatherError
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda city: {
            "city": city,
            "current": {"description": "晴", "temperature": 12},
            "daily": [],
        },
    )
    client = _client()
    data = client.get("/api/weather", params={"city": "北京"}).json()
    assert data["city"] == "北京" and data["current"]["description"] == "晴"

    assert client.get("/api/weather", params={"city": "   "}).status_code == 422

    def missing(city):
        raise WeatherError(f"未找到城市「{city}」")

    monkeypatch.setattr(server_module, "fetch_weather", missing)
    assert client.get("/api/weather", params={"city": "不存在"}).status_code == 404

    def unavailable(city):
        raise WeatherError("天气接口暂时不可用")

    monkeypatch.setattr(server_module, "fetch_weather", unavailable)
    assert client.get("/api/weather", params={"city": "北京"}).status_code == 503
    print("✓ /api/weather 天气查询（成功 / 422 / 404 / 503）")


def test_weather_rate_limit(monkeypatch):
    """天气接口按客户端 IP 独立限流，空白城市不消耗额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda city: {"city": city, "current": {}, "daily": []},
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    app.state.aligo.weather_limiter = RateLimiter(limit=1, window_sec=60)
    client = TestClient(app)

    first = client.get("/api/weather", params={"city": "北京"})
    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "1"
    assert first.headers["x-ratelimit-remaining"] == "0"

    second = client.get("/api/weather", params={"city": "上海"})
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert second.headers["x-ratelimit-remaining"] == "0"

    blank = client.get("/api/weather", params={"city": "   "})
    assert blank.status_code == 422
    print("✓ /api/weather 按 IP 独立限流（空白城市不耗额度）")


def test_weather_endpoint_uses_dedicated_executor(monkeypatch):
    """天气请求必须真的跑在专用线程池里：慢上游不会阻塞事件循环。

    行为断言：记录 fetch_weather 执行时的线程，要求不在主线程（同步调用）
    且专用 executor 确实派生了线程（换成 asyncio.to_thread 共用默认池会失败）。
    """
    from webapp import server as server_module

    executed: list[int] = []

    def fake_fetch(city: str, timeout: float = 8.0) -> dict:
        executed.append(threading.get_ident())
        return {"city": city, "current": {}, "daily": [], "stale": False}

    monkeypatch.setattr(server_module, "fetch_weather", fake_fetch)
    client = TestClient(create_app(model=UsageTrackingModel(_FakeModel())))
    response = client.get("/api/weather", params={"city": "北京"})
    assert response.status_code == 200, response.text
    assert executed and executed[0] != threading.get_ident(), (
        "天气请求必须在工作线程执行（同步调用会阻塞事件循环）"
    )
    executor = server_module._get_weather_executor()
    assert executor._threads, "请求应落在专用线程池（而非共享默认池）"
    print("✓ 天气请求在专用线程池的工作线程执行")


def test_status_endpoint():
    response = _client().get("/api/status", params={"user_id": "web_test_missing"})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "web_test_missing"
    assert data["preferences"] == {}
    assert data["trip_count"] == 0
    assert "storage" in data
    assert data["usage"]["calls"] == 0
    assert data["usage"]["top_agents"] == []
    print("✓ /api/status 正常")


def test_status_travel_summary():
    """行程统计：总行程 / 出行天数（含首尾）/ 到访城市 / 进行中 / 即将出行。"""
    client = _client()
    user_id = _unique_user("web_test_travel_summary")
    try:
        trips = [
            ("北京", "上海", "2099-01-01", "2099-01-03"),  # 3 天
            ("北京", "杭州", "2099-02-01", "2099-02-01"),  # 1 天（当天往返）
        ]
        for origin, destination, start, end in trips:
            created = client.post(
                "/api/trips",
                json={
                    "user_id": user_id,
                    "origin": origin,
                    "destination": destination,
                    "start_date": start,
                    "end_date": end,
                    "purpose": "出差",
                },
            )
            assert created.status_code == 201, created.text

        summary = client.get(
            "/api/status", params={"user_id": user_id}
        ).json()["travel_summary"]
        assert summary == {
            "total_trips": 2,
            "total_days": 4,
            "cities": 2,
            "active": 0,
            "upcoming": 2,
        }, summary
        print("✓ /api/status 行程统计（天数含首尾 / 城市去重 / 即将出行）")
    finally:
        _cleanup(user_id)


def test_chat_returns_reply_and_usage():
    client = _client()
    response = client.post(
        "/api/chat",
        json={"message": "你好", "user_id": "web_test_user", "session_id": "sess-1"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["session_id"] == "sess-1"
    assert isinstance(data["reply"], str) and data["reply"]
    assert data["agents"] == []
    assert data["usage"]["calls"] >= 1
    assert data["usage"]["total_tokens"] >= 10
    assert data["elapsed"] >= 0
    print("✓ /api/chat 返回回复、用量与耗时")


def test_chat_creates_session_when_missing():
    response = _client().post("/api/chat", json={"message": "你好", "user_id": "web_test_user"})
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert isinstance(session_id, str) and len(session_id) == 12
    print("✓ 未提供会话时自动创建")


def test_chat_stream_events():
    client = _client()
    user_id = _unique_user("web_test_stream")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-stream"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-ratelimit-limit"] == "30"
            assert "x-ratelimit-remaining" in response.headers
            events = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))

        types = [event["type"] for event in events]
        assert types[0] == "session", "先推送会话 ID，停止 / 失败后仍可编辑"
        assert events[0]["session_id"] == "sess-stream"
        assert types[1] == "phase"
        assert "plan" in types
        assert types[-1] == "done"
        done = events[-1]
        assert done["session_id"] == "sess-stream"
        assert done["reply"]
        assert done["usage"]["calls"] >= 1
        state = client.app.state.aligo
        session = state.sessions[state._session_key(user_id, "sess-stream")]
        assert session.in_use() is False, "流结束后必须解除在用标记"
        print("✓ /api/chat/stream 推送进度事件并以 done 结束")
    finally:
        _cleanup(user_id)


def test_stream_forwards_agent_progress_events(monkeypatch):
    """orchestrator 发出的 agent_start / agent_done 必须原样转发到 SSE。"""
    from agents.orchestration_agent import OrchestrationAgent
    from agentscope.message import Msg as _Msg

    async def fake_reply(self, intention, on_event=None):
        if on_event:
            on_event({"type": "agent_start", "agent_name": "information_query"})
            on_event(
                {
                    "type": "agent_done",
                    "agent_name": "information_query",
                    "status": "success",
                    "duration": 0.1,
                }
            )
        return _Msg(
            name=self.name,
            content=json.dumps(
                {
                    "status": "completed",
                    "results": [
                        {
                            "agent_name": "information_query",
                            "priority": 1,
                            "status": "success",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            role="assistant",
        )

    monkeypatch.setattr(OrchestrationAgent, "reply", fake_reply)
    client = TestClient(create_app(model=UsageTrackingModel(_FakeModel())))
    user_id = _unique_user("web_test_agent_events")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "sess-agent-events",
            },
        ) as response:
            assert response.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        types = [event["type"] for event in events]
        assert "agent_start" in types, f"agent_start 未转发：{types}"
        assert "agent_done" in types, f"agent_done 未转发：{types}"
        done = events[-1]
        assert done["type"] == "done"
    finally:
        _cleanup(user_id)
    print("✓ SSE 转发 agent_start / agent_done 进度事件")


def test_stream_heartbeat():
    import webapp.server as server_module

    class _SlowModel:
        async def __call__(self, messages, **kwargs):
            await asyncio.sleep(0.15)
            return {
                "content": [{"type": "text", "text": json.dumps(_INTENT, ensure_ascii=False)}],
                "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.15},
            }

    original = server_module.SSE_HEARTBEAT_SEC
    server_module.SSE_HEARTBEAT_SEC = 0.05
    user_id = _unique_user("web_test_heartbeat")
    try:
        client = TestClient(create_app(model=UsageTrackingModel(_SlowModel())))
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-heartbeat"},
        ) as response:
            assert response.status_code == 200
            lines = list(response.iter_lines())

        assert any(line.startswith(": keep-alive") for line in lines), lines[:5]
        assert any(line.startswith("data:") for line in lines)
        print("✓ SSE 心跳保活")
    finally:
        server_module.SSE_HEARTBEAT_SEC = original
        _cleanup(user_id)


def test_chat_failure_returns_500():
    from config import RESILIENCE_CONFIG

    client = TestClient(create_app(model=UsageTrackingModel(_FailingModel())))
    original = dict(RESILIENCE_CONFIG)
    RESILIENCE_CONFIG.update(
        {"max_retries": 0, "retry_base_delay_sec": 0.01, "retry_max_delay_sec": 0.01}
    )
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": "web_test_fail", "session_id": "sess-fail"},
            headers={"X-Request-ID": "fail-test"},
        )
    finally:
        RESILIENCE_CONFIG.clear()
        RESILIENCE_CONFIG.update(original)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "服务器内部错误" in detail and "fail-test" in detail
    assert "connection reset" not in detail, "内部异常文本不应泄露给客户端"
    assert response.headers["x-request-id"] == "fail-test"
    print("✓ 对话失败返回 500 与请求 ID")


def test_knowledge_category_filter_accepts_long_values(monkeypatch):
    """分类筛选上限与写入一致：21..200 字的分类可以被筛选（此前 422）。"""
    from webapp import server as server_module

    calls = {}

    def fake_list(limit, offset, query, category):
        calls["category"] = category
        return {"total": 0, "items": []}

    monkeypatch.setattr(server_module, "list_documents", fake_list)
    long_category = "长分类" * 10  # 30 字，超过旧的 20 字上限
    response = _client().get(
        "/api/knowledge/documents", params={"category": long_category}
    )
    assert response.status_code == 200, response.text
    assert calls["category"] == long_category
    print("✓ 分类筛选接受与写入上限一致的 200 字分类")


def test_chat_stream_rejects_deleted_session_with_404(monkeypatch):
    """SSE 预检：会话已删除时在建立流之前返回 404，而不是断开的空流。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    monkeypatch.setattr(state, "is_recently_deleted", lambda user, sid: True)
    client = TestClient(app)
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好", "user_id": "u-del-stream", "session_id": "s-del"},
    )
    assert response.status_code == 404
    assert "已删除" in response.json()["detail"]
    print("✓ SSE 预检在首个事件前返回 404")


def test_chat_storage_failure_maps_503(monkeypatch):
    """会话构造遇到持久化错误时 /api/chat 与 /api/chat/stream 都返回 503。"""
    from utils.errors import MemoryPersistenceError

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo

    def boom(user, session, *args):
        raise MemoryPersistenceError("disk full")

    monkeypatch.setattr(state, "get_session", boom)
    client = TestClient(app)
    chat = client.post(
        "/api/chat",
        json={"message": "你好", "user_id": "u-503", "session_id": "s-503"},
    )
    assert chat.status_code == 503
    stream = client.post(
        "/api/chat/stream",
        json={"message": "你好", "user_id": "u-503", "session_id": "s-503"},
    )
    assert stream.status_code == 503
    assert "存储暂时不可用" in chat.json()["detail"]
    print("✓ 存储故障在对话与流式接口统一映射 503")


def test_chat_stream_failure_event():
    from config import RESILIENCE_CONFIG

    client = TestClient(create_app(model=UsageTrackingModel(_FailingModel())))
    original = dict(RESILIENCE_CONFIG)
    RESILIENCE_CONFIG.update(
        {"max_retries": 0, "retry_base_delay_sec": 0.01, "retry_max_delay_sec": 0.01}
    )
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": "web_test_fail_stream",
                "session_id": "sess-fail-stream",
            },
        ) as response:
            assert response.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
    finally:
        RESILIENCE_CONFIG.clear()
        RESILIENCE_CONFIG.update(original)

    assert events and events[-1]["type"] == "error"
    assert events[0]["type"] == "session", "失败时也应先收到会话 ID"
    assert events[0]["session_id"] == "sess-fail-stream"
    assert "处理失败" in events[-1]["message"]
    print("✓ 流式对话失败推送 error 事件（先推送会话 ID）")


def _stream_error_message(monkeypatch, exc):
    """注入一个必然失败的运行时，返回 SSE 最后的 error 事件文案。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo

    class _Runtime:
        async def ask(self, message, on_event=None):
            raise exc

    class _Session:
        session_id = "s-inject"
        runtime = _Runtime()

        def begin_turn(self):
            pass

        def end_turn(self):
            pass

        def in_use(self):
            return False

        def long_in_use(self, *_):
            return False

        def close(self):
            pass

    monkeypatch.setattr(
        state, "get_session", lambda user, sid, mark=False: _Session()
    )
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "message": "你好",
            "user_id": f"u-inject-{uuid.uuid4().hex[:8]}",
            "session_id": "s-inject",
        },
    ) as response:
        assert response.status_code == 200
        events = [
            json.loads(line[5:].strip())
            for line in response.iter_lines()
            if line.startswith("data:")
        ]
    assert events and events[-1]["type"] == "error"
    return events[-1]["message"]


def test_chat_stream_error_event_redacts_internal_details(monkeypatch):
    """未预期异常的原始文本可能含 SQL / 路径 / 上游响应，不能透给客户端。"""
    message = _stream_error_message(
        monkeypatch, RuntimeError("数据库密码=secret123 文件 /opt/data/full")
    )
    assert "secret123" not in message
    assert "/opt/data" not in message
    assert "服务器内部错误" in message and "请求 ID" in message
    print("✓ SSE 错误事件脱敏内部异常文本")


def test_chat_stream_user_facing_error_keeps_curated_message(monkeypatch):
    """UserFacingError 是给用户看的固定文案，应原样返回。"""
    from utils.errors import UserFacingError

    message = _stream_error_message(
        monkeypatch, UserFacingError("意图识别结果解析失败，请重试")
    )
    assert "意图识别结果解析失败" in message
    print("✓ SSE 返回 UserFacingError 的用户提示")


def test_chat_stream_storage_error_maps_friendly_message(monkeypatch):
    """存储故障按「存储暂时不可用」返回，不泄露内部细节。"""
    from utils.errors import MemoryPersistenceError

    message = _stream_error_message(
        monkeypatch, MemoryPersistenceError("disk /data/full")
    )
    assert "存储暂时不可用" in message
    assert "/data/full" not in message
    print("✓ SSE 存储故障返回友好提示")


def test_get_session_constructs_outside_lock(monkeypatch):
    """并发首请求各自在锁外构造；后到者关闭自己新建的运行时回收连接。"""
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 10

    barrier = threading.Barrier(2, timeout=10)
    created = []
    closed = []

    class _SlowSession:
        def __init__(self, user_id, session_id, model, breaker):
            created.append(session_id)
            # 两个线程都必须先完成第一次锁检查、进入构造
            barrier.wait()
            self.session_id = session_id
            self._turns = 0

        def begin_turn(self):
            self._turns += 1

        def end_turn(self):
            self._turns -= 1

        def in_use(self):
            return False

        def long_in_use(self, *_):
            return False

        def close(self):
            closed.append(self.session_id)

    monkeypatch.setattr(server_module, "_WebSession", _SlowSession)

    results = []

    def worker():
        results.append(state.get_session("u-conc", "s-conc", True))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 2, "两个线程都应在缓存锁外完成构造"
    assert len({id(item) for item in results}) == 1, "最终应返回同一实例"
    assert closed == ["s-conc"], "竞争失败的实例必须关闭"
    key = state._session_key("u-conc", "s-conc")
    assert state.sessions[key] is results[0]
    assert results[0]._turns == 2, "两个并发请求各自登记一次占用"
    print("✓ 会话构造移出缓存锁且竞争失败实例被关闭")


def test_destructive_endpoints_503_while_pg_cooling_down(monkeypatch):
    """PG 冷却期：删除类接口必须 503，不能静默作用在 JSON 回退库上。"""
    import time as time_module

    from config import STORAGE_CONFIG
    from context import storage as storage_module

    dsn = "postgresql://aligo:secret@127.0.0.1:1/aligo"
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", dsn)
    monkeypatch.setitem(
        storage_module._FAILED_UNTIL,
        f"postgres:{dsn}",
        time_module.monotonic() + 60,
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    user_id = _unique_user("web_pg_cool")

    response = client.delete(f"/api/sessions/s-cool?user_id={user_id}")
    assert response.status_code == 503, response.text
    assert "存储暂时不可用" in response.json()["detail"]
    # 删除失败：墓碑必须撤销，同一 id 仍可继续对话
    assert app.state.aligo.is_recently_deleted(user_id, "s-cool") is False

    response = client.delete(f"/api/sessions/s-cool/last-turn?user_id={user_id}")
    assert response.status_code == 503, response.text

    response = client.delete(f"/api/trips/trip_1?user_id={user_id}")
    assert response.status_code == 503, response.text

    response = client.delete(f"/api/preferences/home_location?user_id={user_id}")
    assert response.status_code == 503, response.text
    print("✓ PG 冷却期删除类接口统一 503 且不撤销墓碑")


def test_sessions_listing_and_delete():
    client = _client()
    user_id = _unique_user("web_test_hist")
    try:
        for message in ("你好", "还在吗"):
            response = client.post(
                "/api/chat",
                json={"message": message, "user_id": user_id, "session_id": "sess-hist"},
            )
            assert response.status_code == 200

        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        entry = next(item for item in sessions if item["session_id"] == "sess-hist")
        assert entry["message_count"] == 4
        assert entry["title"] == "你好"

        detail = client.get("/api/sessions/sess-hist", params={"user_id": user_id}).json()
        roles = [message["role"] for message in detail["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert detail["messages"][1]["content"], "助手消息应渲染为可读文本"
        meta = detail["messages"][1]["meta"]
        assert meta is not None, "助手消息应包含执行元数据"
        assert meta["usage"]["calls"] >= 1
        assert meta["elapsed"] >= 0

        deletion = client.delete("/api/sessions/sess-hist", params={"user_id": user_id}).json()
        assert deletion["deleted"] == 4
        remaining = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        assert remaining == []
        print("✓ 会话列表 / 明细（含执行元数据）/ 删除正常")
    finally:
        _cleanup(user_id)


def test_session_markdown_export():
    client = _client()
    user_id = _unique_user("web_test_export")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-md"},
        )
        assert response.status_code == 200

        export = client.get("/api/sessions/sess-md/export", params={"user_id": user_id})
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/markdown")
        assert "attachment" in export.headers.get("content-disposition", "")
        assert "# 你好" in export.text
        assert "## 用户" in export.text and "## Aligo" in export.text

        missing = client.get("/api/sessions/sess-none/export", params={"user_id": user_id})
        assert missing.status_code == 404
        print("✓ 会话导出为 Markdown（并可 404）")
    finally:
        _cleanup(user_id)


def test_export_filename_is_ascii():
    response = _client().get("/api/export", params={"user_id": "旅行者"})
    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "旅行者" not in disposition, "下载文件名应为 ASCII，避免响应头编码错误"
    print("✓ 中文用户 ID 的导出文件名安全")


def test_trip_whitespace_destination_rejected():
    """纯空白目的地通过 min_length，但会被去重跳过并产生重复记录。"""
    client = _client()
    user_id = _unique_user("web_test_blank_trip")
    try:
        response = client.post(
            "/api/trips",
            json={"user_id": user_id, "destination": "   ", "start_date": "2026-12-01"},
        )
        assert response.status_code == 422
        chat = client.post("/api/chat", json={"message": "   ", "user_id": user_id})
        assert chat.status_code == 422, "纯空白消息不应进入 LLM 调用"
        print("✓ 纯空白目的地 / 消息被拒绝")
    finally:
        _cleanup(user_id)


def test_preference_type_with_space_is_editable():
    """模型 / 面板写入的含空格偏好键也能编辑与删除。"""
    client = _client()
    user_id = _unique_user("web_test_pref_key")
    try:
        key = "酒店 偏好"
        saved = client.put(
            f"/api/preferences/{key}",
            json={"user_id": user_id, "value": "汉庭"},
        )
        assert saved.status_code == 200, saved.text
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"][key] == "汉庭"
        deleted = client.delete(
            f"/api/preferences/{key}", params={"user_id": user_id}
        ).json()
        assert deleted["deleted"] is True
        print("✓ 含空格的偏好键可编辑 / 删除")
    finally:
        _cleanup(user_id)


def test_session_search_snippet_strips_markdown():
    """搜索预览应剥离标题 / 列表 / 表格 / 引用等 Markdown 记号。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_snippet")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_message("user", "查杭州天气", "s-snip")
        store.add_chat_message(
            "assistant",
            "## ✈️ 行程规划\n\n- **杭州** 明日小雨\n\n"
            "| 日期 | 天气 |\n| --- | --- |\n| 11-06 | 小雨 |\n\n> 带伞",
            "s-snip",
        )
        close = getattr(store, "close", None)
        if callable(close):
            close()
        data = _client().get(
            "/api/sessions", params={"user_id": user_id, "q": "杭州"}
        ).json()
        snippet = data["sessions"][0]["snippet"]
        assert "杭州" in snippet
        for token in ("**", "##", "|", "> "):
            assert token not in snippet, (token, snippet)
        print("✓ 搜索片段剥离 Markdown 记号")
    finally:
        _cleanup(user_id)


def test_ambiguous_trip_id_operations_return_409():
    """历史数据里同一 trip_id 对应多条行程时，改 / 删 / 单条日历都不能猜行。"""
    user_id = _unique_user("web_test_dup_trip")
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    trips = [
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        },
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "杭州",
            "start_date": "2026-12-05",
            "end_date": "2026-12-06",
            "purpose": "出差",
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trip_history": trips}), encoding="utf-8")
    client = _client()
    try:
        updated = client.put(
            "/api/trips/trip_1",
            json={"user_id": user_id, "destination": "南京"},
        )
        assert updated.status_code == 409
        deleted = client.delete("/api/trips/trip_1", params={"user_id": user_id})
        assert deleted.status_code == 409
        calendar = client.get(
            "/api/trips/trip_1/calendar", params={"user_id": user_id}
        )
        assert calendar.status_code == 409

        data = json.loads(path.read_text(encoding="utf-8"))
        assert {trip["destination"] for trip in data["trip_history"]} == {
            "上海",
            "杭州",
        }, "歧义 id 上的操作不能改动数据"
        print("✓ 重复 trip_id 的改 / 删 / 单条日历返回 409")
    finally:
        _cleanup(user_id)


def test_trip_panel_accepts_long_fields_from_chat():
    """对话写入的长 purpose / origin 不应让面板编辑直接 422。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_long_trip")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        long_purpose = "参加华东区年度渠道合作伙伴大会并拜访重点客户" * 2
        trip_id = store.save_trip_history(
            {
                "origin": "北京市朝阳区望京SOHO塔1",
                "destination": "上海市浦东新区张江高科园区",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "purpose": long_purpose,
            }
        )
        close = getattr(store, "close", None)
        if callable(close):
            close()
        response = client.put(
            f"/api/trips/{trip_id}",
            json={"user_id": user_id, "purpose": long_purpose + "（更新）"},
        )
        assert response.status_code == 200, response.text
        print("✓ 面板可编辑对话写入的长字段行程")
    finally:
        _cleanup(user_id)


def test_trip_update_validation_and_errors(monkeypatch):
    """行程更新的校验与错误映射：400 / 422（多种）/ 404 / 409（冲突与歧义）/ 503。"""
    from webapp import server as server_module

    class _Store:
        def __init__(self, trips, update_error=None):
            self.trips = trips
            self.update_error = update_error

        def get_trip_history(self, limit=None):
            return list(self.trips)

        def update_trip(self, trip_id, fields):
            if self.update_error == "conflict":
                raise ValueError("与已有行程冲突")
            if self.update_error == "boom":
                raise RuntimeError("disk full")
            return True

        def close(self):
            pass

    def _trip(destination="上海"):
        return {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": destination,
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "出差",
        }

    client = _client()
    valid = _Store([_trip()])
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: valid)

    response = client.put("/api/trips/trip_1", json={"user_id": "u-trip-v"})
    assert response.status_code == 400
    assert "没有需要更新的字段" in response.json()["detail"]

    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "destination": ""}
    )
    assert response.status_code == 422

    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "start_date": None}
    )
    assert response.status_code == 422
    assert "出发日期不能为空" in response.json()["detail"]

    # 格式不符合 YYYY-MM-DD 由模型校验挡下
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "start_date": "2026/12/01"}
    )
    assert response.status_code == 422

    # 格式正确但日期不存在（2026-13-01）
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "start_date": "2026-13-01"}
    )
    assert response.status_code == 422
    assert "YYYY-MM-DD" in response.json()["detail"]

    # 结束日期早于出发日期
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "end_date": "2026-11-01"}
    )
    assert response.status_code == 422
    assert "不能早于" in response.json()["detail"]

    # 旧行程没有出发日期时不能单独设置返程
    dateless = _Store([{"trip_id": "trip_1", "origin": "北京", "destination": "上海"}])
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: dateless)
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "end_date": "2026-12-05"}
    )
    assert response.status_code == 422
    assert "先设置出发日期" in response.json()["detail"]

    # 行程不存在
    empty = _Store([])
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: empty)
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "purpose": "会议"}
    )
    assert response.status_code == 404

    # trip_id 歧义（历史数据异常）
    duplicate = _Store([_trip(), _trip("杭州")])
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: duplicate)
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "purpose": "会议"}
    )
    assert response.status_code == 409
    assert "多条行程" in response.json()["detail"]

    # update_trip 抛 ValueError → 409；其它异常 → 503
    conflict = _Store([_trip()], update_error="conflict")
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: conflict)
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "purpose": "会议"}
    )
    assert response.status_code == 409
    assert "冲突" in response.json()["detail"]

    broken = _Store([_trip()], update_error="boom")
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: broken)
    response = client.put(
        "/api/trips/trip_1", json={"user_id": "u-trip-v", "purpose": "会议"}
    )
    assert response.status_code == 503
    assert "更新行程失败" in response.json()["detail"]
    print("✓ 行程更新校验与错误映射（400/422/404/409/503）")


def test_preference_panel_accepts_long_value():
    """面板偏好值上限应与对话写入路径一致，而不是 200 字。"""
    client = _client()
    user_id = _unique_user("web_test_long_pref")
    try:
        response = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "备注：" + "x" * 300},
        )
        assert response.status_code == 200, response.text
        print("✓ 面板接受 200 字以上的偏好值")
    finally:
        _cleanup(user_id)


def test_last_turn_expect_accepts_long_message():
    """CLI 写入的超长消息也应能通过 Web「编辑重发」删除（并真的删掉）。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_long_expect")
    long_text = "长" * 5000
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", long_text, "s-long")
            store.add_chat_message("assistant", "收到", "s-long")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        mismatch = client.delete(
            "/api/sessions/s-long/last-turn",
            params={"user_id": user_id, "expect": "不匹配的提问"},
        )
        assert mismatch.status_code == 200
        assert mismatch.json()["removed"] == 0 and mismatch.json()["mismatch"] is True

        response = client.delete(
            "/api/sessions/s-long/last-turn",
            params={"user_id": user_id, "expect": long_text},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["removed"] >= 1, "超长 expect 必须能匹配并删除最后一轮"
        assert payload["mismatch"] is False

        detail = client.get(
            "/api/sessions/s-long", params={"user_id": user_id}
        ).json()
        assert all(
            message["content"] != long_text for message in detail["messages"]
        ), "被删除的超长提问不应再出现在历史里"
        print("✓ 编辑重发能删除 4000 字以上的历史消息")
    finally:
        _cleanup(user_id)


def test_ics_handles_overflow_end_date():
    """9999-12-31 的行程不能让日历导出 500（DTEND 无法再加一天）。"""
    client = _client()
    user_id = _unique_user("web_test_ics_max")
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "9999-12-31",
                "end_date": "9999-12-31",
            },
        )
        assert created.status_code == 201
        trip_id = created.json()["trip_id"]
        single = client.get(
            f"/api/trips/{trip_id}/calendar", params={"user_id": user_id}
        )
        assert single.status_code == 200 and "BEGIN:VCALENDAR" in single.text
        all_cal = client.get("/api/trips/calendar", params={"user_id": user_id})
        assert all_cal.status_code == 200
        print("✓ 9999-12-31 行程的日历导出不再 500")
    finally:
        _cleanup(user_id)


def test_export_all_sessions_api():
    """GET /api/sessions/export 导出全部会话（含归档），静态路由不被动态路由吞掉。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_export_all")
    empty_user = _unique_user("web_test_export_empty")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "第一问", "s-a")
            store.add_chat_message("assistant", "回答一", "s-a")
            store.add_chat_message("user", "第二问", "s-b")
            store.set_session_title("s-b", "杭州行程")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        exported = client.get("/api/sessions/export", params={"user_id": user_id})
        assert exported.status_code == 200
        disposition = exported.headers.get("content-disposition", "")
        assert "attachment" in disposition and "aligo_sessions_" in disposition
        assert exported.text.startswith("# Aligo 会话导出")
        assert "杭州行程" in exported.text and "第一问" in exported.text
        assert "### 用户" in exported.text and "### Aligo" in exported.text

        # 静态路由优先于 /api/sessions/{session_id}
        detail = client.get("/api/sessions/s-a", params={"user_id": user_id})
        assert detail.status_code == 200 and detail.json()["messages"]

        missing = client.get("/api/sessions/export", params={"user_id": empty_user})
        assert missing.status_code == 404
        print("✓ /api/sessions/export 导出全部会话")
    finally:
        _cleanup(user_id)
        _cleanup(empty_user)


def test_export_all_sessions_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def get_chat_history(self, *args, **kwargs):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    response = _client().get(
        "/api/sessions/export", params={"user_id": "u-export-fail"}
    )
    assert response.status_code == 503
    print("✓ 全部会话导出读取失败映射 503")


def test_open_long_term_failure_maps_503(monkeypatch):
    """长期记忆打开失败（文件损坏 / 依赖缺失等）应返回可重试的 503 而不是 500。

    注意 PostgreSQL 不可达时存储工厂会回退本地 JSON（既有降级策略），
    不经过这里的异常分支。
    """
    from webapp import server as server_module

    def _boom(*args, **kwargs):
        raise ConnectionError("storage down")

    monkeypatch.setattr(server_module, "create_long_term_memory", _boom)
    client = _client()
    for path in (
        "/api/sessions",
        "/api/sessions/export",
        "/api/sessions/s-open-fail",
        "/api/sessions/s-open-fail/export",
    ):
        response = client.get(path, params={"user_id": "u-open-fail"})
        assert response.status_code == 503, (path, response.status_code)
    print("✓ 长期记忆打开失败映射 503")


def test_session_lru_eviction():
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 2
    for session_id in ("s-a", "s-b", "s-c"):
        state.get_session("web_test_lru", session_id)
    assert list(state.sessions.keys()) == [
        "web_test_lru\ns-b",
        "web_test_lru\ns-c",
    ]
    state.close_all()
    print("✓ 会话缓存按 LRU 淘汰并释放")


def test_session_lru_closes_evicted_session():
    """被淘汰的会话必须 close()，否则 PostgreSQL 连接会随会话数泄漏。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 1
    first = state.get_session("user-a", "s-a")
    closed: list[bool] = []
    setattr(first, "close", lambda: closed.append(True))
    state.get_session("user-a", "s-b")
    assert closed, "被淘汰的会话必须 close()（释放 PG 连接）"
    state.close_all()
    print("✓ LRU 淘汰时关闭会话（释放连接）")


def test_session_cache_is_thread_safe():
    """会话缓存会被线程池（drop_session）与事件循环（get_session）并发修改。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    # 小上限 + 共享键：让 LRU 淘汰在并发下真正执行（此前峰值 30 < 50，
    # 淘汰分支从未跑过，删掉锁也照样通过）
    state.max_sessions = 10
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for index in range(300):
                state.get_session(f"u{n % 2}", f"shared-{index % 8}")
                state.get_session(f"u{n}", f"s{n}-{index % 30}")
                if index % 4 == 0:
                    state.drop_session(f"u{n}", f"s{n}-{index % 30}")
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors[:1]
    assert len(state.sessions) <= state.max_sessions, (
        f"缓存不能超上限，实际 {len(state.sessions)}"
    )
    # 共享键必须始终可用（并发淘汰不能把仍在用的键丢掉后返回 None）
    assert state.get_session("u0", "shared-0") is not None
    state.close_all()
    print("✓ 会话缓存并发安全（小上限触发 LRU 淘汰）")


def test_streaming_releases_user_lock():
    """SSE 流结束后必须释放用户锁，否则同一用户后续请求全部卡死。"""
    import json as json_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    state = app.state.aligo
    user_id = _unique_user("web_test_stream_lock")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "stream-lock"},
        ) as response:
            assert response.status_code == 200
            events = [
                json_module.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]
        assert events and events[0]["type"] == "session"
        assert events[-1]["type"] in ("done", "error")
        assert state.locks[user_id].locked() is False, "流结束后必须释放用户锁"
        deleted = client.delete(
            "/api/sessions/stream-lock", params={"user_id": user_id}
        )
        assert deleted.status_code == 200, "锁未释放时删除会卡住"
        print("✓ SSE 流结束后释放用户锁")
    finally:
        _cleanup(user_id)


def test_delete_last_turn_clears_cached_short_term():
    """缓存的进程内会话也必须清掉被删除的一轮（否则下一轮把旧问答再发给模型）。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    state = app.state.aligo
    user_id = _unique_user("web_test_last_turn_cache")
    try:
        first = client.post(
            "/api/chat",
            json={"message": "第一问", "user_id": user_id, "session_id": "lt-cache"},
        )
        assert first.status_code == 200
        session = state.sessions[state._session_key(user_id, "lt-cache")]
        short_term = session.runtime.memory.short_term
        before = short_term.get_statistics()["total_messages"]
        assert before >= 2

        removed = client.delete(
            "/api/sessions/lt-cache/last-turn",
            params={"user_id": user_id, "expect": "第一问"},
        )
        assert removed.status_code == 200 and removed.json()["removed"] == 2
        assert short_term.get_statistics()["total_messages"] == before - 2

        again = client.delete(
            "/api/sessions/lt-cache/last-turn",
            params={"user_id": user_id, "expect": "第一问"},
        )
        assert again.json()["mismatch"] is True
        print("✓ 删除最后一轮同步清理缓存的进程内短期记忆")
    finally:
        _cleanup(user_id)


def test_session_cache_is_user_scoped():
    """同一 session_id 下不同用户各持独立运行时，drop 不影响他人。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    session_a = state.get_session("user-a", "shared")
    session_b = state.get_session("user-b", "shared")
    assert session_a is not session_b
    state.drop_session("user-b", "shared")
    assert state.sessions.get(state._session_key("user-a", "shared")) is session_a
    assert state.sessions.get(state._session_key("user-b", "shared")) is None
    state.close_all()
    print("✓ 会话缓存按用户隔离（drop 不影响同名会话）")


def test_session_search():
    client = _client()
    user_id = _unique_user("web_test_search")
    try:
        for message, session_id in (
            ("杭州的住宿标准是多少", "sess-search"),
            ("北京天气怎么样", "sess-other"),
        ):
            response = client.post(
                "/api/chat",
                json={"message": message, "user_id": user_id, "session_id": session_id},
            )
            assert response.status_code == 200

        results = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "杭州"}
        ).json()["sessions"]
        assert [item["session_id"] for item in results] == ["sess-search"]
        assert "杭州" in results[0]["snippet"]
        assert results[0]["match_index"] == 0, "命中用户消息时返回会话内序号 0"

        assistant_hits = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "已记录"}
        ).json()["sessions"]
        assert len(assistant_hits) == 2, "助手消息按渲染后的文本参与搜索"
        assert all(
            item["match_index"] == 1 for item in assistant_hits
        ), "命中助手消息时返回会话内序号 1"

        empty = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "不存在的关键词"}
        ).json()["sessions"]
        assert empty == []
        print("✓ 会话全文搜索与命中片段")
    finally:
        _cleanup(user_id)


def test_status_usage_totals():
    client = _client()
    user_id = _unique_user("web_test_usage")
    try:
        for _ in range(2):
            response = client.post(
                "/api/chat",
                json={"message": "你好", "user_id": user_id, "session_id": "sess-usage"},
            )
            assert response.status_code == 200

        usage = client.get("/api/status", params={"user_id": user_id}).json()["usage"]
        assert usage["calls"] >= 2
        assert usage["total_tokens"] >= 20
        assert usage["avg_elapsed"] >= 0
        assert isinstance(usage["top_agents"], list)
        by_day = usage["by_day"]
        assert len(by_day) == 7
        assert by_day[-1]["calls"] >= 2
        assert by_day[-1]["tokens"] == usage["total_tokens"]
        assert all("date" in day for day in by_day)
        print("✓ /api/status 汇总模型用量（含近 7 天）")
    finally:
        _cleanup(user_id)


def test_preference_rejects_non_finite():
    """NaN / Infinity 不能写入偏好，否则会让 /api/status JSON 永久失败。"""
    client = _client()
    for raw in (
        '{"user_id": "web_test_nan", "value": NaN}',
        '{"user_id": "web_test_nan", "value": Infinity}',
    ):
        response = client.put(
            "/api/preferences/home_location",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, raw
    print("✓ 偏好拒绝 NaN / Infinity")


def test_preferences_api():
    client = _client()
    user_id = _unique_user("web_test_pref")
    try:
        response = client.put(
            "/api/preferences/hotel_brands",
            json={"user_id": user_id, "value": ["汉庭", "如家"]},
        )
        assert response.status_code == 200
        assert response.json()["value"] == ["汉庭", "如家"]

        response = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "北京"},
        )
        assert response.status_code == 200

        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {
            "hotel_brands": ["汉庭", "如家"],
            "home_location": "北京",
        }

        deleted = client.delete(
            "/api/preferences/hotel_brands", params={"user_id": user_id}
        ).json()
        assert deleted["deleted"] is True
        remaining = client.get("/api/status", params={"user_id": user_id}).json()
        assert "hotel_brands" not in remaining["preferences"]

        again = client.delete(
            "/api/preferences/hotel_brands", params={"user_id": user_id}
        ).json()
        assert again["deleted"] is False

        zero = client.put(
            "/api/preferences/budget_level",
            json={"user_id": user_id, "value": 0},
        )
        assert zero.status_code == 200
        flag = client.put(
            "/api/preferences/eco_mode",
            json={"user_id": user_id, "value": False},
        )
        assert flag.status_code == 200
        updated = client.get("/api/status", params={"user_id": user_id}).json()
        assert updated["preferences"]["budget_level"] == 0
        assert updated["preferences"]["eco_mode"] is False

        invalid = client.put(
            "/api/preferences/" + "x" * 65, json={"user_id": user_id, "value": "x"}
        )
        assert invalid.status_code == 422
        too_long = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "x" * 501},
        )
        assert too_long.status_code == 422
        print("✓ 偏好新增 / 覆盖 / 删除 / 参数校验")
    finally:
        _cleanup(user_id)


def test_preference_storage_failure_maps_503(monkeypatch):
    """偏好写盘失败应返回 503（与其它存储接口一致），而不是通用 500。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    class _BrokenStore:
        def save_preference(self, pref_type, value):
            raise MemoryPersistenceError("磁盘已满")

        def delete_preference(self, pref_type):
            raise MemoryPersistenceError("磁盘已满")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    client = _client()
    saved = client.put(
        "/api/preferences/home_location", json={"user_id": "pref-fail", "value": "北京"}
    )
    assert saved.status_code == 503
    assert "保存失败" in saved.json()["detail"]
    deleted = client.delete(
        "/api/preferences/home_location", params={"user_id": "pref-fail"}
    )
    assert deleted.status_code == 503
    assert "删除失败" in deleted.json()["detail"]
    print("✓ 偏好写入 / 删除失败映射 503")


def test_status_trips():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trip")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-11-09",
                    "end_date": "2026-11-11",
                    "purpose": "出差",
                    "session_id": "sess-trip-link",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        status = _client().get("/api/status", params={"user_id": user_id}).json()
        assert status["trip_count"] == 1
        assert status["trips"][0]["destination"] == "上海"
        assert status["trips"][0]["start_date"] == "2026-11-09"
        assert status["trips"][0]["session_id"] == "sess-trip-link"
        print("✓ /api/status 返回最近行程")
    finally:
        _cleanup(user_id)


def test_list_trips_api():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_list_trips")
    specs = [
        ("上海", "2026-12-01", "2026-12-02"),
        ("杭州", "2026-12-03", "2026-12-06"),
        ("南京", "2026-12-04", "2026-12-07"),
        ("成都", "2026-12-08", "2026-12-09"),
        ("西安", "2026-12-10", "2026-12-11"),
        ("武汉", "2026-12-12", "2026-12-13"),
        ("重庆", "2026-12-14", "2026-12-15"),
    ]
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for destination, start, end in specs:
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": start,
                        "end_date": end,
                        "purpose": "出差",
                    }
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        page1 = client.get(
            "/api/trips", params={"user_id": user_id, "limit": 3, "offset": 0}
        ).json()
        assert page1["total"] == 7
        assert [item["destination"] for item in page1["trips"]] == ["上海", "杭州", "南京"]
        by_destination = {item["destination"]: item for item in page1["trips"]}
        assert [item["destination"] for item in by_destination["杭州"]["conflicts"]] == [
            "南京"
        ]
        assert [item["destination"] for item in by_destination["南京"]["conflicts"]] == [
            "杭州"
        ]

        page2 = client.get(
            "/api/trips", params={"user_id": user_id, "limit": 3, "offset": 3}
        ).json()
        assert [item["destination"] for item in page2["trips"]] == ["成都", "西安", "武汉"]
        page3 = client.get(
            "/api/trips", params={"user_id": user_id, "limit": 3, "offset": 6}
        ).json()
        assert [item["destination"] for item in page3["trips"]] == ["重庆"]

        latest = client.get(
            "/api/trips", params={"user_id": user_id, "limit": 3, "order": "desc"}
        ).json()
        assert [item["destination"] for item in latest["trips"]] == [
            "重庆",
            "武汉",
            "西安",
        ]
        assert latest["total"] == 7
        assert (
            client.get(
                "/api/trips", params={"user_id": user_id, "order": "sideways"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/trips", params={"user_id": user_id, "limit": 0}
            ).status_code
            == 422
        )
        print("✓ /api/trips 列表（分页 / 倒序 / 重叠信息 / 参数校验）")
    finally:
        _cleanup(user_id)


def test_delete_trip_api():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trip_del")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for destination in ("上海", "杭州"):
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": "2026-11-09",
                        "end_date": "2026-11-11",
                        "purpose": "出差",
                    }
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        deleted = client.delete(
            "/api/trips/trip_1", params={"user_id": user_id}
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        again = client.delete("/api/trips/trip_1", params={"user_id": user_id})
        assert again.status_code == 404

        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["trip_count"] == 1
        assert status["trips"][0]["destination"] == "杭州"
        print("✓ /api/trips/{id} 删除行程（404 / 统计同步）")
    finally:
        _cleanup(user_id)


def test_delete_trip_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def delete_trip(self, trip_id):
            raise OSError("disk full")

        def close(self):
            pass

    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore())
    response = _client().delete(
        "/api/trips/trip_1", params={"user_id": "web_test_trip_broken"}
    )
    assert response.status_code == 503
    print("✓ 行程删除写入失败返回 503（不伪装成成功）")


def test_trip_ics_uid_includes_content():
    from utils.trip_export import render_trip_ics

    base = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-11-09",
        "end_date": "2026-11-11",
        "purpose": "出差",
    }
    other = {**base, "destination": "杭州"}

    def uid(trip):
        return next(
            line for line in render_trip_ics(trip, "u1").splitlines() if line.startswith("UID:")
        )

    assert uid(base) != uid(other), "复用 trip_id 的新行程不应覆盖旧日历事件"
    assert uid(base) == uid(base), "同一行程的 UID 应保持稳定"
    print("✓ ICS UID 包含行程内容（复用 id 不覆盖旧事件）")


def test_ics_escape_handles_lone_cr():
    from utils.trip_export import _ics_escape, render_trip_ics

    assert _ics_escape("a\rb") == "a\\nb"
    trip = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "上海\r恶意行",
        "start_date": "2026-11-09",
        "end_date": "2026-11-11",
        "purpose": "出差",
    }
    content = render_trip_ics(trip, "u1")
    assert "上海\\n恶意行" in content
    assert "\r恶意行" not in content
    print("✓ ICS 转义孤立 CR（不破坏行结构）")


def test_build_trips_ics_multiple_events():
    from utils.trip_export import render_trips_ics

    trips = [
        {
            "trip_id": "trip_1",
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-11-09",
            "end_date": "2026-11-11",
            "purpose": "出差",
        },
        {
            "trip_id": "trip_2",
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "purpose": "出差",
        },
    ]
    content = render_trips_ics(trips, "u1")
    assert content.count("BEGIN:VCALENDAR") == 1
    assert content.count("BEGIN:VEVENT") == 2
    assert content.count("END:VEVENT") == 2
    assert content.count("BEGIN:VALARM") == 2
    assert content.count("END:VALARM") == 2
    assert "TRIGGER:-PT15H" in content
    assert "出行提醒" in content
    # VALARM 必须位于同一 VEVENT 的 DTEND 之后、END:VEVENT 之前
    for event in content.split("BEGIN:VEVENT")[1:]:
        body = event.split("END:VEVENT")[0]
        assert "BEGIN:VALARM" in body
        assert body.index("DTEND") < body.index("BEGIN:VALARM")
    assert content.endswith("END:VCALENDAR\r\n")

    # 缺少出发日期的行程被跳过
    content = render_trips_ics(trips + [{"trip_id": "trip_3", "destination": "杭州"}], "u1")
    assert content.count("BEGIN:VEVENT") == 2

    empty = render_trips_ics([], "u1")
    assert "BEGIN:VCALENDAR" in empty and "BEGIN:VEVENT" not in empty
    print("✓ 全部行程合并为一个日历（跳过缺日期行程）")


def test_trips_calendar_api():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_all_trips")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for day, destination in ((9, "上海"), (12, "杭州")):
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": f"2026-11-{day:02d}",
                        "end_date": f"2026-11-{day + 1:02d}",
                        "purpose": "出差",
                    }
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        response = _client().get("/api/trips/calendar", params={"user_id": user_id})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.count("BEGIN:VEVENT") == 2

        empty = _client().get(
            "/api/trips/calendar", params={"user_id": _unique_user("web_test_no_trips")}
        )
        assert empty.status_code == 200
        assert "BEGIN:VEVENT" not in empty.text
        print("✓ /api/trips/calendar 导出全部行程")
    finally:
        _cleanup(user_id)


def test_trip_calendar_storage_failure_maps_503(monkeypatch):
    """行程日历导出的存储故障应映射 503（与 CSV / 会话导出一致）。"""
    from webapp import server as server_module

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            raise RuntimeError("disk down")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    client = _client()
    for path in ("/api/trips/calendar", "/api/trips/trip_1/calendar"):
        response = client.get(path, params={"user_id": "u-ics"})
        assert response.status_code == 503, path
    print("✓ 行程日历导出存储失败 → 503")


def test_create_trip_api():
    user_id = _unique_user("web_test_add_trip")
    client = _client()
    payload = {
        "user_id": user_id,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-11-20",
        "end_date": "2026-11-22",
        "purpose": "出差",
    }
    try:
        created = client.post("/api/trips", json=payload)
        assert created.status_code == 201
        assert created.json()["trip_id"] == "trip_1"
        assert created.json()["created"] is True

        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["trip_count"] == 1
        assert status["trips"][0]["destination"] == "上海"
        assert status["trips"][0]["purpose"] == "出差"

        # 同路线 + 同起止日期重复添加：去重并返回同一 trip_id
        again = client.post("/api/trips", json=payload)
        assert again.status_code == 201
        assert again.json()["trip_id"] == "trip_1"
        assert again.json()["created"] is False
        assert (
            client.get("/api/status", params={"user_id": user_id}).json()["trip_count"]
            == 1
        )

        # 结束日期早于出发日期 / 非法日期 / 缺少目的地
        bad_range = client.post(
            "/api/trips",
            json={**payload, "start_date": "2026-11-22", "end_date": "2026-11-20"},
        )
        assert bad_range.status_code == 422
        bad_date = client.post(
            "/api/trips", json={**payload, "start_date": "2026-02-30"}
        )
        assert bad_date.status_code == 422
        missing = client.post("/api/trips", json={**payload, "destination": ""})
        assert missing.status_code == 422
        print("✓ /api/trips 手动添加行程（去重 / 日期校验）")
    finally:
        _cleanup(user_id)


def test_update_trip_api():
    user_id = _unique_user("web_test_update_trip")
    client = _client()
    try:
        created = client.post(
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
        trip_id = created.json()["trip_id"]

        updated = client.put(
            f"/api/trips/{trip_id}", json={"user_id": user_id, "destination": "南京"}
        )
        assert updated.status_code == 200
        assert updated.json()["trip"]["destination"] == "南京"
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["trips"][0]["destination"] == "南京"
        assert status["statistics"]["frequent_destinations"] == {"南京": 1}

        # 没有提供字段 → 400
        assert (
            client.put(f"/api/trips/{trip_id}", json={"user_id": user_id}).status_code
            == 400
        )
        # 结束早于出发 / 非法日期 → 422
        assert (
            client.put(
                f"/api/trips/{trip_id}",
                json={"user_id": user_id, "end_date": "2026-11-19"},
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/trips/{trip_id}",
                json={"user_id": user_id, "start_date": "2026-02-30"},
            ).status_code
            == 422
        )
        # 不存在 → 404
        assert (
            client.put(
                "/api/trips/trip_99", json={"user_id": user_id, "purpose": "x"}
            ).status_code
            == 404
        )

        # 与另一条行程重复 → 409
        client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
            },
        )
        conflict = client.put(
            f"/api/trips/{trip_id}",
            json={
                "user_id": user_id,
                "destination": "杭州",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
            },
        )
        assert conflict.status_code == 409

        # 显式传 null 清空返程日期
        cleared = client.put(
            f"/api/trips/{trip_id}", json={"user_id": user_id, "end_date": None}
        )
        assert cleared.status_code == 200
        assert cleared.json()["trip"]["end_date"] is None
        print("✓ /api/trips/{id} 更新行程（部分更新 / 校验 / 冲突 / 清空日期）")
    finally:
        _cleanup(user_id)


def test_trip_overlap_conflicts_api():
    user_id = _unique_user("web_test_trip_overlap")
    client = _client()
    try:
        first = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-07",
            },
        )
        assert first.status_code == 201
        assert first.json()["conflicts"] == []
        first_id = first.json()["trip_id"]

        overlapping = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-06",
                "end_date": "2026-12-08",
            },
        )
        assert overlapping.status_code == 201
        conflicts = overlapping.json()["conflicts"]
        assert [item["trip_id"] for item in conflicts] == [first_id]
        assert conflicts[0]["destination"] == "上海"
        second_id = overlapping.json()["trip_id"]

        # 紧邻（12-09 出发）不算重叠
        adjacent = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "南京",
                "start_date": "2026-12-09",
                "end_date": "2026-12-10",
            },
        )
        assert adjacent.json()["conflicts"] == []

        # 单日行程（无返程日期）与包含该日期的区间重叠
        single = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "成都",
                "start_date": "2026-12-07",
            },
        )
        assert single.status_code == 201
        single_conflicts = {item["destination"] for item in single.json()["conflicts"]}
        assert single_conflicts == {"上海", "杭州"}

        # 修改行程后重新计算重叠（12-10 与南京行程的末日重叠）
        updated = client.put(
            f"/api/trips/{second_id}",
            json={"user_id": user_id, "start_date": "2026-12-10", "end_date": "2026-12-12"},
        )
        assert updated.status_code == 200
        assert [item["destination"] for item in updated.json()["conflicts"]] == ["南京"]

        # 去重保存自身不算冲突
        again = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-07",
            },
        )
        assert again.json()["created"] is False
        assert first_id not in {item["trip_id"] for item in again.json()["conflicts"]}
        assert [item["destination"] for item in again.json()["conflicts"]] == ["成都"]
        print("✓ 行程日期重叠检测（新增 / 单日 / 修改 / 去重排除自身）")
    finally:
        _cleanup(user_id)


def test_trip_conflicts_helper_edges():
    from webapp.server import _trip_conflicts

    trips = [
        {"trip_id": "trip_1", "start_date": "2026-12-05", "end_date": "2026-12-07"},
        {"trip_id": "trip_2", "start_date": "bad-date", "end_date": "2026-12-06"},
        {"trip_id": "trip_3", "start_date": "2026-12-07", "end_date": "2026-12-01"},
    ]
    start = datetime.strptime("2026-12-07", "%Y-%m-%d")
    found = _trip_conflicts(trips, start, start)
    # 非法出发日期跳过；结束早于出发的脏数据按单日处理
    assert [item["trip_id"] for item in found] == ["trip_1", "trip_3"]
    assert _trip_conflicts(trips, start, start, exclude_id="trip_1") == [
        item for item in found if item["trip_id"] != "trip_1"
    ]
    print("✓ 行程重叠辅助函数（脏数据 / 单日 / 排除自身）")


def test_delete_last_turn_api():
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_last_turn")
    session_id = "edit-session-1"
    other_session = "edit-session-2"

    def seed(store):
        for role, content in [
            ("user", "问题 1"),
            ("assistant", "回答 1"),
            ("user", "问题 2"),
            ("assistant", "回答 2"),
        ]:
            store.add_chat_message(role, content, session_id)
        store.add_chat_message("user", "其它会话", other_session)

    def open_store():
        return create_long_term_memory(user_id, str(MEMORY_DIR))

    def close(store):
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()

    try:
        store = open_store()
        try:
            seed(store)
        finally:
            close(store)

        # 内容不匹配：说明这条提问没有写入历史，不能删除上一轮
        mismatch = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "问题 3"},
        )
        assert mismatch.status_code == 200
        assert mismatch.json()["removed"] == 0
        assert mismatch.json()["mismatch"] is True

        # 其它用户不能删除该会话（此时会话仍有 4 条消息）
        stranger = _unique_user("web_test_last_turn_x")
        try:
            denied = client.delete(
                f"/api/sessions/{session_id}/last-turn",
                params={"user_id": stranger, "expect": "问题 2"},
            )
            assert denied.status_code == 200
            assert denied.json()["removed"] == 0
            store = open_store()
            try:
                assert len(store.get_chat_history(session_id=session_id)) == 4
            finally:
                close(store)
        finally:
            _cleanup(stranger)

        # 内容匹配：成对删除最后一轮
        response = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "问题 2"},
        )
        assert response.status_code == 200
        assert response.json()["removed"] == 2

        store = open_store()
        try:
            history = store.get_chat_history(session_id=session_id)
            assert [item["content"] for item in history] == ["问题 1", "回答 1"]
            other = store.get_chat_history(session_id=other_session)
            assert [item["content"] for item in other] == ["其它会话"]
            assert store.get_statistics()["total_messages"] == 3
            store.add_chat_message("user", "只有提问没有回答", session_id)
        finally:
            close(store)

        # 回答中断、只剩提问时只删除提问
        single = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "只有提问没有回答"},
        )
        assert single.json()["removed"] == 1

        # 再删一次移除剩余的一轮，之后没有可删除的消息（幂等返回 0）
        pair = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "问题 1"},
        )
        assert pair.json()["removed"] == 2
        empty = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "问题 1"},
        )
        assert empty.status_code == 200
        assert empty.json()["removed"] == 0
        print("✓ 删除最后一轮对话（内容校验 / 成对删除 / 仅提问 / 幂等 / 会话隔离）")
    finally:
        _cleanup(user_id)


def test_next_trip_prefers_ongoing():
    from datetime import datetime, timedelta

    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_next_ongoing")
    try:
        today = datetime.now().date()
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "深圳",
                    "start_date": (today - timedelta(days=1)).isoformat(),
                    "end_date": (today + timedelta(days=1)).isoformat(),
                    "session_id": "sess-ongoing",
                }
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": (today + timedelta(days=10)).isoformat(),
                }
            )
            # 非 ISO 结束日期不能按字符串比较成「进行中」，按单日处理
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "广州",
                    "start_date": (today - timedelta(days=5)).isoformat(),
                    "end_date": "2026/12/31",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        status = _client().get("/api/status", params={"user_id": user_id}).json()
        assert status["next_trip"]["session_id"] == "sess-ongoing"
        assert status["next_trip"]["destination"] == "深圳"

        # 进行中的行程结束后回落到未来行程
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            trips = store.get_trip_history(limit=None)
            ongoing = next(t for t in trips if t.get("session_id") == "sess-ongoing")
            store.update_trip(
                str(ongoing["trip_id"]),
                {
                    "start_date": (today - timedelta(days=3)).isoformat(),
                    "end_date": (today - timedelta(days=2)).isoformat(),
                },
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        status = _client().get("/api/status", params={"user_id": user_id}).json()
        assert status["next_trip"]["destination"] == "杭州"
        print("✓ next_trip 优先进行中行程，结束后回落到未来行程")
    finally:
        _cleanup(user_id)


def test_trips_query_filter_api():
    """GET /api/trips?q=：与面板 / CLI 同一口径过滤，冲突按完整历史计算。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trip_query")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for destination, start, end, purpose in [
                ("上海", "2026-12-01", "2026-12-03", "出差"),
                ("杭州", "2026-12-05", "2026-12-06", "会议"),
                ("上海", "2026-12-02", "2026-12-02", "培训"),
            ]:
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": start,
                        "end_date": end,
                        "purpose": purpose,
                    }
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        filtered = client.get(
            "/api/trips", params={"user_id": user_id, "q": "上海"}
        ).json()
        assert filtered["total"] == 2 and filtered["query"] == "上海"
        assert {trip["destination"] for trip in filtered["trips"]} == {"上海"}
        assert all(trip["conflicts"] for trip in filtered["trips"]), (
            "过滤后重叠信息仍按完整历史计算"
        )

        cross = client.get(
            "/api/trips", params={"user_id": user_id, "q": "北京 上海"}
        ).json()
        assert cross["total"] == 2, "跨字段多词查询应命中"

        by_purpose = client.get(
            "/api/trips", params={"user_id": user_id, "q": "会议"}
        ).json()
        assert by_purpose["total"] == 1
        assert by_purpose["trips"][0]["destination"] == "杭州"

        empty = client.get(
            "/api/trips", params={"user_id": user_id, "q": "不存在"}
        ).json()
        assert empty["total"] == 0 and empty["trips"] == []

        too_long = client.get(
            "/api/trips", params={"user_id": user_id, "q": "x" * 101}
        )
        assert too_long.status_code == 422
        print("✓ /api/trips q 过滤（路线 / 跨字段 / 目的 / 无匹配）")
    finally:
        _cleanup(user_id)


def test_status_trips_include_conflicts():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trip_badges")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for destination, start, end in [
                ("上海", "2026-12-05", "2026-12-07"),
                ("杭州", "2026-12-06", "2026-12-08"),
                ("南京", "2026-12-09", "2026-12-10"),
            ]:
                store.save_trip_history(
                    {
                        "origin": "北京",
                        "destination": destination,
                        "start_date": start,
                        "end_date": end,
                    }
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        status = _client().get("/api/status", params={"user_id": user_id}).json()
        by_destination = {trip["destination"]: trip for trip in status["trips"]}
        assert [
            item["destination"] for item in by_destination["上海"]["conflicts"]
        ] == ["杭州"]
        assert [
            item["destination"] for item in by_destination["杭州"]["conflicts"]
        ] == ["上海"]
        assert by_destination["南京"]["conflicts"] == []
        print("✓ /api/status 行程附带日期重叠信息（不包含自身）")
    finally:
        _cleanup(user_id)


def test_session_archive_api():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_archive")
    session_id = "archive-1"
    other_session = "archive-2"
    client = _client()
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for sid in (session_id, other_session):
                store.add_chat_message("user", f"消息 {sid}", sid)
                store.add_chat_message("assistant", f"回答 {sid}", sid)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        archived = client.put(
            f"/api/sessions/{session_id}/archive",
            json={"user_id": user_id, "archived": True},
        )
        assert archived.status_code == 200
        assert archived.json()["archived"] is True

        active = client.get("/api/sessions", params={"user_id": user_id}).json()
        active_ids = {item["session_id"] for item in active["sessions"]}
        assert session_id not in active_ids, "归档会话不应出现在默认列表"
        assert other_session in active_ids
        assert active["archived_count"] == 1

        archived_list = client.get(
            "/api/sessions", params={"user_id": user_id, "archived": True}
        ).json()
        assert [item["session_id"] for item in archived_list["sessions"]] == [session_id]
        assert archived_list["sessions"][0]["archived"] is True
        assert archived_list["archived_count"] == 1

        # 归档不影响读取会话内容，详情返回标题与归档标记
        detail = client.get(
            f"/api/sessions/{session_id}", params={"user_id": user_id}
        )
        assert detail.status_code == 200
        assert len(detail.json()["messages"]) == 2
        assert detail.json()["title"] == "消息 archive-1"
        assert detail.json()["archived"] is True

        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["archived_count"] == 1

        restored = client.put(
            f"/api/sessions/{session_id}/archive",
            json={"user_id": user_id, "archived": False},
        )
        assert restored.status_code == 200
        active = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert session_id in {item["session_id"] for item in active["sessions"]}
        assert active["archived_count"] == 0

        assert (
            client.put(
                "/api/sessions/no-such-session/archive",
                json={"user_id": user_id, "archived": True},
            ).status_code
            == 404
        )

        # 消息被删光的归档标记不计入计数；删除会话时清理归档标记
        client.put(
            f"/api/sessions/{session_id}/archive",
            json={"user_id": user_id, "archived": True},
        )
        removed = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "消息 archive-1"},
        )
        assert removed.json()["removed"] == 2
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["archived_count"] == 0, "没有消息的归档标记不应计入"
        ghost_list = client.get(
            "/api/sessions", params={"user_id": user_id, "archived": True}
        ).json()
        assert ghost_list["total"] == 0, "消息删光后归档列表应为空"
        client.delete(f"/api/sessions/{session_id}", params={"user_id": user_id})
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["archived_count"] == 0
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert session_id not in store.get_archived_sessions(), "删除会话应清理归档标记"
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        print("✓ 会话归档 / 恢复（列表过滤 / 内容可读 / 计数 / 删除清理）")
    finally:
        _cleanup(user_id)


def test_archived_sessions_search_and_pagination():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_arch_paging")
    client = _client()
    sessions = ["arch-page-1", "arch-page-2", "arch-page-3"]
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for index, sid in enumerate(sessions, start=1):
                store.add_chat_message("user", f"归档分页问题 {index}", sid)
                store.add_chat_message("assistant", f"回答 {index}", sid)
            store.set_session_pinned(sessions[0], True)
            store.set_session_archived(sessions[0], True)
            store.set_session_archived(sessions[1], True)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        searched = client.get(
            "/api/sessions",
            params={"user_id": user_id, "archived": True, "q": "分页问题 2"},
        ).json()
        assert [item["session_id"] for item in searched["sessions"]] == [sessions[1]]

        page1 = client.get(
            "/api/sessions",
            params={"user_id": user_id, "archived": True, "limit": 1, "offset": 0},
        ).json()
        page2 = client.get(
            "/api/sessions",
            params={"user_id": user_id, "archived": True, "limit": 1, "offset": 1},
        ).json()
        assert page1["total"] == page2["total"] == 2
        page_ids = {
            page1["sessions"][0]["session_id"],
            page2["sessions"][0]["session_id"],
        }
        assert page_ids == {sessions[0], sessions[1]}
        assert page1["sessions"][0]["pinned"] is False, "归档视图不展示置顶排序"
        assert all(
            item["archived"] for item in page1["sessions"] + page2["sessions"]
        )

        active = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert [item["session_id"] for item in active["sessions"]] == [sessions[2]]
        print("✓ 归档会话搜索 / 分页 / 置顶标记")
    finally:
        _cleanup(user_id)


def test_trips_csv_export():
    import csv
    import io

    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trips_csv")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-12-05",
                    "end_date": "2026-12-07",
                    "purpose": "出差,客户拜访",
                }
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": '=HYPERLINK("http://example.invalid")',
                    "start_date": "2026-12-11",
                    "purpose": "@SUM(1)",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        response = _client().get("/api/trips/export", params={"user_id": user_id})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers.get("content-disposition", "")
        assert response.content.startswith(b"\xef\xbb\xbf"), "缺少 UTF-8 BOM"
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert rows[0] == ["出发地", "目的地", "出发日期", "返程日期", "目的"]
        assert rows[1] == ["北京", "上海", "2026-12-05", "2026-12-07", "出差,客户拜访"]
        assert rows[2][1] == '\'=HYPERLINK("http://example.invalid")', "公式前缀应被转义"
        assert rows[2][4] == "'@SUM(1)"

        empty_user = _unique_user("web_test_trips_csv_empty")
        try:
            empty = _client().get(
                "/api/trips/export", params={"user_id": empty_user}
            )
            empty_rows = list(
                csv.reader(io.StringIO(empty.content.decode("utf-8-sig")))
            )
            assert len(empty_rows) == 1, "无行程时只输出表头"
        finally:
            _cleanup(empty_user)
        print("✓ 行程 CSV 导出（BOM / 逗号转义 / 空列表）")
    finally:
        _cleanup(user_id)


def test_session_metadata_storage_failure_maps_503(monkeypatch):
    """标题 / 置顶 / 归档写盘失败统一返回 503。"""
    from webapp import server as server_module

    class _BrokenStore:
        def get_chat_history(self, session_id=None):
            return [{"role": "user", "content": "x", "session_id": session_id}]

        def set_session_title(self, *args):
            raise RuntimeError("disk full")

        def set_session_pinned(self, *args):
            raise RuntimeError("disk full")

        def set_session_archived(self, *args):
            raise RuntimeError("disk full")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    client = _client()
    assert (
        client.put(
            "/api/sessions/s1/title", json={"user_id": "u1", "title": "标题"}
        ).status_code
        == 503
    )
    assert (
        client.put(
            "/api/sessions/s1/pin", json={"user_id": "u1", "pinned": True}
        ).status_code
        == 503
    )
    assert (
        client.put(
            "/api/sessions/s1/archive",
            json={"user_id": "u1", "archived": True},
        ).status_code
        == 503
    )
    print("✓ 会话标题 / 置顶 / 归档写盘失败 → 503")


def test_trips_csv_export_query_filter():
    """导出接口的 q 与列表接口同一口径，只导出命中的行程。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trips_export_q")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-02",
                    "purpose": "出差",
                }
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-12-05",
                    "end_date": "2026-12-06",
                    "purpose": "会议",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        full = client.get("/api/trips/export", params={"user_id": user_id})
        assert full.status_code == 200
        assert "上海" in full.text and "杭州" in full.text

        filtered = client.get(
            "/api/trips/export", params={"user_id": user_id, "q": "上海"}
        )
        assert filtered.status_code == 200
        assert filtered.text.startswith("﻿")
        assert "上海" in filtered.text and "杭州" not in filtered.text

        empty = client.get(
            "/api/trips/export", params={"user_id": user_id, "q": "不存在"}
        )
        assert empty.status_code == 200
        lines = [line for line in empty.text.strip().splitlines() if line]
        assert len(lines) == 1, "无匹配时只保留表头"
        print("✓ /api/trips/export q 过滤")
    finally:
        _cleanup(user_id)


def test_trips_csv_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    response = _client().get("/api/trips/export", params={"user_id": "u1"})
    assert response.status_code == 503
    listed = _client().get("/api/trips", params={"user_id": "u1"})
    assert listed.status_code == 503
    print("✓ 行程 CSV / 列表读取失败 → 503")


def test_delete_session_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def get_chat_history(self, session_id=None):
            return [{"role": "user", "content": "hi", "session_id": session_id}]

        def delete_chat_session(self, session_id):
            raise RuntimeError("disk full")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    client = _client()
    response = client.delete("/api/sessions/s1", params={"user_id": "u1"})
    assert response.status_code == 503
    state = client.app.state.aligo
    assert state.is_recently_deleted("u1", "s1") is False, "删除失败应撤销墓碑"
    print("✓ 删除会话存储失败 → 503（墓碑回滚）")


def test_repeated_delete_keeps_tombstone():
    """重复删除（重试 / 另一标签页）返回 0 时必须保留墓碑，旧 id 不能复活。"""
    client = _client()
    user_id = _unique_user("web_test_double_delete")
    try:
        created = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "double-del"},
        )
        assert created.status_code == 200
        first = client.delete(
            "/api/sessions/double-del", params={"user_id": user_id}
        )
        assert first.status_code == 200 and first.json()["deleted"] == 2
        again = client.delete(
            "/api/sessions/double-del", params={"user_id": user_id}
        )
        assert again.status_code == 200 and again.json()["deleted"] == 0
        blocked = client.post(
            "/api/chat",
            json={"message": "再问", "user_id": user_id, "session_id": "double-del"},
        )
        assert blocked.status_code == 404, "重复删除不能撤销墓碑"
        print("✓ 重复删除保留墓碑，旧 id 不能复活")
    finally:
        _cleanup(user_id)


def test_delete_missing_session_allows_new_chat():
    """从未存在的会话删除后不保留本地墓碑：该 id 仍可新开对话。"""
    client = _client()
    user_id = _unique_user("web_test_missing_delete")
    try:
        deleted = client.delete(
            "/api/sessions/never-existed", params={"user_id": user_id}
        )
        assert deleted.status_code == 200 and deleted.json()["deleted"] == 0
        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "never-existed"},
        )
        assert chat.status_code == 200, "不存在的会话删除后不应留下墓碑"
        print("✓ 不存在会话删除后不保留墓碑")
    finally:
        _cleanup(user_id)


def test_delete_missing_session_failure_does_not_leave_tombstone(monkeypatch):
    """从未存在的会话删除又失败时也要撤销墓碑，允许该 id 新开对话。"""
    from webapp import server as server_module

    class _Store:
        def get_chat_history(self, session_id=None):
            return []

        def delete_chat_session(self, session_id):
            raise RuntimeError("delete boom")

        def close(self):
            pass

    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, strict=False: _Store())
    client = _client()
    state = client.app.state.aligo
    response = client.delete("/api/sessions/ghost", params={"user_id": "u-ghost"})
    assert response.status_code == 503
    assert state.is_recently_deleted("u-ghost", "ghost") is False
    print("✓ 删除不存在会话失败后不留墓碑")


def test_tombstone_helpers_are_thread_safe():
    """墓碑的 mark/unmark/读取会被线程池并发调用，OrderedDict 不能裸迭代。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    # 小容量上限 + 共享用户前缀：让「清理过期 + 容量淘汰」在并发下频繁执行
    state.max_tombstones = 50
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(1500):
                state.mark_deleted(f"u{n % 3}", f"s{i % 40}")
                state.is_recently_deleted(f"u{n % 3}", f"s{i % 40}")
                if i % 3:
                    state.unmark_deleted(f"u{n % 3}", f"s{i % 40}")
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors[:1]
    assert len(state._deleted_sessions) <= state.max_tombstones, (
        f"墓碑数不能超上限，实际 {len(state._deleted_sessions)}"
    )
    print("✓ 删除墓碑字典并发安全（小上限触发淘汰）")


def test_sessions_pagination():
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_paging")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            for index in range(6):
                store.add_chat_message("user", f"消息 {index}", f"page-{index}")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        first = client.get(
            "/api/sessions", params={"user_id": user_id, "limit": 2}
        ).json()
        assert first["total"] == 6
        assert len(first["sessions"]) == 2
        assert first["offset"] == 0 and first["limit"] == 2

        second = client.get(
            "/api/sessions", params={"user_id": user_id, "limit": 2, "offset": 2}
        ).json()
        assert len(second["sessions"]) == 2
        first_ids = {item["session_id"] for item in first["sessions"]}
        second_ids = {item["session_id"] for item in second["sessions"]}
        assert not (first_ids & second_ids), "分页之间不应重复"

        search = client.get(
            "/api/sessions",
            params={"user_id": user_id, "q": "消息 3", "limit": 2, "offset": 0},
        ).json()
        assert search["total"] == 1
        assert "消息 3" in search["sessions"][0]["title"]
        print("✓ 会话列表分页（total / offset / 去重 / 搜索）")
    finally:
        _cleanup(user_id)


def test_metrics_api():
    """GET /api/metrics 返回请求计数 / 状态 / 耗时与熔断器状态。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    try:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").status_code == 200
        data = client.get("/api/metrics").json()
        requests = data["requests"]
        assert requests["requests_total"] >= 2
        assert requests["by_path"]["/api/health"] >= 2
        assert requests["by_status"].get("200", 0) >= 2
        assert requests["avg_duration_ms"] >= 0
        assert requests["p50_duration_ms"] >= 0
        assert requests["p95_duration_ms"] >= requests["p50_duration_ms"]
        assert requests["duration_samples"] >= 2
        assert data["circuit_breaker"]["state"] in ("closed", "open", "half_open")
        assert isinstance(data["sessions_cached"], int)
        rate = data["rate_limit"]
        assert rate["backend"] in {"memory", "redis"}
        assert rate["configured"] is False
        assert rate["degraded"] is False
        assert rate["user_limit"] >= 1 and rate["ip_limit"] >= 1
        assert rate["kb_limit"] >= 1
        assert rate["weather_limit"] >= 1
        assert rate["window_sec"] > 0
        print("✓ /api/metrics 请求指标")
    finally:
        REQUEST_METRICS.reset()


def test_metrics_paths_use_route_templates():
    """指标按路由模板聚合：动态段不撑基数，未匹配路径归入 __unmatched__。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    user_id = _unique_user("web_test_metric_template")
    try:
        client.get("/api/sessions/deadbeefcafe", params={"user_id": user_id})
        client.get("/api/no-such-endpoint")
        by_path = REQUEST_METRICS.snapshot()["by_path"]
        assert "/api/sessions/{session_id}" in by_path
        assert "/api/sessions/deadbeefcafe" not in by_path
        assert "__unmatched__" in by_path
        print("✓ 指标路径使用路由模板，未匹配路径归入 __unmatched__")
    finally:
        REQUEST_METRICS.reset()


def test_prometheus_metrics_endpoint():
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    try:
        assert client.get("/api/health").status_code == 200
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["cache-control"] == "no-store"
        text = response.text
        assert "aligo_requests_total" in text
        assert "aligo_circuit_breaker_state" in text
        assert "aligo_sessions_cached" in text
        assert "aligo_rate_limit_degraded 0" in text
        assert 'aligo_rate_limit_backend{backend="memory"} 1' in text
        assert 'aligo_storage_backend{short_term="memory",long_term="json"} 1' in text
        assert "aligo_short_term_degraded 0" in text
        assert "aligo_long_term_degraded 0" in text
        assert "aligo_tombstones_shared 0" in text
        print("✓ /metrics Prometheus 文本指标")
    finally:
        REQUEST_METRICS.reset()


def test_metrics_values_reflect_sessions_and_breaker():
    """会话数 / 熔断失败计数 / 熔断状态的具体数值，而不是只有指标名。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.get_session("u-metrics", "s-metrics")
    client = TestClient(app)
    text = client.get("/metrics").text
    assert f"aligo_sessions_cached {len(state.sessions)}" in text
    assert "aligo_circuit_breaker_state 0" in text
    assert "aligo_circuit_breaker_failures 0" in text

    state.breaker.record_failure()
    text = client.get("/metrics").text
    assert "aligo_circuit_breaker_failures 1" in text
    for _ in range(state.breaker.failure_threshold - 1):
        state.breaker.record_failure()
    text = client.get("/metrics").text
    assert "aligo_circuit_breaker_state 2" in text, "阈值达到后应显示 open 状态"
    state.breaker.record_success()  # 复位：避免影响同进程其它用例
    state.close_all()
    print("✓ /metrics 数值随会话与熔断状态变化")


def test_prometheus_storage_gauges_reflect_state(monkeypatch):
    """存储后端 / 降级 / 共享墓碑状态要反映实际值，而不是固定 0。"""
    from webapp import server as server_module

    client = _client()
    monkeypatch.setattr(
        server_module,
        "describe_backends",
        lambda: {
            "short_term": "redis",
            "long_term": "json",
            "short_term_configured": True,
            "long_term_configured": True,
            "short_term_degraded": True,
            "long_term_degraded": True,
        },
    )
    client.app.state.aligo._tombstones_shared = True
    text = client.get("/metrics").text
    assert 'aligo_storage_backend{short_term="redis",long_term="json"} 1' in text
    assert "aligo_short_term_degraded 1" in text
    assert "aligo_long_term_degraded 1" in text
    assert "aligo_tombstones_shared 1" in text
    print("✓ /metrics 存储后端 / 降级 / 墓碑指标反映实际状态")


def test_metrics_endpoint_not_in_snapshot_before_recording():
    """指标接口自身在返回后才计数，快照不包含当前请求（避免自增误导）。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    try:
        first = client.get("/api/metrics").json()
        second = client.get("/api/metrics").json()
        assert first["requests"]["requests_total"] == 0
        assert second["requests"]["requests_total"] == 1
        print("✓ 指标接口自身在快照之后再计数")
    finally:
        REQUEST_METRICS.reset()


def test_request_id_header():
    client = _client()
    response = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers.get("x-request-id") == "trace-abc-123"

    generated = client.get("/api/health").headers.get("x-request-id", "")
    assert re.fullmatch(r"[0-9a-f]{12}", generated), generated
    print("✓ X-Request-ID 透传 / 自动生成")


def test_rate_limit_headers():
    client = _client()
    user_id = _unique_user("web_test_rl_headers")
    try:
        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "rl-h1"},
        )
        assert first.status_code == 200
        assert first.headers["x-ratelimit-limit"] == "30"
        remaining_first = int(first.headers["x-ratelimit-remaining"])
        assert int(first.headers["x-ratelimit-reset"]) >= 1

        second = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "rl-h2"},
        )
        remaining_second = int(second.headers["x-ratelimit-remaining"])
        assert remaining_second == remaining_first - 1
        print("✓ 对话响应附带 X-RateLimit-Limit / Remaining / Reset")
    finally:
        _cleanup(user_id)


def test_chat_rate_limit():
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=2, window_sec=60)
    client = TestClient(app)

    user_id = _unique_user("web_test_limit")
    other_id = _unique_user("web_test_limit_other")
    try:
        for _ in range(2):
            response = client.post(
                "/api/chat",
                json={"message": "你好", "user_id": user_id, "session_id": "sess-limit"},
            )
            assert response.status_code == 200

        limited = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-limit"},
        )
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers
        assert limited.headers["x-ratelimit-remaining"] == "0"
        assert "请求 ID" in limited.json()["detail"]

        other = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": other_id, "session_id": "sess-other"},
        )
        assert other.status_code == 200, "不同用户不应共享限流额度"
        print("✓ 对话接口限流（429 / Retry-After / 用户隔离）")
    finally:
        _cleanup(user_id)
        _cleanup(other_id)


def test_rate_limit_race_rejects_over_limit():
    """peek 与 check 之间额度被并发请求抢走时，按 check 的结果拒绝。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    real = RateLimiter(limit=5, window_sec=60)

    class _RaceLimiter:
        def __init__(self, delegate):
            self._delegate = delegate

        @property
        def limit(self):
            return self._delegate.limit

        def peek(self, key):
            # 模拟 peek 时仍有额度
            return None

        def check(self, key):
            # 模拟 check 时额度已被并发请求抢走
            return 7.2

        def status(self, key):
            return self._delegate.status(key)

    state.rate_limiter = _RaceLimiter(real)
    client = TestClient(app)
    user_id = _unique_user("web_test_limit_race")
    response = client.post(
        "/api/chat",
        json={"message": "你好", "user_id": user_id, "session_id": "s-race"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "8"
    assert response.headers["x-ratelimit-limit"] == "5"
    assert response.headers["x-ratelimit-remaining"] == "0"
    print("✓ 限流 peek/check 竞态按 check 结果拒绝")


def test_chat_rate_limit_rejected_request_does_not_consume_other_bucket():
    """被 429 的请求不能消耗另一个桶的配额（否则同 IP 其它用户被连坐）。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=1, window_sec=60)   # 用户桶：1 次
    state.ip_limiter = RateLimiter(limit=2, window_sec=60)     # IP 桶：2 次
    client = TestClient(app)

    users = []
    try:
        user_a = _unique_user("web_test_bucket_a")
        user_b = _unique_user("web_test_bucket_b")
        users = [user_a, user_b]
        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_a, "session_id": "bucket-a1"},
        )
        assert first.status_code == 200
        rejected = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_a, "session_id": "bucket-a2"},
        )
        assert rejected.status_code == 429, "用户桶已用尽"
        # 若被拒绝的请求仍记录 IP 配额，B 的第一次请求会因 IP 桶（2 次）已满而 429
        other = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_b, "session_id": "bucket-b1"},
        )
        assert other.status_code == 200, "被拒绝的请求不应消耗 IP 桶配额"
        print("✓ 429 请求不消耗另一限流桶的配额")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_rate_limit_trusted_proxy_headers(monkeypatch):
    """TRUST_PROXY_HEADERS 开启后按 X-Forwarded-For 第一个地址限流。"""
    from config import RATE_LIMIT_CONFIG
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=100, window_sec=60)
    client = TestClient(app)
    users = []
    try:
        # 默认不信任 XFF：不同 XFF 的请求仍共用同一 IP 桶（TCP 对端）
        user_a = _unique_user("web_test_proxy_a")
        user_b = _unique_user("web_test_proxy_b")
        users = [user_a, user_b]
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_a, "session_id": "proxy-a1"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert first.status_code == 200
        second = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_b, "session_id": "proxy-b1"},
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        assert second.status_code == 429, "默认配置下不应信任 X-Forwarded-For"

        # 开启信任后：相同 XFF 共享 IP 桶，不同 XFF 互不影响
        user_c = _unique_user("web_test_proxy_c")
        user_d = _unique_user("web_test_proxy_d")
        user_e = _unique_user("web_test_proxy_e")
        users += [user_c, user_d, user_e]
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_headers", True)
        forwarded = "203.0.113.9, 10.0.0.1"
        third = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_c, "session_id": "proxy-c1"},
            headers={"X-Forwarded-For": forwarded},
        )
        assert third.status_code == 200
        fourth = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_d, "session_id": "proxy-d1"},
            headers={"X-Forwarded-For": forwarded},
        )
        assert fourth.status_code == 429, "相同转发地址应共享 IP 桶"
        fifth = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_e, "session_id": "proxy-e1"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert fifth.status_code == 200, "不同转发地址不应互相影响"
        print("✓ 可信代理开启后按 X-Forwarded-For 第一个地址限流")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_ip_rejection_does_not_consume_user_bucket():
    """IP 桶拒绝时不能消耗用户桶配额（与用户桶拒绝对称）。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=10, window_sec=60)
    state.ip_limiter = RateLimiter(limit=1, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_bucket_rev")
    try:
        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "b2-1"},
        )
        assert first.status_code == 200
        rejected = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "b2-2"},
        )
        assert rejected.status_code == 429
        remaining, _ = state.rate_limiter.status(f"testclient:{user_id}")
        assert remaining == 9, f"被 IP 桶拒绝时消耗了用户桶配额（剩 {remaining}）"
        print("✓ IP 桶拒绝不消耗用户桶配额")
    finally:
        _cleanup(user_id)


def test_deleted_session_cannot_be_resurrected():
    """删除后的会话在墓碑窗口内不能通过旧 session_id 写回。"""
    client = _client()
    user_id = _unique_user("web_test_tombstone")
    try:
        created = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "tomb-sess"},
        )
        assert created.status_code == 200
        deleted = client.delete(
            "/api/sessions/tomb-sess", params={"user_id": user_id}
        )
        assert deleted.status_code == 200 and deleted.json()["deleted"] == 2

        again = client.post(
            "/api/chat",
            json={"message": "再问一次", "user_id": user_id, "session_id": "tomb-sess"},
        )
        assert again.status_code == 404
        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert sessions["total"] == 0, "已删除的会话不应被写回"
        print("✓ 删除后的会话不会被旧 session_id 复活")
    finally:
        _cleanup(user_id)


def test_shared_tombstones_across_instances():
    """配置 Redis 时，删除墓碑跨实例可见（新请求会被拒绝）。"""

    class _FakeRedis:
        def __init__(self):
            self.data = {}
            self.ttls = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            self.ttls[key] = ex
            return True

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    shared = _FakeRedis()
    app_a = create_app(model=UsageTrackingModel(_FakeModel()))
    app_b = create_app(model=UsageTrackingModel(_FakeModel()))
    state_a = app_a.state.aligo
    state_b = app_b.state.aligo
    for state in (state_a, state_b):
        state._redis_client = shared
        state._tombstones_shared = True

    state_a.mark_deleted("u-shared", "s-shared")
    assert "aligo:deleted:u-shared:s-shared" in shared.data
    assert shared.ttls["aligo:deleted:u-shared:s-shared"] == int(
        state_a.tombstone_ttl_sec
    ), "共享墓碑 TTL 应与本地一致"
    assert state_b.is_recently_deleted("u-shared", "s-shared") is True
    assert state_b.is_recently_deleted("u-shared", "other") is False

    state_a.unmark_deleted("u-shared", "s-shared")
    assert state_b.is_recently_deleted("u-shared", "s-shared") is False
    print("✓ 删除墓碑通过 Redis 跨实例共享（含 TTL 与撤销）")


def test_shared_tombstone_not_overwritten_by_other_instance():
    """SET NX：重复标记不覆盖其它实例的墓碑，回滚只撤销自己的 token。"""

    class _FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)

        def eval(self, script, numkeys, *args):
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    shared = _FakeRedis()
    state_a = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state_b = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    for state in (state_a, state_b):
        state._redis_client = shared
        state._tombstones_shared = True

    token_a = state_a.mark_deleted("u-nx", "s-nx")
    token_b = state_b.mark_deleted("u-nx", "s-nx")
    assert token_a != token_b
    key = "aligo:deleted:u-nx:s-nx"
    assert shared.data[key] == token_a, "第二个实例不能覆盖已有墓碑"

    state_b.unmark_deleted("u-nx", "s-nx", token_b)
    assert shared.data.get(key) == token_a, "回滚不能误删其它实例的墓碑"
    assert state_a.is_recently_deleted("u-nx", "s-nx") is True

    state_a.unmark_deleted("u-nx", "s-nx", token_a)
    assert key not in shared.data
    assert state_b.is_recently_deleted("u-nx", "s-nx") is False
    print("✓ 共享墓碑按 token 归属（NX 不覆盖 / 回滚只删自己的）")


def test_delete_failures_roll_back_shared_tombstone(monkeypatch):
    """共享墓碑模式下删除失败 / 从未存在时也要回滚本请求的墓碑。"""
    from webapp import server as server_module

    class _FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)

        def eval(self, script, numkeys, *args):
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    class _Store:
        fail_exists = True

        def get_chat_history(self, session_id=None):
            if self.fail_exists:
                raise RuntimeError("db down")
            return []

        def delete_chat_session(self, session_id):
            return 0

        def close(self):
            pass

    shared = _FakeRedis()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state._redis_client = shared
    state._tombstones_shared = True
    monkeypatch.setattr(server_module, "_open_long_term", lambda uid, strict=False: _Store())
    client = TestClient(app)

    failed = client.delete("/api/sessions/s-db", params={"user_id": "u-db"})
    assert failed.status_code == 503
    key = "aligo:deleted:u-db:s-db"
    assert key not in shared.data, "存在性检查失败后应撤销本请求的共享墓碑"
    assert state.is_recently_deleted("u-db", "s-db") is False, "本地墓碑也应撤销"

    _Store.fail_exists = False
    missing = client.delete("/api/sessions/s-ghost", params={"user_id": "u-db"})
    assert missing.status_code == 200 and missing.json()["deleted"] == 0
    ghost_key = "aligo:deleted:u-db:s-ghost"
    assert ghost_key not in shared.data, "从未存在的会话不应留下共享墓碑"
    assert state.is_recently_deleted("u-db", "s-ghost") is False
    print("✓ 共享墓碑下删除失败 / 从未存在时回滚自己的墓碑")


def test_repeated_delete_repairs_shared_tombstone():
    """首次删除时共享墓碑写入失败，冷却结束后的重复删除要补写。"""

    class _FlakySharedRedis:
        def __init__(self):
            self.data = {}
            self.fail = True

        def set(self, key, value, nx=False, ex=None):
            if self.fail:
                raise ConnectionError("redis down")
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            if self.fail:
                raise ConnectionError("redis down")
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    shared = _FlakySharedRedis()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state._redis_client = shared
    state._tombstones_shared = False
    client = TestClient(app)
    user_id = _unique_user("web_test_tombstone_repair")
    try:
        created = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "repair"},
        )
        assert created.status_code == 200
        first = client.delete("/api/sessions/repair", params={"user_id": user_id})
        assert first.status_code == 200 and first.json()["deleted"] == 2
        assert not shared.data, "共享墓碑写入应失败"

        shared.fail = False
        state._tombstone_retry_at = 0.0  # 模拟冷却结束
        second = client.delete("/api/sessions/repair", params={"user_id": user_id})
        assert second.status_code == 200 and second.json()["deleted"] == 0
        assert f"aligo:deleted:{user_id}:repair" in shared.data, (
            "重复删除应补写上一次失败的共享墓碑"
        )
        print("✓ 重复删除补写共享墓碑")
    finally:
        _cleanup(user_id)


def test_tombstone_redis_retry_after_failure():
    """共享墓碑一次失败只回退本次，冷却后自动恢复 Redis 路径。"""

    class _FlakyRedis:
        def __init__(self):
            self.fail = False
            self.data = {}
            self.calls = 0

        def set(self, key, value, nx=False, ex=None):
            self.calls += 1
            if self.fail:
                raise ConnectionError("redis blip")
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            self.calls += 1
            if self.fail:
                raise ConnectionError("redis blip")
            return int(key in self.data)

        def delete(self, key):
            self.calls += 1
            if self.fail:
                raise ConnectionError("redis blip")
            self.data.pop(key, None)

        def eval(self, script, numkeys, *args):
            self.calls += 1
            if self.fail:
                raise ConnectionError("redis blip")
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    client = _FlakyRedis()
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state._redis_client = client
    state._tombstones_shared = True

    client.fail = True
    state.mark_deleted("u-retry", "s-retry")
    assert state._tombstones_shared is False
    assert state._tombstone_retry_at > 0
    assert state.is_recently_deleted("u-retry", "s-retry") is True, "本地墓碑兜底"

    calls_before = client.calls
    state.is_recently_deleted("u-retry", "s-other")
    assert client.calls == calls_before, "冷却期内不应重复请求 Redis"

    client.fail = False
    state._tombstone_retry_at = 0.0
    client.data["aligo:deleted:u-retry:s-other"] = "1"
    assert state.is_recently_deleted("u-retry", "s-other") is True
    assert state._tombstones_shared is True, "冷却后应恢复共享墓碑"

    state.unmark_deleted("u-retry", "s-other")
    assert "aligo:deleted:u-retry:s-other" not in client.data
    print("✓ 共享墓碑故障回退并在冷却后恢复")


def test_shared_tombstone_healed_on_read(monkeypatch):
    """共享墓碑首次写入失败后，命中本地墓碑的读取会顺带补写（自愈）。"""

    class _FlakySharedRedis:
        def __init__(self):
            self.data = {}
            self.fail = True
            self.set_calls = 0

        def set(self, key, value, nx=False, ex=None):
            if self.fail:
                raise ConnectionError("redis down")
            if nx and key in self.data:
                return None
            self.data[key] = value
            self.set_calls += 1
            return True

        def exists(self, key):
            if self.fail:
                raise ConnectionError("redis down")
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)

    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    client = _FlakySharedRedis()
    state._redis_client = client
    state._tombstones_shared = False

    state.mark_deleted("u-heal", "s-heal")
    assert "aligo:deleted:u-heal:s-heal" not in client.data, "首次写入失败"
    assert state.is_recently_deleted("u-heal", "s-heal") is True

    client.fail = False
    state._tombstone_retry_at = 0.0  # 冷却结束
    assert state.is_recently_deleted("u-heal", "s-heal") is True
    assert "aligo:deleted:u-heal:s-heal" in client.data, "命中本地墓碑时应补写共享墓碑"
    assert client.set_calls == 1
    print("✓ 共享墓碑在读取时自愈")


def test_tombstone_reconnects_when_redis_appears_later(monkeypatch):
    """启动时没有 Redis 客户端，恢复后通过共享客户端工厂重新接入。"""
    from webapp import server as server_module

    class _FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)

        def eval(self, script, numkeys, *args):
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    client = _FakeRedis()
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state._redis_client = None
    state._tombstones_shared = False
    monkeypatch.setattr(server_module, "get_shared_redis_client", lambda: client)

    state.mark_deleted("u-late", "s-late")
    assert "aligo:deleted:u-late:s-late" in client.data
    assert state._tombstones_shared is True
    print("✓ 启动时无 Redis 的墓碑在恢复后接入共享")


def test_tombstone_expired_entries_pruned_before_eviction():
    """容量淘汰前先清理过期墓碑，避免仍有效的墓碑被挤出。"""
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_tombstones = 2
    now = time.monotonic()
    state._deleted_sessions["u\nold"] = (
        now - state.tombstone_ttl_sec - 1,
        "old",
    )
    state._deleted_sessions["u\nkeep"] = (now, "keep")

    state.mark_deleted("u", "new")
    assert "u\nold" not in state._deleted_sessions
    assert "u\nkeep" in state._deleted_sessions
    assert "u\nnew" in state._deleted_sessions
    assert len(state._deleted_sessions) == 2, "清理过期后不应超过容量上限"

    # 过期键比容量最早键「更新」：删掉过期清理分支时，容量淘汰会挤掉
    # 仍有效的最旧墓碑 keep2，断言 keep2 存活即可区分两个分支
    state._deleted_sessions.clear()
    now = time.monotonic()
    state._deleted_sessions["u\nkeep2"] = (now - 1, "keep2")
    state._deleted_sessions["u\nexpired2"] = (
        now - state.tombstone_ttl_sec - 1,
        "expired2",
    )
    state.mark_deleted("u", "newer")
    assert "u\nexpired2" not in state._deleted_sessions, "过期墓碑应先被清理"
    assert "u\nkeep2" in state._deleted_sessions, (
        "未被淘汰的有效墓碑不能因为缺少过期清理而被挤出"
    )
    assert state.is_recently_deleted("u", "newer") is True
    print("✓ 墓碑容量淘汰前先清理过期条目（过期键非最旧）")


def test_session_metadata_requires_existing_session():
    client = _client()
    user_id = _unique_user("web_test_ghost")
    try:
        renamed = client.put(
            "/api/sessions/ghost-session/title",
            json={"user_id": user_id, "title": "幽灵标题"},
        )
        assert renamed.status_code == 404
        pinned = client.put(
            "/api/sessions/ghost-session/pin",
            json={"user_id": user_id, "pinned": True},
        )
        assert pinned.status_code == 404
        detail = client.get(
            "/api/sessions/ghost-session", params={"user_id": user_id}
        ).json()
        assert "幽灵标题" not in detail["title"], "不存在的会话不应产生幽灵标题"
        print("✓ 重命名 / 置顶拒绝不存在的会话")
    finally:
        _cleanup(user_id)


def test_rename_after_delete_is_rejected():
    """删除后写入标题在存储层同一把锁内被拒绝，不会留下幽灵标题。"""
    client = _client()
    user_id = _unique_user("web_test_ghost_deleted")
    try:
        created = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "ghost-del"},
        )
        assert created.status_code == 200
        assert (
            client.put(
                "/api/sessions/ghost-del/title",
                json={"user_id": user_id, "title": "正常标题"},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                "/api/sessions/ghost-del", params={"user_id": user_id}
            ).status_code
            == 200
        )
        renamed = client.put(
            "/api/sessions/ghost-del/title",
            json={"user_id": user_id, "title": "幽灵标题"},
        )
        assert renamed.status_code == 404
        detail = client.get(
            "/api/sessions/ghost-del", params={"user_id": user_id}
        ).json()
        assert detail["title"] != "幽灵标题"
        print("✓ 删除后重命名返回 404，不写幽灵标题")
    finally:
        _cleanup(user_id)


def test_delete_unknown_session_does_not_block_new_chat():
    """删除不存在的会话不应留下墓碑，该 id 仍可新开对话。"""
    client = _client()
    user_id = _unique_user("web_test_delete_missing")
    try:
        deleted = client.delete(
            "/api/sessions/never-existed", params={"user_id": user_id}
        )
        assert deleted.status_code == 200 and deleted.json()["deleted"] == 0
        created = client.post(
            "/api/chat",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "never-existed",
            },
        )
        assert created.status_code == 200, "删除不存在的会话不应留下墓碑"
        print("✓ 删除不存在的会话不留墓碑")
    finally:
        _cleanup(user_id)


def test_reserved_session_id_is_rejected():
    response = _client().post(
        "/api/chat",
        json={"message": "你好", "user_id": "u1", "session_id": "export"},
    )
    assert response.status_code == 422
    print("✓ 保留字 session_id（export）被拒绝")


def test_session_read_failures_map_503(monkeypatch):
    """会话相关读接口在存储故障时应返回可重试的 503，而不是通用 500。"""
    from webapp import server as server_module

    class _BrokenStore:
        def _fail(self, *args, **kwargs):
            raise RuntimeError("db down")

        get_preference = get_trip_history = _fail
        get_chat_history = get_session_titles = _fail
        get_pinned_sessions = get_archived_sessions = _fail
        get_statistics = _fail

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False: _BrokenStore()
    )
    client = _client()
    for path in (
        "/api/sessions",
        "/api/sessions/s1",
        "/api/sessions/s1/export",
        "/api/status",
    ):
        response = client.get(path, params={"user_id": "u1"})
        assert response.status_code == 503, (path, response.text)
    print("✓ 会话读取失败统一 503")


def test_chat_rate_limit_ip_bucket():
    """用户 ID 由请求方提供、可轮换；同一客户端 IP 仍受总量桶约束。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=100, window_sec=60)  # 用户桶放宽
    state.ip_limiter = RateLimiter(limit=2, window_sec=60)      # IP 桶收紧
    client = TestClient(app)

    users = []
    try:
        for i in range(2):
            user_id = _unique_user(f"web_test_ip_{i}")
            users.append(user_id)
            response = client.post(
                "/api/chat",
                json={"message": "你好", "user_id": user_id, "session_id": f"sess-ip-{i}"},
            )
            assert response.status_code == 200
            # 额度头应描述更紧的 IP 桶，而不是用户桶的 100
            assert response.headers["x-ratelimit-limit"] == "2"

        rotated = _unique_user("web_test_ip_rotated")
        users.append(rotated)
        limited = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": rotated, "session_id": "sess-ip-rotated"},
        )
        assert limited.status_code == 429, "轮换用户 ID 不应绕过客户端 IP 限流"
        assert limited.headers["Retry-After"].isdigit()
        assert limited.headers["x-ratelimit-limit"] == "2"
        print("✓ 轮换用户 ID 仍受客户端 IP 限流约束")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_session_rename():
    client = _client()
    user_id = _unique_user("web_test_title")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-title"},
        )
        assert response.status_code == 200

        renamed = client.put(
            "/api/sessions/sess-title/title",
            json={"user_id": user_id, "title": "杭州出差计划"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "杭州出差计划"

        export = client.get(
            "/api/sessions/sess-title/export", params={"user_id": user_id}
        )
        assert export.status_code == 200
        assert export.text.startswith("# 杭州出差计划")

        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        assert sessions[0]["title"] == "杭州出差计划"

        hits = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "出差计划"}
        ).json()["sessions"]
        assert [item["session_id"] for item in hits] == ["sess-title"], "自定义标题应可被搜索"

        cleared = client.put(
            "/api/sessions/sess-title/title",
            json={"user_id": user_id, "title": ""},
        )
        assert cleared.json()["title"] == ""
        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        assert sessions[0]["title"] == "你好", "空标题应恢复自动标题"

        too_long = client.put(
            "/api/sessions/sess-title/title",
            json={"user_id": user_id, "title": "x" * 61},
        )
        assert too_long.status_code == 422
        print("✓ 会话重命名 / 标题搜索 / 恢复自动标题")
    finally:
        _cleanup(user_id)


def test_security_headers():
    client = _client()
    for path in ("/", "/api/health", "/static/theme.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        if path.startswith("/api/"):
            assert response.headers["cache-control"] == "no-store"
        else:
            assert "no-store" not in response.headers.get("cache-control", "")

    status_response = client.get("/api/status", params={"user_id": "web_test_cache"})
    assert status_response.headers["cache-control"] == "no-store"

    html = client.get("/").text
    assert "/static/theme.js" in html
    assert "<script>" not in html, "不应再使用内联脚本（CSP 禁止）"
    print("✓ 安全响应头（CSP / nosniff / frame / referrer / API no-store）")


def test_lock_table_is_bounded():
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    original = state.max_locks
    state.max_locks = 10
    try:
        for index in range(50):
            state.lock_for(f"lock-user-{index}")
        assert len(state.locks) <= 10, "用户锁表应有上限"
    finally:
        state.max_locks = original
    print("✓ 用户锁表有上限（未持有的旧锁被淘汰）")


def test_lock_table_overflow_uses_shared_lock():
    """锁表满且全部在用（并发高峰）时，新用户共享兜底锁，表不再增长。"""
    import asyncio

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    original = state.max_locks
    state.max_locks = 2
    try:
        for index in range(2):
            lock = asyncio.Lock()
            # 模拟锁正在被持有：同步测试里没有可直接 acquire 的事件循环
            setattr(lock, "_locked", True)
            state.locks[f"held-{index}"] = lock
        assert state.lock_for("brand-new-user") is state.overflow_lock
        assert len(state.locks) == 2, "锁表占满时不能再为每个新用户建锁"
        assert state.lock_for("yet-another-user") is state.overflow_lock
        # 有锁释放后可以重新入表
        setattr(state.locks["held-0"], "_locked", False)
        fresh = state.lock_for("recovered-user")
        assert fresh is not state.overflow_lock
        assert "recovered-user" in state.locks
    finally:
        state.max_locks = original
        state.locks.clear()
    print("✓ 并发高峰下锁表保持有界（共享兜底锁）")


def test_lock_eviction_skips_waiting_lock():
    """有等待者的锁不能被淘汰：持有者刚 release 时等待者还没恢复执行。"""
    import asyncio

    async def scenario():
        state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
        state.max_locks = 1
        state.locks.clear()
        lock = state.lock_for("holder")
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)  # 等待者已排队但拿不到锁
        lock.release()  # 等待者即将恢复执行，此刻 locked() 为 False
        picked = state.lock_for("newcomer")
        assert picked is state.overflow_lock, "有等待者的锁不能被淘汰"
        assert "holder" in state.locks
        await waiter
        lock.release()

    asyncio.run(scenario())
    print("✓ 用户锁淘汰跳过有等待者的锁")


def test_lock_for_overflow_user_keeps_shared_lock_while_held():
    """兜底锁被占用期间，同一用户的新请求必须继续共用它，不能新建私有锁并行。"""
    import asyncio

    async def scenario():
        state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
        state.max_locks = 1
        state.locks.clear()
        blocker = state.lock_for("holder")
        await blocker.acquire()
        overflow = state.lock_for("u-overflow")
        assert overflow is state.overflow_lock, "锁表满时应使用兜底锁"
        await overflow.acquire()  # 模拟该用户的 R1 正持有兜底锁
        blocker.release()  # 锁表腾出空位
        again = state.lock_for("u-overflow")
        assert again is state.overflow_lock, "兜底锁仍被占用时不能给同一用户新建私有锁"
        overflow.release()
        fresh = state.lock_for("u-overflow")
        assert fresh is not state.overflow_lock
        assert "u-overflow" in state.locks

    asyncio.run(scenario())
    print("✓ 兜底锁占用期间同用户继续共用兜底锁")


def test_session_turn_lease_releases_once():
    """租约幂等释放：锁只 release 一次，会话占用只解除一次。"""
    import asyncio

    from webapp.server import _SessionTurnLease

    class _Session:
        def __init__(self):
            self.ended = 0

        def end_turn(self):
            self.ended += 1

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        session = _Session()
        lease = _SessionTurnLease(lock, session)
        lease.release()
        lease.release()
        assert lock.locked() is False
        assert session.ended == 1

    asyncio.run(scenario())
    print("✓ SSE 租约幂等释放锁与会话占用")


def test_leased_streaming_response_releases_even_if_body_never_starts():
    """响应在首次迭代前就失败（客户端提前断开）时，锁与会话占用也必须释放。"""
    import asyncio
    from typing import Any

    from webapp.server import _LeasedStreamingResponse, _SessionTurnLease

    class _Session:
        def __init__(self):
            self.ended = 0

        def end_turn(self):
            self.ended += 1

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        session = _Session()
        lease = _SessionTurnLease(lock, session)

        async def body():
            raise AssertionError("不应进入迭代")
            yield ""  # pragma: no cover

        async def send(message):
            raise OSError("client gone")

        async def receive():
            return {"type": "http.disconnect"}

        scope: Any = {"type": "http", "asgi": {"spec_version": "2.4"}}
        response = _LeasedStreamingResponse(body(), lease)
        with pytest.raises(Exception):
            await response(scope, receive, send)
        assert lock.locked() is False, "响应未开始迭代也不能泄漏用户锁"
        assert session.ended == 1

    asyncio.run(scenario())
    print("✓ 响应未开始迭代即失败时租约兜底释放")


def test_session_lru_skips_in_use_sessions():
    """LRU 淘汰不能关闭正在对话的会话，也不能淘汰本次要返回的会话。"""
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_sessions = 1
    closed = []

    busy = state.get_session("u-lru", "busy", True)
    busy.close = lambda: closed.append(busy)  # 实例属性覆盖 close

    idle = state.get_session("u-lru", "idle")
    assert len(state.sessions) == 2, "在用会话不能被淘汰（暂时超限）"
    assert idle not in closed and busy not in closed, "本次返回的会话也不能被淘汰"

    busy.end_turn()
    state.get_session("u-lru", "next")
    assert closed == [busy], "请求结束后最久未用的会话可被淘汰"
    assert len(state.sessions) == 1, "超出上限的其它空闲会话应继续回收"

    only = state.sessions[state._session_key("u-lru", "next")]
    only.begin_turn()
    state.get_session("u-lru", "third")
    assert len(state.sessions) == 2, "全部在用时应暂时超限而不是关闭"

    # 占用标记泄漏（如 SSE 响应未启动就断开）：超过上限时长后允许回收
    only._turn_started_at -= state.session_turn_leak_sec + 1
    state.get_session("u-lru", "fourth")
    assert state._session_key("u-lru", "next") not in state.sessions
    only.end_turn()
    print("✓ 会话 LRU 跳过在用会话 / 本次返回的会话")


def test_chat_releases_session_in_use_mark():
    """对话结束后必须解除在用标记，否则会话永远不会被回收。"""
    client = _client()
    user_id = _unique_user("web_test_turn_flag")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "turn-flag"},
        )
        assert response.status_code == 200
        state = client.app.state.aligo
        session = state.sessions[state._session_key(user_id, "turn-flag")]
        assert session.in_use() is False
        print("✓ 对话结束后解除会话在用标记")
    finally:
        _cleanup(user_id)


def test_request_body_too_large():
    client = _client()
    response = client.post(
        "/api/chat",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "99999999",
        },
    )
    assert response.status_code == 413
    assert "请求体过大" in response.json()["detail"]
    assert response.headers.get("x-request-id")
    assert response.headers["x-content-type-options"] == "nosniff"
    print("✓ 超大请求体返回 413（带安全头 / 请求 ID）")


def test_chunked_request_body_too_large():
    """分块传输没有 Content-Length，也要在读取时截断。"""
    client = _client()
    oversized = b"x" * (1024 * 1024 * 2 + 1)

    response = client.post(
        "/api/chat",
        content=[oversized],
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in response.request.headers, "用例应走分块传输"
    assert response.status_code == 413
    assert "请求体过大" in response.json()["detail"]
    assert response.headers.get("x-request-id")
    assert response.headers["x-content-type-options"] == "nosniff"
    print("✓ 分块请求体超过 2 MB 同样返回 413")


def test_chunked_request_body_allowed_under_limit():
    """未提供 Content-Length 的正常小请求不受影响。"""
    client = _client()
    user_id = _unique_user("web_test_chunked_ok")
    payload = json.dumps(
        {"message": "你好", "user_id": user_id, "session_id": "sess-chunked"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        response = client.post(
            "/api/chat",
            content=[payload],
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == "sess-chunked"
    finally:
        _cleanup(user_id)
    print("✓ 分块传输的小请求正常处理")


def test_gzip_compression_excludes_sse():
    """静态资源与 JSON 启用 gzip；SSE 流保持原样（压缩会破坏实时进度）。"""
    from context.storage import create_long_term_memory

    client = _client()
    static_gzip = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    assert static_gzip.status_code == 200
    assert static_gzip.headers.get("content-encoding") == "gzip"
    assert len(static_gzip.content) > 10_000

    static_plain = client.get(
        "/static/app.js", headers={"Accept-Encoding": "identity"}
    )
    assert "content-encoding" not in static_plain.headers
    assert static_plain.content == static_gzip.content

    user_id = _unique_user("web_test_gzip")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "长消息 " + "x" * 3000, "gzip-session")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        exported = client.get(
            "/api/export", params={"user_id": user_id}, headers={"Accept-Encoding": "gzip"}
        )
        assert exported.status_code == 200
        assert exported.headers.get("content-encoding") == "gzip"
        assert len(exported.content) > 3000
        plain_export = client.get(
            "/api/export",
            params={"user_id": user_id},
            headers={"Accept-Encoding": "identity"},
        )
        assert "content-encoding" not in plain_export.headers
        assert b"x" * 100 in plain_export.content
        assert b"x" * 100 in exported.content
        assert abs(len(plain_export.content) - len(exported.content)) < 64
    finally:
        _cleanup(user_id)

    sse_user = _unique_user("web_test_gzip_sse")
    try:
        stream = client.post(
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": sse_user,
                "session_id": uuid.uuid4().hex[:12],
            },
            headers={"Accept-Encoding": "gzip"},
        )
        assert stream.status_code == 200
        assert stream.headers.get("content-encoding") != "gzip"
        assert "text/event-stream" in stream.headers.get("content-type", "")
        assert "data:" in stream.text
    finally:
        _cleanup(sse_user)
    print("✓ gzip 压缩（静态 / JSON 启用，SSE 排除）")


def test_docs_csp_allows_cdn():
    """Swagger UI 需要从 CDN 加载资源：仅文档页面放宽 CSP，普通页面保持严格。"""
    client = _client()
    docs = client.get("/docs")
    assert docs.status_code == 200
    docs_csp = docs.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in docs_csp
    assert "script-src 'self'" in docs_csp

    home_csp = client.get("/").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in home_csp
    assert "script-src 'self'" in home_csp
    assert "frame-ancestors 'none'" in home_csp
    print("✓ 文档页面放宽 CSP，普通页面保持严格策略")


def test_unhandled_error_returns_500_with_headers():
    """未处理异常由中间件兜底：500 响应也要带安全头与请求 ID。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))

    @app.get("/api/_boom")
    async def boom():
        raise RuntimeError("boom")

    response = TestClient(app).get("/api/_boom", headers={"X-Request-ID": "boom-1"})
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "boom-1"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers
    assert "boom-1" in response.json()["detail"]

    # 客户端支持 gzip 时也必须有同样的安全头 / JSON 错误体
    gzip_response = TestClient(app).get(
        "/api/_boom",
        headers={"X-Request-ID": "boom-2", "Accept-Encoding": "gzip"},
    )
    assert gzip_response.status_code == 500
    assert gzip_response.headers["x-request-id"] == "boom-2"
    assert gzip_response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in gzip_response.headers
    assert "boom-2" in gzip_response.json()["detail"]
    print("✓ 未处理异常返回 500 且带安全响应头 / 请求 ID（含 gzip 请求）")


def test_export_markdown():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_md")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_preference("hotel_brands", ["汉庭"])
            store.save_preference("budget_level", 0)
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-11-09",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-md-export"},
        )
        client.post(
            "/api/chat",
            json={
                "message": "另一个问题",
                "user_id": user_id,
                "session_id": "sess-md-other",
            },
        )
        client.put(
            "/api/sessions/sess-md-export/archive",
            json={"user_id": user_id, "archived": True},
        )

        response = client.get("/api/export", params={"user_id": user_id, "format": "md"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers.get("content-disposition", "")
        text = response.text
        assert "# Aligo 用户数据导出" in text
        assert "hotel_brands：汉庭" in text
        assert "budget_level：0" in text
        assert "北京 → 上海" in text
        assert "## 最近会话" in text and "你好" in text
        assert "（已归档）" in text
        assert "归档会话：1" in text
        assert "另一个问题（2 条消息）" in text
        assert "另一个问题（2 条消息）（已归档）" not in text, "未归档会话不应带标记"

        invalid = client.get("/api/export", params={"user_id": user_id, "format": "pdf"})
        assert invalid.status_code == 422
        print("✓ 用户数据导出 Markdown（JSON 仍为默认）")
    finally:
        _cleanup(user_id)


def test_user_data_import_api():
    from context.storage import create_long_term_memory

    source = _unique_user("web_test_import_src")
    target = _unique_user("web_test_import_dst")
    try:
        store = create_long_term_memory(source, str(MEMORY_DIR))
        try:
            store.save_preference("home_location", "上海")
            store.save_trip_history(
                {
                    "origin": "上海",
                    "destination": "深圳",
                    "start_date": "2026-12-05",
                    "end_date": "2026-12-07",
                    "purpose": "展会",
                }
            )
            store.add_chat_message(
                "user", "帮我安排深圳的行程", "sess-imp", timestamp="2026-11-02T08:30:00"
            )
            store.add_chat_message(
                "assistant",
                '{"reply":"好的，已安排"}',
                "sess-imp",
                timestamp="2026-11-02T08:30:06",
            )
            store.set_session_title("sess-imp", "深圳展会")
            store.set_session_pinned("sess-imp", True)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        backup = client.get("/api/export", params={"user_id": source}).json()
        response = client.post(
            "/api/import", params={"user_id": target}, json={"backup": backup}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["preferences"] == 1
        assert payload["trips_created"] == 1 and payload["trips_updated"] == 0
        assert payload["messages_added"] == 2 and payload["messages_skipped"] == 0
        assert payload["titles"] == 1 and payload["pinned"] == 1
        assert payload["skipped"] == []

        # 会话可读、标题/置顶生效、时间戳保真
        sessions = client.get("/api/sessions", params={"user_id": target}).json()
        restored = next(
            (s for s in sessions["sessions"] if s["session_id"] == "sess-imp"), None
        )
        assert restored is not None, sessions
        assert restored["title"] == "深圳展会" and restored["pinned"] is True
        detail = client.get("/api/sessions/sess-imp", params={"user_id": target}).json()
        stamps = [m["timestamp"] for m in detail["messages"]]
        assert stamps[0].startswith("2026-11-02T08:30:00"), stamps
        assert stamps[1].startswith("2026-11-02T08:30:06")
        status = client.get("/api/status", params={"user_id": target}).json()
        assert status["preferences"]["home_location"] == "上海"

        # 重复导入幂等
        again = client.post(
            "/api/import", params={"user_id": target}, json={"backup": backup}
        ).json()
        assert again["messages_added"] == 0 and again["messages_skipped"] == 2
        assert again["trips_created"] == 0 and again["trips_updated"] == 1

        # 没有可恢复数据 / 非法用户
        empty = client.post(
            "/api/import",
            params={"user_id": target},
            json={"backup": {"preferences": {}}},
        )
        assert empty.status_code == 400
        assert "没有可恢复的数据" in empty.json()["detail"]
        bad_user = client.post(
            "/api/import", params={"user_id": "bad id!"}, json={"backup": backup}
        )
        assert bad_user.status_code == 422
        print("✓ /api/import 恢复备份（会话/时间戳/幂等/错误路径）")
    finally:
        _cleanup(source)
        _cleanup(target)


def test_user_import_large_backup_over_2mb():
    """回归：/api/import 必须能接收 2 MB 以上的合法备份（面板上限 50 MB）。

    此前全局 2 MB 请求体上限会让正常规模备份（几百条消息）直接 413，
    与面板预检 / README 的 50 MB 承诺矛盾。
    """
    target = _unique_user("web_test_import_big")
    try:
        payload = {
            "backup": {
                "user_id": "big-src",
                "chat_history": [
                    {
                        "role": "user",
                        "content": "x" * 2_500_000,
                        "session_id": "sess-big",
                        "timestamp": "2026-11-03T09:00:00",
                    }
                ],
            }
        }
        client = _client()
        response = client.post(
            "/api/import", params={"user_id": target}, json=payload
        )
        assert response.status_code == 200, response.text[:200]
        result = response.json()
        assert result["messages_added"] == 1
        detail = client.get("/api/sessions/sess-big", params={"user_id": target}).json()
        assert len(detail["messages"]) == 1
        assert len(detail["messages"][0]["content"]) == 2_500_000
        print("✓ /api/import 支持 2 MB 以上的备份")
    finally:
        _cleanup(target)


def test_session_pin():
    client = _client()
    user_id = _unique_user("web_test_pin")
    try:
        for session_id in ("sess-pin-a", "sess-pin-b"):
            response = client.post(
                "/api/chat",
                json={"message": "你好", "user_id": user_id, "session_id": session_id},
            )
            assert response.status_code == 200

        pinned = client.put(
            "/api/sessions/sess-pin-a/pin",
            json={"user_id": user_id, "pinned": True},
        )
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True

        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        assert sessions[0]["session_id"] == "sess-pin-a", "置顶会话应排在最前"
        assert sessions[0]["pinned"] is True
        assert all(item["pinned"] is False for item in sessions[1:])

        unpinned = client.put(
            "/api/sessions/sess-pin-a/pin",
            json={"user_id": user_id, "pinned": False},
        )
        assert unpinned.json()["pinned"] is False
        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()["sessions"]
        assert all(item["pinned"] is False for item in sessions)
        print("✓ 会话置顶 / 取消置顶与排序")
    finally:
        _cleanup(user_id)


def test_trip_calendar_export():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_ics")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-11-09",
                    "end_date": "2026-11-11",
                    "purpose": "出差",
                    "session_id": "sess-ics",
                }
            )
            store.save_trip_history(
                {"origin": "北京", "destination": "上海", "purpose": "旅游"}
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-11-11",
                    "end_date": "2026-11-09",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        response = client.get(
            "/api/trips/trip_1/calendar", params={"user_id": user_id}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert "attachment" in response.headers.get("content-disposition", "")
        text = response.text
        assert "BEGIN:VCALENDAR" in text
        assert "DTSTART;VALUE=DATE:20261109" in text
        assert "DTEND;VALUE=DATE:20261112" in text, "全天事件结束日期应为次日（exclusive）"
        assert "北京 → 杭州" in text

        again = client.get("/api/trips/trip_1/calendar", params={"user_id": user_id})
        uid_first = next(line for line in text.splitlines() if line.startswith("UID:"))
        uid_second = next(line for line in again.text.splitlines() if line.startswith("UID:"))
        assert uid_first == uid_second, "同一行程的 UID 应稳定"

        reversed_range = client.get(
            "/api/trips/trip_3/calendar", params={"user_id": user_id}
        )
        assert reversed_range.status_code == 200
        assert "DTSTART;VALUE=DATE:20261111" in reversed_range.text
        assert "DTEND;VALUE=DATE:20261112" in reversed_range.text

        missing = client.get("/api/trips/trip_999/calendar", params={"user_id": user_id})
        assert missing.status_code == 404

        no_date = client.get("/api/trips/trip_2/calendar", params={"user_id": user_id})
        assert no_date.status_code == 400
        print("✓ 行程导出 iCalendar（含日期校验）")
    finally:
        _cleanup(user_id)


def test_knowledge_endpoint():
    data = _client().get("/api/knowledge").json()
    assert "available" in data and "collection" in data
    if data["available"]:
        assert data["documents"] >= 0
    else:
        assert data.get("reason")
    print("✓ /api/knowledge 返回知识库状态")


def test_doctor_endpoint():
    response = _client().get("/api/doctor")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"ok", "fail"}
    names = [check["name"] for check in data["checks"]]
    assert "Python 版本" in names
    assert "RAG 知识库" in names
    assert all(
        check["status"] in {"ok", "warn", "fail", "skip"} for check in data["checks"]
    )
    llm = next(check for check in data["checks"] if check["name"] == "LLM 连通性")
    assert llm["status"] == "skip", "默认不发起网络检查"
    print("✓ /api/doctor 返回自检结果（默认不请求网络）")


def test_knowledge_documents_api(monkeypatch):
    """知识片段列表接口：透传分页 / 搜索 / 分类参数（后端 Milvus 由假实现替代）。"""
    from webapp import server as server_module

    captured = {}

    def fake_list(limit, offset, query, category=""):
        captured.update(
            limit=limit, offset=offset, query=query, category=category
        )
        return {"total": 1, "items": [{"id": 7, "content": "住宿标准", "metadata": {}}]}

    monkeypatch.setattr(server_module, "list_documents", fake_list)
    response = _client().get(
        "/api/knowledge/documents",
        params={"limit": 10, "offset": 0, "q": "住宿", "category": "住宿"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == 7
    assert captured == {
        "limit": 10,
        "offset": 0,
        "query": "住宿",
        "category": "住宿",
    }

    def fake_categories():
        return [{"category": "住宿", "count": 2}]
    monkeypatch.setattr(server_module, "list_categories", fake_categories)
    summary = _client().get("/api/knowledge/categories")
    assert summary.status_code == 200
    assert summary.json()["categories"] == [{"category": "住宿", "count": 2}]

    def boom():
        raise RuntimeError("collection is in state 'released'")

    monkeypatch.setattr(server_module, "list_categories", boom)
    assert _client().get("/api/knowledge/categories").status_code == 503
    print("✓ /api/knowledge/documents 列表接口（分页 / 搜索 / 分类）与分类统计")


def test_knowledge_document_write_api(monkeypatch):
    """知识片段新增 / 删除接口：参数校验、错误映射与 404。"""
    from webapp import server as server_module

    calls = {}

    def fake_add(content, metadata):
        calls["add"] = (content, metadata)
        return {"status": "success", "id": 7, "total_count": 1}

    monkeypatch.setattr(server_module, "add_document", fake_add)
    client = _client()

    created = client.post(
        "/api/knowledge/documents",
        json={"content": "新知识", "metadata": {"category": "测试"}},
    )
    assert created.status_code == 201
    assert created.json()["id"] == 7
    assert calls["add"] == ("新知识", {"category": "测试"})

    # 空内容由 Pydantic 拦截
    assert client.post("/api/knowledge/documents", json={"content": ""}).status_code == 422

    # 业务校验失败 → 400
    def fake_add_invalid(content, metadata):
        raise ValueError("知识内容不能为空")

    monkeypatch.setattr(server_module, "add_document", fake_add_invalid)
    invalid = client.post("/api/knowledge/documents", json={"content": "   "})
    assert invalid.status_code == 400

    deleted = {}

    def fake_delete(doc_id, whole_group=False):
        deleted["id"] = doc_id
        deleted["group"] = whole_group
        return doc_id in {7, 9}

    monkeypatch.setattr(server_module, "delete_document", fake_delete)
    ok = client.delete("/api/knowledge/documents/7")
    assert ok.status_code == 200 and ok.json()["deleted"] is True
    assert deleted == {"id": 7, "group": False}
    # group=1 删除整组分片
    grouped = client.delete("/api/knowledge/documents/9?group=1")
    assert grouped.status_code == 200 and grouped.json()["group"] is True
    assert deleted == {"id": 9, "group": True}
    assert client.delete("/api/knowledge/documents/8").status_code == 404

    updated = {}

    def fake_update(doc_id, content, metadata):
        updated["call"] = (doc_id, content, metadata)
        return {"status": "success", "id": doc_id}

    monkeypatch.setattr(server_module, "update_document", fake_update)
    changed = client.put(
        "/api/knowledge/documents/7",
        json={"content": "更新后的知识", "metadata": {"category": "交通"}},
    )
    assert changed.status_code == 200
    assert changed.json()["id"] == 7
    assert updated["call"] == (7, "更新后的知识", {"category": "交通"})
    # 更新允许整份文档（≤20000 字，整组编辑用）；新增仍限制 2000 字
    long_update = client.put(
        "/api/knowledge/documents/7", json={"content": "长" * 15000}
    )
    assert long_update.status_code == 200
    assert len(updated["call"][1]) == 15000
    assert (
        client.post(
            "/api/knowledge/documents", json={"content": "长" * 2001}
        ).status_code
        == 422
    )
    assert (
        client.put("/api/knowledge/documents/7", json={"content": ""}).status_code
        == 422
    )
    # 整份编辑回传的派生分片元数据（part/parts/group）在校验前被丢弃：
    # 8 个用户键 + 3 个派生键不应 422；11 个用户键仍被拒绝
    derived = {f"k{index}": "v" for index in range(8)}
    derived.update({"part": "1", "parts": "2", "group": "g"})
    accepted = client.put(
        "/api/knowledge/documents/7", json={"content": "x", "metadata": derived}
    )
    assert accepted.status_code == 200
    assert "part" not in updated["call"][2] and "group" not in updated["call"][2]
    too_many_keys = {f"k{index}": "v" for index in range(11)}
    assert (
        client.put(
            "/api/knowledge/documents/7",
            json={"content": "x", "metadata": too_many_keys},
        ).status_code
        == 422
    )
    assert (
        client.put("/api/knowledge/documents/0", json={"content": "x"}).status_code
        == 422
    )

    def fake_update_missing(doc_id, content, metadata):
        raise LookupError("知识片段不存在")

    monkeypatch.setattr(server_module, "update_document", fake_update_missing)
    assert (
        client.put(
            "/api/knowledge/documents/99", json={"content": "不存在"}
        ).status_code
        == 404
    )
    print("✓ /api/knowledge/documents 新增 / 更新 / 删除接口")


def test_knowledge_group_document_api(monkeypatch):
    """整组文档回填接口：整篇内容 / 404 / 503。"""
    from webapp import server as server_module

    client = _client()
    monkeypatch.setattr(
        server_module,
        "get_document_group",
        lambda doc_id: (
            {"id": doc_id, "group": "g", "parts": 2, "content": "第一段\n第二段"}
            if doc_id == 7
            else None
        ),
    )
    ok = client.get("/api/knowledge/documents/7/group")
    assert ok.status_code == 200
    assert ok.json()["content"] == "第一段\n第二段"
    assert ok.json()["parts"] == 2
    assert client.get("/api/knowledge/documents/8/group").status_code == 404

    def boom(doc_id):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(server_module, "get_document_group", boom)
    assert client.get("/api/knowledge/documents/7/group").status_code == 503
    print("✓ /api/knowledge/documents/{id}/group 整组回填")


def test_knowledge_search_api(monkeypatch):
    """语义检索接口：参数校验、结果透传与 503 映射。"""
    from webapp import server as server_module

    calls = {}

    def fake_search(query, top_k):
        calls["args"] = (query, top_k)
        return [
            {
                "id": 7,
                "content": "住宿超标准需提前审批",
                "metadata": {"category": "住宿"},
                "distance": 0.87,
            }
        ]

    monkeypatch.setattr(server_module, "search_documents", fake_search)
    client = _client()
    response = client.get(
        "/api/knowledge/search", params={"q": "住宿超标准", "top_k": 5}
    )
    assert response.status_code == 200
    assert calls["args"] == ("住宿超标准", 5)
    payload = response.json()
    assert payload["query"] == "住宿超标准"
    assert payload["results"][0]["distance"] == 0.87

    assert client.get("/api/knowledge/search", params={"q": ""}).status_code == 422
    calls.clear()
    blank = client.get("/api/knowledge/search", params={"q": "   "})
    assert blank.status_code == 422, "纯空白应判为客户端输入错误（422）"
    assert calls == {}, "空白查询不应触发检索（也不应消耗限流额度）"
    assert (
        client.get(
            "/api/knowledge/search", params={"q": "住宿", "top_k": 0}
        ).status_code
        == 422
    )

    def boom(query, top_k):
        raise RuntimeError("collection released")

    monkeypatch.setattr(server_module, "search_documents", boom)
    assert (
        client.get("/api/knowledge/search", params={"q": "住宿"}).status_code == 503
    )

    def missing(query, top_k):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "search_documents", missing)
    missing_response = client.get("/api/knowledge/search", params={"q": "住宿"})
    assert missing_response.status_code == 503
    assert "未初始化" in missing_response.json()["detail"]
    print("✓ /api/knowledge/search 语义检索（参数 / 结果 / 503）")


def test_knowledge_rate_limit_is_separate_bucket(monkeypatch):
    """知识库高开销接口按 IP 独立限流，不占用对话额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "add_document",
        lambda content, metadata: {"status": "success", "id": 1, "total_count": 1},
    )
    monkeypatch.setattr(server_module, "search_documents", lambda q, top_k: [])
    monkeypatch.setattr(
        server_module,
        "list_documents",
        lambda **kwargs: {"total": 0, "items": []},
    )
    monkeypatch.setattr(server_module, "list_categories", lambda: [])

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.kb_limiter = RateLimiter(limit=2, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_kb_limit")
    try:
        listed = client.get("/api/knowledge/documents")
        assert listed.status_code == 200, "列表也应使用知识库限流桶（带额度头）"
        assert listed.headers["x-ratelimit-limit"] == "2"
        assert listed.headers["x-ratelimit-remaining"] == "1"

        first = client.post("/api/knowledge/documents", json={"content": "测试知识"})
        assert first.status_code == 201
        assert first.headers["x-ratelimit-remaining"] == "0"

        categories = client.get("/api/knowledge/categories")
        assert categories.status_code == 429, "分类统计应共享知识库桶"

        second = client.post("/api/knowledge/documents", json={"content": "第二条"})
        assert second.status_code == 429
        assert "Retry-After" in second.headers
        assert second.headers["x-ratelimit-remaining"] == "0"

        search = client.get("/api/knowledge/search", params={"q": "住宿"})
        assert search.status_code == 429, "写入与检索应共享知识库桶"

        export = client.get("/api/knowledge/export")
        assert export.status_code == 429, "导出也应使用知识库限流桶"

        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "kb-limit"},
        )
        assert chat.status_code == 200, "知识库限流不应影响对话额度"
        print("✓ 知识库接口独立限流（列表 / 分类 / 写入 / 检索共享，不影响对话）")
    finally:
        _cleanup(user_id)


def test_knowledge_eval_api(monkeypatch):
    """检索评测接口：返回 hit@K / MRR，带额度头；知识库不可用返回 503。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    runs = []

    class FakeAgent:
        def __init__(self, collection_name):
            self.collection_name = collection_name
            self.initialized = True

        def search_knowledge(self, query, top_k=5, strict=False):
            runs.append(strict)
            return [{"content": query}]

    def fake_evaluate(search):
        search("测试", 5)
        return {
            "total": 14,
            "hit1": 0.64,
            "hit3": 0.93,
            "hit5": 1.0,
            "mrr": 0.80,
            "rows": [],
        }

    monkeypatch.setattr("agents.rag_knowledge_agent.RAGKnowledgeAgent", FakeAgent)
    monkeypatch.setattr("utils.rag_eval.evaluate_retrieval", fake_evaluate)
    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda: {
            "available": True,
            "collection": "kb",
            "documents": 141,
            "path": "p",
        },
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    client = TestClient(app)

    response = client.get("/api/knowledge/eval")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 14
    assert data["hit5"] == 1.0
    assert "x-ratelimit-limit" in response.headers
    assert runs == [True], "评测必须用 strict=True 区分空结果与检索失败"

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda: {"available": False, "reason": "知识库未初始化", "path": "p"},
    )
    assert client.get("/api/knowledge/eval").status_code == 503

    token = state.eval_lock.acquire()
    assert token
    try:
        busy = client.get("/api/knowledge/eval")
        assert busy.status_code == 429
        assert "Retry-After" in busy.headers
    finally:
        state.eval_lock.release(token)

    # 并发锁拒绝不应消耗知识库额度：恢复知识库后仍可再评测一次（额度 3/3）
    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda: {
            "available": True,
            "collection": "kb",
            "documents": 141,
            "path": "p",
        },
    )
    assert client.get("/api/knowledge/eval").status_code == 200
    assert len(runs) == 2 and all(runs)
    limited = client.get("/api/knowledge/eval")
    assert limited.status_code == 429
    assert limited.headers["x-ratelimit-remaining"] == "0"

    # 共享锁在评测期间失效（心跳续期返回 0）时，结果必须打标；用假锁模拟
    # 真实时序（真锁 acquire 成功会清零 lost，直接置位不算覆盖）
    class _LostEvalLock:
        lost = True
        degraded = False

        def acquire(self):
            return "token"

        def release(self, token):
            pass

    state.eval_lock = _LostEvalLock()
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)  # 重置额度
    warned = client.get("/api/knowledge/eval")
    assert warned.status_code == 200
    assert "warning" in warned.json()

    # 配置了 Redis 但加锁回退进程内（degraded）同样不能当作独占评测
    class _DegradedEvalLock(_LostEvalLock):
        lost = False
        degraded = True

    state.eval_lock = _DegradedEvalLock()
    monkeypatch.setitem(
        server_module.STORAGE_CONFIG, "redis_url", "redis://localhost:6379/0"
    )
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    degraded = client.get("/api/knowledge/eval")
    assert degraded.status_code == 200 and "warning" in degraded.json()

    # 未配置 Redis 时进程内锁是正常模式，不应误报
    monkeypatch.setitem(server_module.STORAGE_CONFIG, "redis_url", "")
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    single = client.get("/api/knowledge/eval")
    assert single.status_code == 200 and "warning" not in single.json()
    print("✓ /api/knowledge/eval 检索评测（结果 / 503 / 额度头 / 并发 429 不耗额度）")


def test_knowledge_eval_import_failure(monkeypatch):
    """技能文件缺失时评测依赖 import 失败，应返回 503 而不是 500。"""
    import sys
    import types

    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda: {"available": True, "collection": "kb", "path": "p"},
    )
    monkeypatch.setitem(
        sys.modules, "agents.rag_knowledge_agent", types.ModuleType("agents.rag_knowledge_agent")
    )
    client = _client()
    response = client.get("/api/knowledge/eval")
    assert response.status_code == 503
    assert "评测依赖不可用" in response.json()["detail"]
    print("✓ 评测依赖缺失返回 503")


def test_knowledge_api_maps_backend_errors(monkeypatch):
    """Milvus 查询 / 写入失败应映射为 503，而不是 200 空库或 404。"""
    from webapp import server as server_module

    def boom(*args, **kwargs):
        raise RuntimeError("collection is in state 'released'")

    monkeypatch.setattr(server_module, "list_documents", boom)
    monkeypatch.setattr(server_module, "delete_document", boom)
    client = _client()
    assert client.get("/api/knowledge/documents").status_code == 503
    assert client.delete("/api/knowledge/documents/1").status_code == 503

    def missing_db(*args, **kwargs):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "add_document", missing_db)
    response = client.post("/api/knowledge/documents", json={"content": "新知识"})
    assert response.status_code == 503
    assert "初始化" in response.json()["detail"]
    print("✓ 知识接口把后端故障映射为 503")


def test_knowledge_metadata_limits():
    """metadata 键数与键值长度受限，防止超大对象拖垮列表接口。"""
    client = _client()
    too_many = {f"k{i}": "v" for i in range(11)}
    response = client.post(
        "/api/knowledge/documents", json={"content": "x", "metadata": too_many}
    )
    assert response.status_code == 422

    too_long = {"category": "长" * 201}
    response = client.post(
        "/api/knowledge/documents", json={"content": "x", "metadata": too_long}
    )
    assert response.status_code == 422
    print("✓ 知识片段 metadata 键数 / 长度受限")


def test_kb_delete_grouped_fragment_maps_400(monkeypatch):
    """带 group 的分片不能单条删除：服务端 ValueError 映射为 400。"""
    from webapp import server as server_module

    def grouped(doc_id, whole_group=False):
        raise ValueError(
            "该片段属于多分片知识内容，不能单条删除：请整组删除"
        )

    monkeypatch.setattr(server_module, "delete_document", grouped)
    response = _client().delete("/api/knowledge/documents/1")
    assert response.status_code == 400
    assert "整组删除" in response.json()["detail"]
    print("✓ 多分片片段单条删除 → 400（提示整组删除）")


def test_knowledge_transfer_api(monkeypatch):
    """知识库导出 / 导入接口：下载头、参数校验与导入数量映射。"""
    from webapp import server as server_module

    client = _client()

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [{"content": "a", "metadata": {"category": "测试"}}],
    )
    exported = client.get("/api/knowledge/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    payload = exported.json()
    assert payload["documents"] == [{"content": "a", "metadata": {"category": "测试"}}]
    assert payload["version"] == 1

    # 空内容条目被跳过而不是整批 422（与 CLI 行为一致）
    empty_item = client.post(
        "/api/knowledge/import", json={"documents": [{"content": ""}]}
    )
    assert empty_item.status_code == 200
    assert empty_item.json() == {
        "imported": 0,
        "skipped": 1,
        "skipped_invalid": 1,
        "skipped_duplicate": 0,
    }

    calls = {}

    def fake_import(documents, dry_run=False):
        calls["documents"] = documents
        calls["dry_run"] = dry_run
        if dry_run:
            return {"imported": 0, "skipped": 0, "would_import": len(documents)}
        return {"imported": len(documents), "skipped": 0}

    monkeypatch.setattr(server_module, "import_documents", fake_import)
    preview = client.post(
        "/api/knowledge/import?dry_run=1",
        json={"documents": [{"content": "y"}]},
    )
    assert preview.status_code == 200
    assert preview.json()["would_import"] == 1
    assert calls["dry_run"] is True

    # 预览端点一次接收整个备份（不受导入 200 条上限），只透传 dry_run
    preview_many = client.post(
        "/api/knowledge/import/preview",
        json={"documents": [{"content": f"p{index}"} for index in range(250)]},
    )
    assert preview_many.status_code == 200
    assert preview_many.json()["would_import"] == 250
    assert calls["dry_run"] is True
    assert len(calls["documents"]) == 250
    imported = client.post(
        "/api/knowledge/import",
        json={"documents": [{"content": "x", "metadata": {"category": "c"}}]},
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    assert calls["documents"] == [{"content": "x", "metadata": {"category": "c"}}]

    # 条目不是对象（null）时交给 import_documents 跳过，不能整批 422
    mixed = client.post(
        "/api/knowledge/import",
        json={"documents": [{"content": "x"}, None]},
    )
    assert mixed.status_code == 200
    assert calls["documents"] == [{"content": "x"}, None]

    assert (
        client.post("/api/knowledge/import", json={"documents": []}).status_code == 422
    )
    too_many = [{"content": "x"} for _ in range(20001)]
    assert (
        client.post("/api/knowledge/import", json={"documents": too_many}).status_code
        == 422
    )

    # 备份还原超限 → 400（不能静默截断，也不能误报为 503）
    def too_big(documents, dry_run=False):
        raise ValueError("备份中的文档超过还原上限（20000 字），请拆分后导入")

    monkeypatch.setattr(server_module, "import_documents", too_big)
    oversized = client.post(
        "/api/knowledge/import", json={"documents": [{"content": "x"}]}
    )
    assert oversized.status_code == 400
    assert "还原上限" in oversized.json()["detail"]
    print("✓ /api/knowledge/export 与 /api/knowledge/import")


def test_kb_import_error_mapping(monkeypatch):
    """知识导入 / 预览的错误映射：库未初始化 503、后端异常 503、超限 400。"""
    from webapp import server as server_module

    client = _client()

    def missing(documents, dry_run=False):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "import_documents", missing)
    for path in ("/api/knowledge/import", "/api/knowledge/import/preview"):
        response = client.post(path, json={"documents": [{"content": "x"}]})
        assert response.status_code == 503, path
        assert "先运行初始化脚本" in response.json()["detail"]

    def backend_boom(documents, dry_run=False):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(server_module, "import_documents", backend_boom)
    imported = client.post(
        "/api/knowledge/import", json={"documents": [{"content": "x"}]}
    )
    assert imported.status_code == 503
    assert "导入失败" in imported.json()["detail"]
    preview = client.post(
        "/api/knowledge/import/preview", json={"documents": [{"content": "x"}]}
    )
    assert preview.status_code == 503
    assert "预览失败" in preview.json()["detail"]

    def invalid(documents, dry_run=False):
        raise ValueError("备份中的文档不完整")

    monkeypatch.setattr(server_module, "import_documents", invalid)
    response = client.post(
        "/api/knowledge/import/preview", json={"documents": [{"content": "x"}]}
    )
    assert response.status_code == 400
    assert "不完整" in response.json()["detail"]
    print("✓ 知识导入 / 预览错误映射（503 / 400）")


def test_kb_group_error_mapping(monkeypatch):
    """整篇回填接口的错误映射：库未初始化 / 读取失败都返回 503。"""
    from webapp import server as server_module

    client = _client()

    def missing(doc_id):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "get_document_group", missing)
    response = client.get("/api/knowledge/documents/1/group")
    assert response.status_code == 503
    assert "先运行初始化脚本" in response.json()["detail"]

    def boom(doc_id):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(server_module, "get_document_group", boom)
    response = client.get("/api/knowledge/documents/1/group")
    assert response.status_code == 503
    assert "读取失败" in response.json()["detail"]
    print("✓ 知识整篇回填错误映射（503）")


def test_next_trip_from_full_history():
    from datetime import datetime, timedelta

    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_next")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            future = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": future,
                    "session_id": "sess-next",
                }
            )
            for index in range(6):
                past_day = (datetime.now() - timedelta(days=1 + index)).strftime(
                    "%Y-%m-%d"
                )
                store.save_trip_history(
                    {"origin": "上海", "destination": "南京", "start_date": past_day}
                )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        status = _client().get("/api/status", params={"user_id": user_id}).json()
        assert status["next_trip"]["session_id"] == "sess-next"
        assert status["next_trip"]["start_date"] == future
        assert len(status["trips"]) == 5, "列表仍只返回最近 5 条"
        print("✓ next_trip 基于完整行程历史计算")
    finally:
        _cleanup(user_id)


def test_usage_by_day_local_date():
    """by_day 把 UTC 时间戳按本地日期分桶（固定时区 + 跨日时间戳）。

    此前期望值用同一 astimezone 规则现算：UTC 的 CI 上删掉转换仍通过。
    改为固定 TZ=Asia/Shanghai，喂「昨天 20:00 UTC」（= 东八区今天 04:00）：
    正确实现把 5 tokens 记到今天，删掉转换会记到昨天——用桶内令牌数区分。
    Windows 没有 time.tzset，跳过（CI 在 Linux 上校验）。
    """
    import time as time_module
    from datetime import datetime, timedelta, timezone

    if not hasattr(time_module, "tzset"):
        pytest.skip("当前平台不支持动态时区（time.tzset）")
    from webapp.server import _usage_summary

    utc_evening = datetime.now(timezone.utc).replace(
        hour=20, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    local_day = (utc_evening + timedelta(hours=8)).date().isoformat()
    utc_day = utc_evening.date().isoformat()
    meta = {
        "usage": {"calls": 1, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "elapsed": 1.0,
        "agents": [],
    }
    history = [
        {
            "role": "assistant",
            "content": json.dumps({"results": [], "_meta": meta}, ensure_ascii=False),
            "timestamp": utc_evening.isoformat(),
            "session_id": "s",
        }
    ]
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time_module.tzset()
    try:
        usage = _usage_summary(history)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time_module.tzset()
    buckets = {day["date"]: day["tokens"] for day in usage["by_day"]}
    assert buckets.get(local_day) == 5, f"应记到东八区日期 {local_day}: {buckets}"
    assert buckets.get(utc_day) == 0, "UTC 日期桶不应计入（缺少时区转换）"
    print("✓ by_day 使用本地日期（固定时区跨日时间戳）")


def test_usage_summary_avg_and_top_agents_exact():
    """avg_elapsed / top_agents 必须由元数据真实计算，而不是恒 0 / 空列表。"""
    from webapp.server import _usage_summary

    def _assistant(meta: dict) -> dict:
        return {
            "role": "assistant",
            "content": json.dumps(
                {"results": [], "_meta": meta}, ensure_ascii=False
            ),
            "timestamp": "2026-11-04T12:00:00+08:00",
            "session_id": "s",
        }

    usage_meta = {
        "calls": 1,
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    history = [
        _assistant(
            {
                "usage": usage_meta,
                "elapsed": 1.0,
                "agents": [{"name": "x"}, {"name": "y"}],
            }
        ),
        _assistant(
            {"usage": usage_meta, "elapsed": 3.0, "agents": [{"name": "x"}]}
        ),
    ]
    usage = _usage_summary(history)
    assert usage["avg_elapsed"] == 2.0, "平均耗时应为 (1+3)/2"
    assert usage["top_agents"] == [
        {"name": "x", "count": 2},
        {"name": "y", "count": 1},
    ], usage["top_agents"]
    assert usage["calls"] == 2 and usage["total_tokens"] == 10
    print("✓ usage 汇总的均值与 top_agents 为精确值")


def test_invalid_user_id_rejected():
    response = _client().post("/api/chat", json={"message": "你好", "user_id": "../etc"})
    assert response.status_code == 422
    print("✓ 非法用户 ID 被拒绝（防止路径穿越）")


def test_export_attachment():
    response = _client().get("/api/export", params={"user_id": "web_test_user"})
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    payload = json.loads(response.text)
    assert payload["user_id"] == "web_test_user"
    print("✓ /api/export 返回可下载 JSON")


def _seed_trip_file(user_id, trips):
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.write_text(
        json.dumps({"trip_history": trips}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _clean_trip_file(path):
    path.unlink(missing_ok=True)
    lock = Path(str(path) + ".lock")
    lock.unlink(missing_ok=True)


def test_create_trip_duplicate_ids_report_other_overlap():
    """重复 trip_id 历史下，去重更新返回已有 id 时不隐藏同 id 其它行程的重叠。"""
    user_id = _unique_user("web_trip_dup")
    path = _seed_trip_file(
        user_id,
        [
            {
                "trip_id": "trip_9",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "出差",
            },
            {
                "trip_id": "trip_9",
                "origin": "北京",
                "destination": "广州",
                "start_date": "2026-12-02",
                "end_date": "2026-12-05",
                "purpose": "出差",
            },
        ],
    )
    try:
        response = _client().post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "会议",
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["created"] is False and payload["trip_id"] == "trip_9"
        destinations = [item["destination"] for item in payload["conflicts"]]
        assert "广州" in destinations, "同 id 的其它行程不能被连带排除"
        assert "上海" not in destinations, "本次保存的记录应被排除"
    finally:
        _clean_trip_file(path)
    print("✓ 重复 trip_id 下创建汇报其它行程重叠")


def test_update_trip_invalid_legacy_end_date_keeps_conflicts():
    """历史行程的返程日期非法时，更新响应不能把已解析的出发日期一并置空。"""
    user_id = _unique_user("web_trip_legacy_end")
    path = _seed_trip_file(
        user_id,
        [
            {
                "trip_id": "trip_1",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "未知",
                "purpose": "出差",
            },
            {
                "trip_id": "trip_2",
                "origin": "北京",
                "destination": "广州",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
                "purpose": "出差",
            },
        ],
    )
    try:
        response = _client().put(
            "/api/trips/trip_1", json={"user_id": user_id, "destination": "杭州"}
        )
        assert response.status_code == 200, response.text
        conflicts = response.json()["conflicts"]
        assert [item["trip_id"] for item in conflicts] == ["trip_2"], (
            "出发日期已解析，重叠不能被非法返程日期清空"
        )
    finally:
        _clean_trip_file(path)
    print("✓ 历史非法返程日期不清空更新响应的冲突")


if __name__ == "__main__":
    test_index_page()
    test_health_endpoint()
    test_knowledge_endpoint()
    test_doctor_endpoint()
    test_create_trip_duplicate_ids_report_other_overlap()
    test_update_trip_invalid_legacy_end_date_keeps_conflicts()
    for case in (
        test_chat_stream_error_event_redacts_internal_details,
        test_chat_stream_user_facing_error_keeps_curated_message,
        test_chat_stream_storage_error_maps_friendly_message,
        test_get_session_constructs_outside_lock,
        test_destructive_endpoints_503_while_pg_cooling_down,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_documents_api(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_search_api(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_document_write_api(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_group_document_api(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_api_maps_backend_errors(_mp)
    finally:
        _mp.undo()
    test_knowledge_metadata_limits()
    _mp_kb_del = pytest.MonkeyPatch()
    try:
        test_kb_delete_grouped_fragment_maps_400(_mp_kb_del)
    finally:
        _mp_kb_del.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_transfer_api(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_kb_import_error_mapping(_mp)
    finally:
        _mp.undo()
    _mp = pytest.MonkeyPatch()
    try:
        test_kb_group_error_mapping(_mp)
    finally:
        _mp.undo()
    test_status_endpoint()
    test_chat_returns_reply_and_usage()
    test_chat_creates_session_when_missing()
    test_chat_stream_events()
    test_stream_heartbeat()
    test_chat_failure_returns_500()
    test_chat_stream_failure_event()
    test_sessions_listing_and_delete()
    test_session_search()
    test_status_usage_totals()
    test_preference_rejects_non_finite()
    test_preferences_api()
    test_preference_storage_failure_maps_503(pytest.MonkeyPatch())
    test_status_trips()
    test_trips_query_filter_api()
    test_delete_trip_api()
    test_create_trip_api()
    test_update_trip_api()
    test_trip_overlap_conflicts_api()
    test_trip_conflicts_helper_edges()
    test_list_trips_api()
    test_delete_last_turn_api()
    test_next_trip_prefers_ongoing()
    test_status_trips_include_conflicts()
    test_session_archive_api()
    test_archived_sessions_search_and_pagination()
    _mp_storage = pytest.MonkeyPatch()
    try:
        test_session_metadata_storage_failure_maps_503(_mp_storage)
        test_trips_csv_storage_failure_maps_503(_mp_storage)
        test_delete_session_storage_failure_maps_503(_mp_storage)
    finally:
        _mp_storage.undo()
    test_sessions_pagination()
    _mp = pytest.MonkeyPatch()
    try:
        test_delete_trip_storage_failure_maps_503(_mp)
    finally:
        _mp.undo()
    test_trip_ics_uid_includes_content()
    test_ics_escape_handles_lone_cr()
    test_build_trips_ics_multiple_events()
    test_trips_calendar_api()
    _mp_ics = pytest.MonkeyPatch()
    try:
        test_trip_calendar_storage_failure_maps_503(_mp_ics)
    finally:
        _mp_ics.undo()
    test_trips_csv_export()
    test_trips_csv_export_query_filter()
    test_session_markdown_export()
    test_export_filename_is_ascii()
    test_session_lru_eviction()
    test_session_lru_closes_evicted_session()
    test_session_cache_is_thread_safe()
    test_streaming_releases_user_lock()
    test_delete_last_turn_clears_cached_short_term()
    test_session_cache_is_user_scoped()
    test_trip_whitespace_destination_rejected()
    test_ics_handles_overflow_end_date()
    test_request_id_header()
    test_metrics_api()
    test_prometheus_metrics_endpoint()
    _mp_metrics_gauges = pytest.MonkeyPatch()
    try:
        test_prometheus_storage_gauges_reflect_state(_mp_metrics_gauges)
    finally:
        _mp_metrics_gauges.undo()
    test_metrics_endpoint_not_in_snapshot_before_recording()
    test_security_headers()
    test_lock_table_is_bounded()
    test_lock_table_overflow_uses_shared_lock()
    test_lock_eviction_skips_waiting_lock()
    test_lock_for_overflow_user_keeps_shared_lock_while_held()
    test_session_turn_lease_releases_once()
    test_leased_streaming_response_releases_even_if_body_never_starts()
    test_session_lru_skips_in_use_sessions()
    test_chat_releases_session_in_use_mark()
    test_request_body_too_large()
    test_chunked_request_body_too_large()
    test_chunked_request_body_allowed_under_limit()
    test_gzip_compression_excludes_sse()
    test_docs_csp_allows_cdn()
    test_unhandled_error_returns_500_with_headers()
    test_chat_rate_limit()
    test_rate_limit_race_rejects_over_limit()
    test_rate_limit_headers()
    test_chat_rate_limit_ip_bucket()
    test_chat_rate_limit_rejected_request_does_not_consume_other_bucket()
    test_ip_rejection_does_not_consume_user_bucket()
    test_deleted_session_cannot_be_resurrected()
    test_repeated_delete_keeps_tombstone()
    test_delete_missing_session_allows_new_chat()
    _mp_ghost = pytest.MonkeyPatch()
    try:
        test_delete_missing_session_failure_does_not_leave_tombstone(_mp_ghost)
    finally:
        _mp_ghost.undo()
    test_tombstone_helpers_are_thread_safe()
    test_repeated_delete_repairs_shared_tombstone()
    test_session_metadata_requires_existing_session()
    test_session_rename()
    test_session_pin()
    test_export_markdown()
    test_trip_calendar_export()
    _mp_trip_update = pytest.MonkeyPatch()
    try:
        test_trip_update_validation_and_errors(_mp_trip_update)
    finally:
        _mp_trip_update.undo()
    test_next_trip_from_full_history()
    test_usage_by_day_local_date()
    test_invalid_user_id_rejected()
    test_export_attachment()
    test_export_all_sessions_api()
    test_export_all_sessions_storage_failure_maps_503(pytest.MonkeyPatch())
    _mp_open = pytest.MonkeyPatch()
    try:
        test_open_long_term_failure_maps_503(_mp_open)
    finally:
        _mp_open.undo()
    print("\n全部通过")
