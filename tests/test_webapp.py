"""
Web 界面 API 测试（无需 API Key、不访问网络）

通过注入假模型走通完整接口：页面、健康检查、状态、对话（含 SSE 流式进度）、
会话历史 / 删除、导出。真实大模型链路由其它测试覆盖。

运行：python tests/test_webapp.py
"""
import json
import os
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
    test_sessions_listing_and_delete()
    test_session_markdown_export()
    test_export_filename_is_ascii()
    test_session_lru_eviction()
    test_invalid_user_id_rejected()
    test_export_attachment()
    print("\n全部通过")
