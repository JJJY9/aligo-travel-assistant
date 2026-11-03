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
import uuid
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
    print("✓ 首页可访问（新版界面）")


def test_health_endpoint():
    data = _client().get("/api/health").json()
    assert data["status"] == "ok"
    assert data["version"]
    assert data["storage"]["short_term"] in {"memory", "redis"}
    assert data["storage"]["long_term"] in {"json", "postgres"}
    print("✓ /api/health 返回版本 / 模型 / 存储后端")


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
            events = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))

        types = [event["type"] for event in events]
        assert types[0] == "phase"
        assert "plan" in types
        assert types[-1] == "done"
        done = events[-1]
        assert done["session_id"] == "sess-stream"
        assert done["reply"]
        assert done["usage"]["calls"] >= 1
        print("✓ /api/chat/stream 推送进度事件并以 done 结束")
    finally:
        _cleanup(user_id)


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


def test_session_lru_eviction():
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 2
    for session_id in ("s-a", "s-b", "s-c"):
        state.get_session("web_test_lru", session_id)
    assert list(state.sessions.keys()) == ["s-b", "s-c"]
    state.close_all()
    print("✓ 会话缓存按 LRU 淘汰并释放")


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

        assistant_hits = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "已记录"}
        ).json()["sessions"]
        assert len(assistant_hits) == 2, "助手消息按渲染后的文本参与搜索"

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

        invalid = client.put(
            "/api/preferences/bad type", json={"user_id": user_id, "value": "x"}
        )
        assert invalid.status_code == 422
        too_long = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "x" * 201},
        )
        assert too_long.status_code == 422
        print("✓ 偏好新增 / 覆盖 / 删除 / 参数校验")
    finally:
        _cleanup(user_id)


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


def test_request_id_header():
    client = _client()
    response = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers.get("x-request-id") == "trace-abc-123"

    generated = client.get("/api/health").headers.get("x-request-id", "")
    assert re.fullmatch(r"[0-9a-f]{12}", generated), generated
    print("✓ X-Request-ID 透传 / 自动生成")


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

    html = client.get("/").text
    assert "/static/theme.js" in html
    assert "<script>" not in html, "不应再使用内联脚本（CSP 禁止）"
    print("✓ 安全响应头（CSP / nosniff / frame / referrer）")


def test_export_markdown():
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_md")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_preference("hotel_brands", ["汉庭"])
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

        response = client.get("/api/export", params={"user_id": user_id, "format": "md"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers.get("content-disposition", "")
        text = response.text
        assert "# Aligo 用户数据导出" in text
        assert "hotel_brands：汉庭" in text
        assert "北京 → 上海" in text
        assert "## 最近会话" in text and "你好" in text

        invalid = client.get("/api/export", params={"user_id": user_id, "format": "pdf"})
        assert invalid.status_code == 422
        print("✓ 用户数据导出 Markdown（JSON 仍为默认）")
    finally:
        _cleanup(user_id)


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

        missing = client.get("/api/trips/trip_999/calendar", params={"user_id": user_id})
        assert missing.status_code == 404

        no_date = client.get("/api/trips/trip_2/calendar", params={"user_id": user_id})
        assert no_date.status_code == 400
        print("✓ 行程导出 iCalendar（含日期校验）")
    finally:
        _cleanup(user_id)


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


if __name__ == "__main__":
    test_index_page()
    test_health_endpoint()
    test_status_endpoint()
    test_chat_returns_reply_and_usage()
    test_chat_creates_session_when_missing()
    test_chat_stream_events()
    test_stream_heartbeat()
    test_sessions_listing_and_delete()
    test_session_search()
    test_status_usage_totals()
    test_preferences_api()
    test_status_trips()
    test_session_markdown_export()
    test_export_filename_is_ascii()
    test_session_lru_eviction()
    test_request_id_header()
    test_security_headers()
    test_chat_rate_limit()
    test_session_rename()
    test_session_pin()
    test_export_markdown()
    test_trip_calendar_export()
    test_invalid_user_id_rejected()
    test_export_attachment()
    print("\n全部通过")
