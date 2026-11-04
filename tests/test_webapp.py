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
    assert "处理失败" in detail and "fail-test" in detail
    assert response.headers["x-request-id"] == "fail-test"
    print("✓ 对话失败返回 500 与请求 ID")


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
        server_module, "_open_long_term", lambda user_id: _BrokenStore()
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
        assert (
            client.get(
                "/api/trips", params={"user_id": user_id, "limit": 0}
            ).status_code
            == 422
        )
        print("✓ /api/trips 列表（分页 / 重叠信息 / 参数校验）")
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

    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id: _BrokenStore())
    response = _client().delete(
        "/api/trips/trip_1", params={"user_id": "web_test_trip_broken"}
    )
    assert response.status_code == 503
    print("✓ 行程删除写入失败返回 503（不伪装成成功）")


def test_trip_ics_uid_includes_content():
    from webapp.server import _build_trip_ics

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
            line for line in _build_trip_ics(trip, "u1").splitlines() if line.startswith("UID:")
        )

    assert uid(base) != uid(other), "复用 trip_id 的新行程不应覆盖旧日历事件"
    assert uid(base) == uid(base), "同一行程的 UID 应保持稳定"
    print("✓ ICS UID 包含行程内容（复用 id 不覆盖旧事件）")


def test_ics_escape_handles_lone_cr():
    from webapp.server import _build_trip_ics, _ics_escape

    assert _ics_escape("a\rb") == "a\\nb"
    trip = {
        "trip_id": "trip_1",
        "origin": "北京",
        "destination": "上海\r恶意行",
        "start_date": "2026-11-09",
        "end_date": "2026-11-11",
        "purpose": "出差",
    }
    content = _build_trip_ics(trip, "u1")
    assert "上海\\n恶意行" in content
    assert "\r恶意行" not in content
    print("✓ ICS 转义孤立 CR（不破坏行结构）")


def test_build_trips_ics_multiple_events():
    from webapp.server import _build_trips_ics

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
    content = _build_trips_ics(trips, "u1")
    assert content.count("BEGIN:VCALENDAR") == 1
    assert content.count("BEGIN:VEVENT") == 2
    assert content.count("END:VEVENT") == 2
    assert content.endswith("END:VCALENDAR\r\n")

    # 缺少出发日期的行程被跳过
    content = _build_trips_ics(trips + [{"trip_id": "trip_3", "destination": "杭州"}], "u1")
    assert content.count("BEGIN:VEVENT") == 2

    empty = _build_trips_ics([], "u1")
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
        server_module, "_open_long_term", lambda user_id: _BrokenStore()
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


def test_trips_csv_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id: _BrokenStore()
    )
    response = _client().get("/api/trips/export", params={"user_id": "u1"})
    assert response.status_code == 503
    listed = _client().get("/api/trips", params={"user_id": "u1"})
    assert listed.status_code == 503
    print("✓ 行程 CSV / 列表读取失败 → 503")


def test_delete_session_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def delete_chat_session(self, session_id):
            raise RuntimeError("disk full")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id: _BrokenStore()
    )
    response = _client().delete("/api/sessions/s1", params={"user_id": "u1"})
    assert response.status_code == 503
    print("✓ 删除会话存储失败 → 503")


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

    html = client.get("/").text
    assert "/static/theme.js" in html
    assert "<script>" not in html, "不应再使用内联脚本（CSP 禁止）"
    print("✓ 安全响应头（CSP / nosniff / frame / referrer）")


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

    def fake_delete(doc_id):
        deleted["id"] = doc_id
        return doc_id == 7

    monkeypatch.setattr(server_module, "delete_document", fake_delete)
    ok = client.delete("/api/knowledge/documents/7")
    assert ok.status_code == 200 and ok.json()["deleted"] is True
    assert deleted["id"] == 7
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
    assert (
        client.put("/api/knowledge/documents/7", json={"content": ""}).status_code
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
    print("✓ /api/knowledge/search 语义检索（参数 / 结果 / 503）")


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
    assert empty_item.json() == {"imported": 0, "skipped": 1}

    calls = {}

    def fake_import(documents):
        calls["documents"] = documents
        return {"imported": len(documents), "skipped": 0}

    monkeypatch.setattr(server_module, "import_documents", fake_import)
    imported = client.post(
        "/api/knowledge/import",
        json={"documents": [{"content": "x", "metadata": {"category": "c"}}]},
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    assert calls["documents"] == [{"content": "x", "metadata": {"category": "c"}}]

    assert (
        client.post("/api/knowledge/import", json={"documents": []}).status_code == 422
    )
    too_many = [{"content": "x"} for _ in range(201)]
    assert (
        client.post("/api/knowledge/import", json={"documents": too_many}).status_code
        == 422
    )
    print("✓ /api/knowledge/export 与 /api/knowledge/import")


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
    from datetime import datetime, timezone

    from webapp.server import _usage_summary

    aware = datetime.now(timezone.utc).replace(microsecond=0)
    meta = {
        "usage": {"calls": 1, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "elapsed": 1.0,
        "agents": [],
    }
    history = [
        {
            "role": "assistant",
            "content": json.dumps({"results": [], "_meta": meta}, ensure_ascii=False),
            "timestamp": aware.isoformat(),
            "session_id": "s",
        }
    ]
    usage = _usage_summary(history)
    expected = aware.astimezone().date().isoformat()
    bucket = next(day for day in usage["by_day"] if day["date"] == expected)
    assert bucket["tokens"] == 5
    print("✓ by_day 使用本地日期")


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
    test_knowledge_endpoint()
    test_doctor_endpoint()
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
        test_knowledge_api_maps_backend_errors(_mp)
    finally:
        _mp.undo()
    test_knowledge_metadata_limits()
    _mp = pytest.MonkeyPatch()
    try:
        test_knowledge_transfer_api(_mp)
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
    test_trips_csv_export()
    test_session_markdown_export()
    test_export_filename_is_ascii()
    test_session_lru_eviction()
    test_request_id_header()
    test_security_headers()
    test_lock_table_is_bounded()
    test_lock_table_overflow_uses_shared_lock()
    test_request_body_too_large()
    test_chunked_request_body_too_large()
    test_chunked_request_body_allowed_under_limit()
    test_gzip_compression_excludes_sse()
    test_docs_csp_allows_cdn()
    test_unhandled_error_returns_500_with_headers()
    test_chat_rate_limit()
    test_rate_limit_headers()
    test_chat_rate_limit_ip_bucket()
    test_session_rename()
    test_session_pin()
    test_export_markdown()
    test_trip_calendar_export()
    test_next_trip_from_full_history()
    test_usage_by_day_local_date()
    test_invalid_user_id_rejected()
    test_export_attachment()
    print("\n全部通过")
