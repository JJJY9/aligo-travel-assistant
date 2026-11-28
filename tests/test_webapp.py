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
# 生产默认不信任测试客户端的 Host: testserver（见 _host_allowed）；
# 测试显式启用测试缝
os.environ.setdefault("ALIGO_TEST_CLIENT", "1")

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
    # filelock 每次写入都会创建 <user>.json.lock：不清理的话几百个用例
    # 会把 data/memory 堆成数万个零字节文件（长期运行后拖慢目录操作）
    lock_path = Path(MEMORY_DIR) / f"{user_id}.json.lock"
    if lock_path.exists():
        lock_path.unlink()
    # 导入互斥锁（<user>.json.import.lock）同样是零字节残留
    import_lock = Path(MEMORY_DIR) / f"{user_id}.json.import.lock"
    if import_lock.exists():
        import_lock.unlink()
    # 旧格式数据加载会触发破坏性迁移并留下原始副本：同样不能堆在
    # data/memory（迁移副本含完整用户数据）
    migrate_path = Path(MEMORY_DIR) / f"{user_id}.json.migrate-backup"
    if migrate_path.exists():
        migrate_path.unlink()


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


def test_knowledge_busy_maps_to_503(monkeypatch):
    """知识库写锁超时（导入进行中）：读接口快速 503，不再挂线程等锁。"""
    from utils import knowledge_admin
    from webapp import server as server_module

    def busy(*args, **kwargs):
        raise knowledge_admin.KnowledgeBusyError(
            "知识库正在写入（导入进行中），请稍后重试"
        )

    monkeypatch.setattr(server_module, "list_documents", busy)
    client = _client()
    response = client.get("/api/knowledge/documents")
    assert response.status_code == 503, response.text
    # 可操作的忙碌状态直接透出原因并带 Retry-After，不再吞成通用
    # 「知识库暂不可用，请稍后再试」
    assert "导入进行中" in response.json()["detail"], response.text
    assert response.headers.get("Retry-After") == "5", response.headers
    print("✓ 知识库写锁超时快速 503（可操作提示 + Retry-After）")


def test_knowledge_dimension_mismatch_maps_to_503():
    """维度不一致给出「重建知识库」的可操作提示，而不是通用「稍后再试」。"""
    from webapp import server as server_module

    exc = RuntimeError(
        "知识库向量维度 512 与嵌入模型 768 不一致，请重新运行 python init_knowledge_base.py 重建知识库"
    )
    mapped = server_module._knowledge_unavailable("检索", exc)
    assert mapped.status_code == 503, mapped
    assert "维度" in mapped.detail and "重新运行" in mapped.detail, mapped.detail
    print("✓ 知识库维度不一致给出可操作提示")
def test_write_rate_limit_headers_and_rejection(monkeypatch):
    """用户数据写接口有独立的按 IP 额度：超限 429，读接口不受影响。"""
    from webapp import server as server_module

    monkeypatch.setitem(server_module.RATE_LIMIT_CONFIG, "write_requests", 2)
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    user_id = _unique_user("web_write_limit")
    try:
        for index in (1, 2):
            response = client.put(
                f"/api/preferences/p{index}",
                json={"user_id": user_id, "value": "x"},
            )
            assert response.status_code == 200, response.text
        third = client.put(
            "/api/preferences/p3", json={"user_id": user_id, "value": "x"}
        )
        assert third.status_code == 429, third.text
        assert third.headers.get("retry-after"), third.headers
        assert "写入过于频繁" in third.json()["detail"]
        # 写桶与读桶独立：读接口不因写超限被拒
        assert client.get("/api/health").status_code == 200
    finally:
        _cleanup(user_id)
    print("✓ 写接口独立限流（超限 429，读接口不受影响）")


def test_preference_and_trip_write_caps(monkeypatch):
    """偏好 / 行程实时写入有条数上限（与导入上限同口径）。"""
    from webapp import server as server_module

    monkeypatch.setattr(server_module, "MAX_PREFERENCE_ENTRIES", 2)
    monkeypatch.setattr(server_module, "MAX_TRIP_ENTRIES", 1)
    client = _client()
    user_id = _unique_user("web_write_caps")
    try:
        for name in ("p1", "p2"):
            response = client.put(
                f"/api/preferences/{name}",
                json={"user_id": user_id, "value": "x"},
            )
            assert response.status_code == 200, response.text
        third = client.put(
            "/api/preferences/p3", json={"user_id": user_id, "value": "x"}
        )
        assert third.status_code == 409, third.text
        assert "上限" in third.json()["detail"]
        # 覆盖已有偏好不受条数上限影响
        update = client.put(
            "/api/preferences/p1", json={"user_id": user_id, "value": "y"}
        )
        assert update.status_code == 200, update.text

        trips = [
            {"origin": "北京", "destination": "杭州", "start_date": "2026-12-01"},
            {"origin": "上海", "destination": "深圳", "start_date": "2026-12-02"},
        ]
        first = client.post("/api/trips", json={"user_id": user_id, **trips[0]})
        assert first.status_code == 201, first.text
        second = client.post("/api/trips", json={"user_id": user_id, **trips[1]})
        assert second.status_code == 409, second.text
        assert "上限" in second.json()["detail"]
        # 恰好在上限时同身份重复提交按更新放行（不再被锁外预检 409）；
        # 上限收紧后已越限的账号继续可用（与存储写层同口径）
        duplicate = client.post(
            "/api/trips", json={"user_id": user_id, **trips[0]}
        )
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["created"] is False
        monkeypatch.setattr(server_module, "MAX_PREFERENCE_ENTRIES", 1)
        over = client.put(
            "/api/preferences/p3", json={"user_id": user_id, "value": "x"}
        )
        assert over.status_code == 200, over.text
    finally:
        _cleanup(user_id)
    print("✓ 偏好 / 行程写接口条数上限")
def test_host_header_allowlist_blocks_rebinding():
    """未知 Host 一律 400：DNS rebinding 不能以同源身份读写本机数据。"""
    client = _client()
    assert client.get("/api/health").status_code == 200

    evil = client.get("/api/health", headers={"Host": "evil.example:8000"})
    assert evil.status_code == 400, evil.text
    assert "Host 不在允许列表" in evil.text

    # 尾斜杠的 307 重定向之前就拒绝，不能回显攻击者控制的 Host
    redirect = client.get(
        "/api/health/",
        headers={"Host": "evil.example:8000"},
        follow_redirects=False,
    )
    assert redirect.status_code == 400, redirect.text

    # LAN 演示用 IP 字面量 / localhost 子域直接放行
    assert (
        client.get(
            "/api/health", headers={"Host": "192.168.1.5:8000"}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/health", headers={"Host": "app.localhost"}).status_code
        == 200
    )
    print("✓ Host 白名单拒绝未知域名（防 DNS rebinding）")


def test_host_authority_malformed_and_duplicate_rejected(monkeypatch):
    """畸形 / 重复 Host 一律 400（白名单解析与 URL 解析同口径）。"""
    from webapp import server as server_module

    client = _client()
    for host in (
        "127.0.0.1:8000@evil.com",
        "[::1]evil.com",
        "[::1]evil.com:8000",
        "127.0.0.1:8000,evil.com",
        "127.0.0.1:8000, evil.com",
        "evil@x.localhost",
        "x.localhost:8000@evil.com",
    ):
        response = client.get("/api/health", headers={"Host": host})
        assert response.status_code == 400, (host, response.status_code)
        assert server_module._host_allowed(host) is False, host
    # 重复 Host（first/last-wins 分歧）：一律 400
    duplicated = client.get(
        "/api/health",
        headers=[("host", "127.0.0.1"), ("host", "evil.com")],
    )
    assert duplicated.status_code == 400, duplicated.text
    # Host 拒绝响应在所有路径都带 no-store（非 /api 路径同样不可缓存）
    rejected = client.get("/", headers={"Host": "evil.example"})
    assert rejected.status_code == 400
    assert rejected.headers.get("cache-control") == "no-store"
    # 合法形式保持放行：配置域名可带端口 / 尾点匹配
    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", "example.com")
    assert (
        client.get(
            "/api/health", headers={"Host": "example.com.:8000"}
        ).status_code
        == 200
    )
    print("✓ 畸形 / 重复 Host 拒绝（尾点 + 端口归一仍放行）")


def test_host_allowlist_idna(monkeypatch):
    """ALIGO_ALLOWED_HOSTS 的 Unicode 域名按浏览器 A-label 匹配。"""
    client = _client()
    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", "例子.中")
    punycode = "例子.中".encode("idna").decode("ascii")
    assert (
        client.get("/api/health", headers={"Host": punycode}).status_code == 200
    )
    other = "别的.中".encode("idna").decode("ascii")
    assert (
        client.get("/api/health", headers={"Host": other}).status_code == 400
    ), "未配置的 IDN 域名必须继续拒绝"
    print("✓ ALIGO_ALLOWED_HOSTS 支持 IDN 域名（A-label 匹配）")


def test_trailing_slash_redirect_uses_relative_location(monkeypatch):
    """同源 307 的 Location 为相对路径：TLS 反代下不把 https 降级为 http。"""
    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", "aligo.example.com")
    client = _client()
    response = client.get(
        "/api/health/",
        headers={
            "Host": "aligo.example.com",
            "X-Forwarded-Proto": "https",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307, response.text
    assert response.headers["location"] == "/api/health"
    assert "no-store" in response.headers.get("cache-control", "")
    assert response.headers.get("x-request-id")
    query = client.get(
        "/api/sessions/export/?user_id=alice",
        headers={"Host": "aligo.example.com"},
        follow_redirects=False,
    )
    assert query.headers["location"] == "/api/sessions/export?user_id=alice"
    print("✓ 尾斜杠 307 使用相对 Location（不解引用后端 http scheme）")


def test_sse_and_exports_are_no_store():
    """SSE 对话流与导出响应都不可缓存（/api 不变式无例外）。"""
    client = _client()
    user = _unique_user("web_test_cache_headers")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": user,
                "session_id": "cache-1",
            },
        ) as stream:
            assert stream.status_code == 200
            assert "no-store" in stream.headers.get("cache-control", "")
        export = client.get("/api/export", params={"user_id": user})
        assert export.status_code == 200
        assert "no-store" in export.headers.get("cache-control", "")
        # 无历史的会话导出是 404：错误响应同样不可缓存
        empty = client.get("/api/sessions/export", params={"user_id": user})
        assert "no-store" in empty.headers.get("cache-control", "")
    finally:
        _cleanup(user)
    print("✓ SSE / 导出响应 no-store 覆盖（含错误响应）")


def test_index_conditional_get_returns_304():
    """入口页带 ETag 的条件请求回 304 空体，仍带 no-cache 与安全头。"""
    client = _client()
    first = client.get("/")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "入口页响应必须带 ETag"
    second = client.get("/", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers.get("cache-control") == "no-cache"
    assert second.headers.get("x-frame-options") == "DENY"
    print("✓ 入口页条件请求返回 304")


def test_docs_csp_frame_ancestors_and_disabled_strict(monkeypatch):
    """文档页 CSP 的 frame-ancestors 恒为 none；docs 关闭时 404 用严格策略。"""
    client = _client()
    disabled = client.get("/docs")
    assert disabled.status_code == 404
    strict_csp = disabled.headers.get("content-security-policy", "")
    assert "cdn.jsdelivr.net" not in strict_csp
    assert "script-src 'self'" in strict_csp
    assert "frame-ancestors 'none'" in strict_csp

    monkeypatch.setenv("ALIGO_ENABLE_DOCS", "1")
    docs_client = TestClient(create_app(model=UsageTrackingModel(_FakeModel())))
    docs = docs_client.get("/docs")
    assert docs.status_code == 200
    docs_csp = docs.headers.get("content-security-policy", "")
    assert "cdn.jsdelivr.net" in docs_csp, "文档页仍需放行 CDN"
    assert "frame-ancestors 'none'" in docs_csp, (
        "文档页 frame-ancestors 必须与 X-Frame-Options: DENY 一致"
    )
    assert docs.headers.get("x-frame-options") == "DENY"
    assert docs.headers.get("cache-control") == "no-store"
    print("✓ 文档页 CSP frame-ancestors=none；关闭时 404 用严格策略")


def test_host_allowlist_env(monkeypatch):
    """ALIGO_ALLOWED_HOSTS 支持精确域名与 *.example.com 通配。"""
    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", "*.example.com, aligo.lan")
    client = _client()
    assert (
        client.get("/api/health", headers={"Host": "box.example.com"}).status_code
        == 200
    )
    assert (
        client.get(
            "/api/health", headers={"Host": "aligo.lan:8000"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/health", headers={"Host": "other.example.net"}
        ).status_code
        == 400
    )
    # 配置项与请求 Host 同一归一化：按地址栏写成「域名:端口」也能匹配，
    # 尾点 + 端口的 Host 先剥端口再去尾点
    monkeypatch.setenv(
        "ALIGO_ALLOWED_HOSTS",
        "aligo.example.com:8000, *.corp.example.com:8443",
    )
    client = _client()
    assert (
        client.get(
            "/api/health", headers={"Host": "aligo.example.com:8000"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/health", headers={"Host": "aligo.example.com"}
        ).status_code
        == 200
    ), "配置带端口时，不带端口的请求 Host 同样应匹配"
    assert (
        client.get(
            "/api/health", headers={"Host": "box.corp.example.com:8443"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/health", headers={"Host": "aligo.example.com.:8000"}
        ).status_code
        == 200
    ), "尾点必须在剥端口之后再去除"
    print("✓ ALIGO_ALLOWED_HOSTS 域名白名单")


def test_testserver_host_rejected_without_test_env(monkeypatch):
    """生产默认不信任 Host: testserver（可被解析到本机 / DNS rebinding）。"""
    client = _client()
    monkeypatch.delenv("ALIGO_TEST_CLIENT", raising=False)
    response = client.get("/api/health", headers={"Host": "testserver"})
    assert response.status_code == 400, response.text
    # 测试环境下仍放行（TestClient 默认 Host）
    monkeypatch.setenv("ALIGO_TEST_CLIENT", "1")
    assert (
        client.get("/api/health", headers={"Host": "testserver"}).status_code
        == 200
    )
    print("✓ Host: testserver 仅在测试环境放行")


def test_side_effect_guard_requires_same_origin_marker():
    """缺失 Sec-Fetch-Site 不再视为可信：需 X-Aligo-Client 或 same-origin。

    Referer 不作为同源证据（对所有响应下发 Referrer-Policy: no-referrer，
    且反代部署下 request.url.scheme 恒为 http，scheme 比较不可信）。
    """
    client = _client()
    user_id = _unique_user("web_side_effect_guard")
    host = {"Host": "127.0.0.1:8000"}
    try:
        blocked = client.get("/api/status", params={"user_id": user_id}, headers=host)
        assert blocked.status_code == 403, blocked.text
        assert "X-Aligo-Client" in blocked.json()["detail"]

        marked = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "X-Aligo-Client": "web"},
        )
        assert marked.status_code == 200, marked.text

        # Referer 不再作为同源证据：即使 netloc 与 Host 一致也拒绝
        same_ref = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Referer": "http://127.0.0.1:8000/index.html"},
        )
        assert same_ref.status_code == 403, same_ref.text

        bad_ref = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Referer": "http://evil.example/page"},
        )
        assert bad_ref.status_code == 403, bad_ref.text

        # 同主机不同 scheme / 协议相对串都是跨源：只比 netloc 会误放行
        other_scheme = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Referer": "https://127.0.0.1:8000/index.html"},
        )
        assert other_scheme.status_code == 403, other_scheme.text
        relative = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Referer": "//127.0.0.1:8000/index.html"},
        )
        assert relative.status_code == 403, relative.text

        # 显式跨站头即使带了客户端标记也拒绝
        cross = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Sec-Fetch-Site": "cross-site", "X-Aligo-Client": "web"},
        )
        assert cross.status_code == 403, cross.text

        # Sec-Fetch-Site: none（地址栏 / 外部链接 / 302 掩码）不证明同源：
        # 外部链接一次点击不能触发全量导出 / 真实 LLM 调用
        none_site = client.get(
            "/api/export",
            params={"user_id": user_id},
            headers={**host, "Sec-Fetch-Site": "none"},
        )
        assert none_site.status_code == 403, none_site.text

        same_site = client.get(
            "/api/status",
            params={"user_id": user_id},
            headers={**host, "Sec-Fetch-Site": "same-origin"},
        )
        assert same_site.status_code == 200, same_site.text

        export_blocked = client.get(
            "/api/export", params={"user_id": user_id}, headers=host
        )
        assert export_blocked.status_code == 403, export_blocked.text
        # 非高开销接口不受影响
        assert client.get("/api/health", headers=host).status_code == 200
    finally:
        _cleanup(user_id)
    print("✓ 高开销 GET 需同源标记（缺失 Sec-Fetch-Site 不再放行）")

def test_session_detail_fails_closed_when_store_unavailable(monkeypatch):
    """配置了 PG 但不可用时会话详情返回 503，不能以空消息冒充「已删除」。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    calls = {}

    def fake_open(user_id, *args, **kwargs):
        calls["strict"] = kwargs.get("strict")
        raise MemoryPersistenceError("PostgreSQL 处于故障冷却期")

    monkeypatch.setattr(server_module, "_open_long_term", fake_open)
    client = _client()
    response = client.get("/api/sessions/s1", params={"user_id": "u1"})
    assert response.status_code == 503, response.text
    assert calls.get("strict") is True, (
        "会话详情必须以 strict 打开存储，不能静默回退 JSON 库返回空消息"
    )
    print("✓ 会话详情严格打开存储（不可用时 503）")


def test_status_requires_user_id():
    """数据接口必填口径：/api/status 缺省 user_id 返回 422，不回退 web_user。"""
    client = _client()
    response = client.get("/api/status")
    assert response.status_code == 422, response.text
    response = client.get("/api/status", params={"user_id": "web_user"})
    assert response.status_code == 200, response.text
    print("✓ /api/status 缺省 user_id 返回 422")


def test_sessions_and_status_fail_closed_when_store_unavailable(monkeypatch):
    """PG 故障冷却期：会话列表与状态同样 503（与详情 / 写接口同口径）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    def _fail(user_id, *args, **kwargs):
        raise MemoryPersistenceError("PostgreSQL 处于故障冷却期")

    monkeypatch.setattr(server_module, "_open_long_term", _fail)
    client = _client()
    for path in ("/api/sessions", "/api/status"):
        response = client.get(path, params={"user_id": "u1"})
        assert response.status_code == 503, (path, response.text)
    print("✓ 列表 / 状态严格打开存储（PG 故障期 503）")


def test_session_metadata_string_identity():
    """整数历史元数据：置顶响应 / 列表 / 归档计数按字符串归一口径。"""
    user = _unique_user("web_test_int_meta")
    path = Path(MEMORY_DIR) / f"{user}.json"
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "Q",
                        "session_id": 42,
                        "timestamp": "2000-01-01T00:00:00",
                    },
                    {
                        "role": "user",
                        "content": "Z",
                        "session_id": 0,
                        "timestamp": "2000-01-02T00:00:00",
                    },
                ],
                "pinned_sessions": [42],
                "archived_sessions": [42],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        client = _client()
        pinned = client.put(
            "/api/sessions/42/pin", json={"user_id": user, "pinned": True}
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["pinned"] is True, (
            "整数置顶条目下重复置顶应报 pinned=true"
        )
        listing = client.get("/api/sessions", params={"user_id": user}).json()
        ids = {item["session_id"] for item in listing["sessions"]}
        assert "0" in ids, "整数 0 会话应出现在列表（与 Web / PG 同口径）"
        assert "42" not in ids, "归档会话不出现在默认列表"
        assert listing["archived_count"] == 1
        archived = client.get(
            "/api/sessions", params={"user_id": user, "archived": True}
        ).json()
        assert [item["session_id"] for item in archived["sessions"]] == ["42"]
        assert archived["archived_count"] == 1
    finally:
        _cleanup(user)
    print("✓ 整数元数据按字符串归一口径（置顶响应 / 列表 / 归档计数）")


def test_export_concurrency_guard():
    """全量导出并发受信号量限制：槽位占满时快速 503，不排队申请内存。"""
    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_export_slots")
    acquired = [state.export_slots.acquire(blocking=False) for _ in range(2)]
    try:
        assert all(acquired), "测试应能占满导出槽位"
        for path in (
            "/api/export",
            "/api/sessions/export",
            "/api/sessions/s1/export",
            "/api/knowledge/export",
        ):
            response = client.get(path, params={"user_id": user_id})
            assert response.status_code == 503, (path, response.text)
            assert "导出任务过多" in response.json()["detail"]
    finally:
        for _ in acquired:
            state.export_slots.release()
    # 槽位释放后不再被并发保护拒绝（无数据可能是 404 / 200）
    response = client.get("/api/export", params={"user_id": user_id})
    assert response.status_code != 503, response.text
    print("✓ 全量导出并发上限（槽位占满快速 503）")


def test_knowledge_update_conflict_maps_409(monkeypatch):
    """并发编辑冲突映射 409（可刷新重试），不混入 400 的内容不合法口径。"""
    from utils.knowledge_admin import DocumentConflictError
    from webapp import server as server_module

    captured: dict = {}

    def fake_update(doc_id, content, metadata=None, **kwargs):
        captured.update(kwargs)
        raise DocumentConflictError("知识片段已在其他会话被修改，请刷新后重新编辑")

    monkeypatch.setattr(server_module, "update_document", fake_update)
    client = _client()
    response = client.put(
        "/api/knowledge/documents/7",
        json={
            "content": "改动",
            "metadata": {},
            "expect": "旧内容",
            "expect_parts": 1,
            "expect_group": "",
        },
    )
    assert response.status_code == 409, response.text[:300]
    assert response.headers.get("retry-after") == "1"
    assert captured == {
        "expect_content": "旧内容",
        "expect_parts": 1,
        "expect_group": "",
        "expect_metadata": None,
    }, captured
    # 元数据轴的快照同样透传给存储层（另一会话只改分类时 409）
    captured.clear()
    response = client.put(
        "/api/knowledge/documents/7",
        json={
            "content": "改动",
            "metadata": {"category": "旧分类"},
            "expect": "旧内容",
            "expect_parts": 1,
            "expect_group": "",
            "expect_metadata": {"category": "旧分类"},
        },
    )
    assert response.status_code == 409, response.text[:300]
    assert captured["expect_metadata"] == {"category": "旧分类"}, captured
    print("✓ 知识编辑冲突返回 409 并透传快照")


def test_kb_reads_use_separate_slots_from_exports(monkeypatch):
    """知识库面板只读接口与导出分槽：导出占满时面板刷新不再被 503。"""
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "list_documents",
        lambda **kwargs: {"items": [], "total": 0},
    )
    monkeypatch.setattr(server_module, "list_categories", lambda: [])
    monkeypatch.setattr(
        server_module, "find_duplicate_contents", lambda: []
    )
    client = _client()
    state = client.app.state.aligo
    held = [state.export_slots.acquire(blocking=False) for _ in range(2)]
    try:
        assert all(held), "测试应能占满导出槽位"
        for path in (
            "/api/knowledge/documents",
            "/api/knowledge/categories",
            "/api/knowledge/duplicates",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:200])
    finally:
        for _ in held:
            state.export_slots.release()
    # 面板读接口自己的并发上限照样生效（不会变成无界全库扫描）
    kb_held = [state.kb_read_slots.acquire(blocking=False) for _ in range(4)]
    try:
        assert all(kb_held)
        response = client.get("/api/knowledge/documents")
        assert response.status_code == 503
        assert "知识库读取任务过多" in response.json()["detail"]
    finally:
        for _ in kb_held:
            state.kb_read_slots.release()
    print("✓ 知识库面板读接口独立并发槽（不再与导出互踩）")


def test_knowledge_unavailable_recognizes_milvus3_missing_collection():
    """Milvus Lite 3.x 的 does not exist / released 文案给出可操作提示。"""
    from webapp import server as server_module

    for message in (
        "collection 'business_travel_knowledge' does not exist",
        "Collection business_travel_knowledge is in state 'released'; call load() before search/get/query",
    ):
        response = server_module._knowledge_unavailable(
            "读取知识库失败", RuntimeError(message)
        )
        assert response.status_code == 503
        assert "重新运行初始化脚本" in response.detail, response.detail
    # 其它存储错误仍是通用文案（不回显内部细节）
    generic = server_module._knowledge_unavailable(
        "读取知识库失败", RuntimeError("db down at /home/alice/secret")
    )
    assert "请稍后再试" in generic.detail
    assert "alice" not in generic.detail


def test_import_slow_body_times_out_and_frees_slot(monkeypatch):
    """只发请求头不发 body：60 秒内超时 408，导入名额必须释放。"""
    import asyncio

    from webapp import server as server_module

    monkeypatch.setattr(server_module, "IMPORT_BODY_READ_TIMEOUT_SEC", 0.05)
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    events: list = []

    async def receive():
        if not events:
            events.append("first-request-chunk")
            return {"type": "http.request", "body": b"", "more_body": True}
        await asyncio.sleep(10)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            events.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/import",
        "raw_path": b"/api/import",
        "query_string": b"user_id=slowbody-user",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            # 声明 50 MB（合法上限内）但迟迟不发 body：走读取超时路径；
            # 超过 上限+余量 的声明会在读取前直接 413（见超限用例）
            (b"content-length", b"52428800"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert 408 in events, events
    assert state.import_queue_slots.acquire(blocking=False), (
        "读取 body 超时后必须释放导入名额"
    )
    state.import_queue_slots.release()
    print("✓ 慢速请求体超时 408 并释放导入名额")


def test_health_endpoint():
    data = _client().get("/api/health").json()
    assert data["status"] == "ok"
    assert data["version"]
    assert data["storage"]["short_term"] in {"memory", "redis"}
    assert data["storage"]["long_term"] in {"json", "postgres"}
    assert data["rate_limit"] in {"memory", "redis"}
    assert data["rate_limit_configured"] is False
    assert data["rate_limit_degraded"] is False
    assert data["sessions"]["cached"] >= 0
    assert data["sessions"]["max"] == 50, "健康信息要能看到并发会话上限"
    assert data["skills"]["count"] >= 6
    assert data["skills"]["dir_found"] is True
    assert data["skills"]["ok"] is True
    print("✓ /api/health 返回版本 / 模型 / 存储后端")


def test_health_degraded_when_skills_missing(monkeypatch):
    """/api/health 与 doctor 同口径：技能目录不可用时 status 必须 degraded。"""
    from pathlib import Path

    from webapp import server as server_module

    client = _client()
    monkeypatch.setattr(
        server_module,
        "resolve_skills_root",
        lambda: Path("definitely-missing-skills-dir"),
    )
    data = client.get("/api/health").json()
    assert data["status"] == "degraded"
    assert data["skills"]["ok"] is False
    assert data["skills"]["count"] == 0
    assert data["skills"]["dir_found"] is False
    print("✓ 技能缺失时 health 标记 degraded")


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


def test_health_reports_degraded_write_limiter():
    """写桶单独降级（只被写接口触发）也必须上报 degraded 与指标。"""

    class _DegradedLimiter:
        backend = "redis"
        limit = 240
        window_sec = 60
        degraded = True

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.write_limiter = _DegradedLimiter()
    client = TestClient(app)
    data = client.get("/api/health").json()
    assert data["rate_limit_degraded"] is True
    info = client.get("/api/metrics").json()
    assert info["rate_limit"]["write_limit"] == 240
    metrics = client.get("/metrics").text
    assert "aligo_rate_limit_degraded 1" in metrics
    print("✓ 写限流桶降级会上报健康与指标")


def test_weather_api(monkeypatch, caplog):
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

    def leaking(city):
        # requests 的连接异常原文带完整上游 URL 与查询参数：
        # 503 详情与 WARNING 日志都不能原样回显
        raise WeatherError(
            "天气接口暂时不可用：HTTPConnectionPool(host='geocoding-api.open-meteo.com', "
            "port=443): Max retries exceeded with url: /v1/search?name=北京"
        )

    monkeypatch.setattr(server_module, "fetch_weather", leaking)
    leaked = client.get("/api/weather", params={"city": "北京"})
    assert leaked.status_code == 503
    detail = leaked.json()["detail"]
    assert "open-meteo" not in detail and "name=北京" not in detail, detail
    assert "暂时不可用" in detail

    def missing_again(city):
        raise WeatherError(f"未找到城市「{city}」")

    monkeypatch.setattr(server_module, "fetch_weather", missing_again)
    line_sep = chr(0x2028)
    with caplog.at_level("WARNING"):
        injected = client.get(
            "/api/weather", params={"city": f"Paris{line_sep}FAKE WARNING LINE"}
        )
    assert injected.status_code == 404
    assert line_sep not in caplog.text, "日志行分隔符必须净化"
    print("✓ /api/weather 天气查询（成功 / 422 / 404 / 503，失败详情脱敏）")


def test_weather_city_length_matches_trip_destination(monkeypatch):
    """天气查询城市上限与行程目的地上限一致（100 字），否则长目的地静默无天气。"""
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda city: {"city": city, "current": {}, "daily": []},
    )
    client = _client()
    long_city = "长" * 60
    data = client.get("/api/weather", params={"city": long_city})
    assert data.status_code == 200, data.text
    assert data.json()["city"] == long_city
    assert client.get("/api/weather", params={"city": "x" * 101}).status_code == 422
    print("✓ /api/weather 城市长度口径与行程目的地一致（100 字）")


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


def test_weather_cache_hit_reports_rate_headers(monkeypatch):
    """缓存命中的 200 也带额度头，且不消耗额度（同一端点 200 形状一致）。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    payload = {"city": "北京", "current": {"description": "晴"}, "daily": []}
    monkeypatch.setattr(
        server_module, "peek_cached_weather", lambda name: dict(payload)
    )
    calls = []
    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda city: calls.append(city) or dict(payload),
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    app.state.aligo.weather_limiter = RateLimiter(limit=2, window_sec=60)
    client = TestClient(app)

    first = client.get("/api/weather", params={"city": "北京"})
    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "2"
    assert first.headers["x-ratelimit-remaining"] == "2", "缓存命中不消耗额度"
    assert "x-ratelimit-reset" in first.headers

    second = client.get("/api/weather", params={"city": "北京"})
    assert second.headers["x-ratelimit-remaining"] == "2"
    assert calls == [], "缓存命中不能访问上游"

    # 缓存失效后的真实请求仍可使用完整额度（并附带递减后的额度头）
    monkeypatch.setattr(server_module, "peek_cached_weather", lambda name: None)
    third = client.get("/api/weather", params={"city": "北京"})
    assert third.status_code == 200
    assert third.headers["x-ratelimit-remaining"] == "1"
    assert calls == ["北京"]
    print("✓ /api/weather 缓存命中附带额度头且不消耗额度")


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


def test_status_storage_description_does_not_wait_for_redis(monkeypatch):
    """/api/status 只为展示后端名：不能等 Redis 客户端创建波次（占线程池）。"""
    from webapp import server as server_module

    calls: list[bool] = []

    def fake_describe(wait_for_create: bool = True) -> dict:
        calls.append(wait_for_create)
        return {
            "short_term": "memory",
            "long_term": "json",
            "short_term_degraded": False,
            "long_term_degraded": False,
        }

    monkeypatch.setattr(server_module, "describe_backends", fake_describe)
    response = _client().get("/api/status", params={"user_id": "web_test_status_wait"})
    assert response.status_code == 200, response.text
    assert calls == [False], calls
    print("✓ /api/status 不等 Redis 创建波次")


def test_reserved_device_user_id_rejected():
    """Windows 设备保留名不能作 user_id：会拼成记忆库文件名并指向设备。"""
    client = _client()
    for user_id in ("NUL", "nul", "CON", "com1", "LPT9"):
        response = client.get("/api/status", params={"user_id": user_id})
        assert response.status_code == 422, (user_id, response.text)
    # 对话入口经 ConversationRuntime 直接创建长期记忆：同样必须拒绝
    chat = client.post("/api/chat", json={"message": "你好", "user_id": "NUL"})
    assert chat.status_code == 422, chat.text
    # 普通名字不受影响（含保留名前缀但不是保留名）
    accepted = ["console", "con-1", "com10", "nul_2"]
    try:
        for user_id in accepted:
            response = client.get("/api/status", params={"user_id": user_id})
            assert response.status_code == 200, (user_id, response.text)
    finally:
        for user_id in accepted:
            _cleanup(user_id)
    print("✓ 设备保留名 user_id 被拒绝")


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


def test_status_survives_date_max_trip():
    """终点 9999-12-31 的行程不能使 /api/status 永久 500（面板打不开）。"""
    client = _client()
    user_id = _unique_user("web_test_datemax")
    try:
        for start, end in (
            ("2020-01-01", "9999-12-31"),
            ("2026-03-01", "2026-03-05"),
        ):
            created = client.post(
                "/api/trips",
                json={
                    "user_id": user_id,
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": start,
                    "end_date": end,
                    "purpose": "出差",
                },
            )
            assert created.status_code == 201, created.text
        response = client.get("/api/status", params={"user_id": user_id})
        assert response.status_code == 200, response.text[:300]
        summary = response.json()["travel_summary"]
        assert summary["total_trips"] == 2, summary
        print("✓ /api/status 容忍 9999-12-31 终点行程")
    finally:
        _cleanup(user_id)


def test_chat_requires_explicit_user_id():
    """对话入口与其余数据接口同口径：user_id 必填，缺省 422。

    缺省回退固定账号会让省略字段的不同客户端共读写同一份记忆
    （偏好 / 历史摘要互相注入、幂等键跨客户端回放）。
    """
    client = _client()
    response = client.post("/api/chat", json={"message": "你好"})
    assert response.status_code == 422, response.text
    detail = response.json().get("detail") or []
    assert any("user_id" in str(item.get("loc", "")) for item in detail), detail
    streamed = client.post("/api/chat/stream", json={"message": "你好"})
    assert streamed.status_code == 422, streamed.text
    print("✓ /api/chat 缺少 user_id 返回 422")


def test_chat_returns_reply_and_usage():
    client = _client()
    # 固定 user_id 会把真实记忆文件写进仓库 data/memory 并跨轮次累积：
    # 每个用例用唯一账号，finally 清理
    user_id = _unique_user("web_test_chat")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-1"},
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
    finally:
        _cleanup(user_id)


def test_chat_idempotency_key_replays_without_rerun():
    """同一幂等键重放缓存响应；不同键仍真实执行。"""

    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-idem",
            "idempotency_key": "req-1",
        }
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        calls_after_first = counting.calls
        assert calls_after_first >= 1

        second = client.post("/api/chat", json=payload)
        assert second.status_code == 200, second.text
        assert second.headers.get("x-idempotent-replay") == "1"
        assert second.json() == first.json(), "重放必须返回同一份响应"
        assert counting.calls == calls_after_first, "重放不能重新执行模型"
        state = client.app.state.aligo
        session = state.sessions[state._session_key(user_id, "sess-idem")]
        assert session.in_use() is False, "回放后必须归还会话占用标记"

        third = client.post(
            "/api/chat", json={**payload, "idempotency_key": "req-2"}
        )
        assert third.status_code == 200, third.text
        assert counting.calls > calls_after_first, "不同幂等键应真实执行"
        print("✓ 幂等键重放不重复执行；不同键正常执行")
    finally:
        _cleanup(user_id)


def test_chat_idempotency_replay_blocked_after_session_delete():
    """会话删除后同键重试不能回放已删除会话的答案。"""

    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_deleted")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-replay-del",
            "idempotency_key": "req-del-1",
        }
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        deleted = client.delete(
            f"/api/sessions/sess-replay-del?user_id={user_id}"
        )
        assert deleted.status_code == 200, deleted.text

        replay = client.post("/api/chat", json=payload)
        assert replay.status_code == 404, replay.text
        assert "已删除" in replay.text
        assert replay.headers.get("x-idempotent-replay") is None

        # 删除最后一轮后同键重发：缓存已清，必须真实执行而不是回放旧答案
        payload2 = {
            "message": "第二问",
            "user_id": user_id,
            "session_id": "sess-replay-turn",
            "idempotency_key": "req-turn-1",
        }
        second = client.post("/api/chat", json=payload2)
        assert second.status_code == 200, second.text
        calls_after_second = counting.calls
        last_turn = client.delete(
            f"/api/sessions/sess-replay-turn/last-turn?user_id={user_id}"
            "&expect=第二问"
        )
        assert last_turn.status_code == 200, last_turn.text
        resent = client.post("/api/chat", json=payload2)
        assert resent.status_code == 200, resent.text
        assert resent.headers.get("x-idempotent-replay") is None, (
            "删除最后一轮后同键重发不能回放已删除的答案"
        )
        assert counting.calls > calls_after_second
        print("✓ 删除会话 / 最后一轮后幂等回放被拦截")
    finally:
        _cleanup(user_id)


def test_chat_idempotency_key_binds_message_content():
    """同一幂等键换了消息不能被回放：新提问必须真实执行。"""
    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_message")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-idem-msg",
            "idempotency_key": "req-same",
        }
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        calls_after_first = counting.calls
        second = client.post(
            "/api/chat", json={**payload, "message": "在吗"}
        )
        assert second.status_code == 200, second.text
        assert second.headers.get("x-idempotent-replay") is None, (
            "同键不同消息不能回放上一问"
        )
        assert counting.calls > calls_after_first
    finally:
        _cleanup(user_id)
    print("✓ 幂等键绑定消息内容")


def test_chat_idempotency_without_session_id_replays():
    """不带 session_id 的首条消息：同键重试回放首次响应（含生成的会话 id）。"""

    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_no_session")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "idempotency_key": "req-ns-1",
        }
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        calls_after_first = counting.calls
        session_id = first.json().get("session_id")
        assert session_id, first.json()

        second = client.post("/api/chat", json=payload)
        assert second.status_code == 200, second.text
        assert second.headers.get("x-idempotent-replay") == "1"
        assert second.json() == first.json()
        assert second.json().get("session_id") == session_id
        assert counting.calls == calls_after_first, "首条消息的重试不能重新执行"
        print("✓ 不带 session_id 的同键重试回放首次响应")
    finally:
        _cleanup(user_id)


def test_chat_stream_idempotency_without_session_id_replays():
    """流式不带 session_id：同键重试回放首次会话 id，不重新执行。"""

    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_stream_no_session")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "idempotency_key": "req-stream-ns",
        }

        def _events(response):
            return [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]

        with client.stream("POST", "/api/chat/stream", json=payload) as response:
            events = _events(response)
        assert events[-1]["type"] == "done"
        session_id = events[0]["session_id"]
        assert session_id
        calls_after_first = counting.calls

        with client.stream("POST", "/api/chat/stream", json=payload) as response:
            events2 = _events(response)
        assert [event["type"] for event in events2] == ["session", "done"]
        assert events2[0]["session_id"] == session_id, (
            "回放会话语必须使用首次执行的会话 id"
        )
        assert events2[0].get("idempotent_replay") is True
        assert events2[-1].get("idempotent_replay") is True
        assert counting.calls == calls_after_first, "首条消息的重试不能重新执行"
        print("✓ 流式不带 session_id 的同键重试回放首次会话")
    finally:
        _cleanup(user_id)


def test_chat_idempotency_replays_when_retry_sends_resolved_session_id():
    """首条消息（无 session_id）执行后，客户端带上服务端生成的会话 id
    同键重试：仍命中缓存回放，而不是重新执行。"""

    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_resolved_session")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "idempotency_key": "req-rs-1",
        }
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        calls_after_first = counting.calls
        session_id = first.json()["session_id"]
        assert session_id
        # 客户端在 session 事件里学会会话 id 后用同一键重试
        retry = client.post("/api/chat", json={**payload, "session_id": session_id})
        assert retry.status_code == 200, retry.text
        assert retry.headers.get("x-idempotent-replay") == "1"
        assert retry.json() == first.json()
        assert counting.calls == calls_after_first
        print("✓ 带上解析后会话 id 的同键重试仍回放")
    finally:
        _cleanup(user_id)


def test_remember_idempotent_indexes_resolved_session():
    """幂等缓存双写：请求会话键与解析后的会话键都能取到同一负载。"""
    from utils.idempotency import IdempotencyCache, make_key

    from webapp.server import _remember_idempotent

    cache = IdempotencyCache()
    primary = make_key("u", "", "k", endpoint="chat", message="m")
    _remember_idempotent(
        cache,
        primary,
        user_id="u",
        request_session_id=None,
        resolved_session_id="S1",
        key="k",
        endpoint="chat",
        message="m",
        payload={"ok": True},
    )
    assert cache.get(primary) == {"ok": True}
    assert cache.get(
        make_key("u", "S1", "k", endpoint="chat", message="m")
    ) == {"ok": True}
    print("✓ 幂等缓存按解析后会话 id 双写")


def test_chat_incomplete_idempotency_marker_returns_409():
    """断连 499 落库成功后的标记：重试返回 409，不重放也不重复执行。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_incomplete")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-idem-incomplete",
            "idempotency_key": "req-incomplete",
        }
        state = client.app.state.aligo
        key = make_key(
            user_id,
            "sess-idem-incomplete",
            "req-incomplete",
            endpoint="chat",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "session_id": "sess-idem-incomplete",
                "__incomplete__": True,
                "message": "上一请求已处理但响应未送达，请刷新会话历史",
            },
        )
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 409, response.text
        assert "刷新会话历史" in response.json()["detail"]
    finally:
        _cleanup(user_id)
    print("✓ 未完成幂等标记返回 409")


def test_chat_incomplete_marker_prefers_deleted_session_404():
    """未完成标记与「会话已删除」同时成立：与流式端点一致返回 404。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_incomplete_del")
    session_id = "sess-idem-incomplete-del"
    state = client.app.state.aligo
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": session_id,
            "idempotency_key": "req-incomplete-del",
        }
        key = make_key(
            user_id,
            session_id,
            "req-incomplete-del",
            endpoint="chat",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "session_id": session_id,
                "__incomplete__": True,
                "message": "上一请求已处理但响应未送达，请刷新会话历史",
            },
        )
        state.mark_deleted(user_id, session_id)
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 404, response.text
        assert "已删除" in response.json()["detail"]
        assert "X-Idempotent-Replay" not in response.headers
    finally:
        state.unmark_deleted(user_id, session_id)
        _cleanup(user_id)
    print("✓ 未完成标记 + 会话删除优先返回 404")


def test_persist_guard_skips_pending_tombstone_replay(monkeypatch):
    """存储落库守卫不触发待补写墓碑的补写（避免在写锁内做数十次 Redis 往返）。"""
    client = _client()
    state = client.app.state.aligo
    calls = {"n": 0}

    monkeypatch.setattr(
        state, "_maybe_replay_pending", lambda: calls.__setitem__("n", calls["n"] + 1)
    )
    assert state.is_recently_deleted("u-guard", "s-guard", replay_pending=False) is False
    assert calls["n"] == 0, "落库守卫路径不能补写墓碑"
    state.is_recently_deleted("u-guard", "s-guard")
    assert calls["n"] == 1, "入口检查仍负责补写"
    print("✓ 落库守卫跳过待补写墓碑")


def test_persist_guard_force_reads_shared_tombstone_during_cooldown():
    """共享墓碑重试窗口内，落库守卫（force_shared）仍真实读取一次。"""
    import time as time_module

    client = _client()
    state = client.app.state.aligo
    original = state._redis_client
    calls: list[bool] = []

    class _Shared:
        def exists(self, key):
            calls.append(True)
            return 1

    state._redis_client = _Shared()
    state._tombstones_shared = False
    state._tombstone_retry_at = time_module.monotonic() + 60
    try:
        assert (
            state.is_recently_deleted(
                "u-guard", "s-guard", replay_pending=False, force_shared=True
            )
            is True
        ), "冷却期内共享墓碑仍须挡住已删除会话"
        assert calls == [True]
        calls.clear()
        # 成功的共享读取会自愈重试窗口；重新置回窗口内验证非关键路径的省略
        state._tombstones_shared = False
        state._tombstone_retry_at = time_module.monotonic() + 60
        assert (
            state.is_recently_deleted("u-guard", "s-guard", replay_pending=False)
            is False
        ), "非关键路径在重试窗口内仍回退进程内墓碑"
        assert calls == []
    finally:
        state._redis_client = original
        state._tombstones_shared = True
        state._tombstone_retry_at = 0.0
    print("✓ 落库守卫在墓碑冷却期强制读取共享墓碑")


def test_chat_lock_released_when_disconnect_probe_raises():
    """拿到用户锁后断连探测抛异常：锁必须归还，不能永久 locked。"""
    import asyncio

    from webapp.server import _acquire_chat_lock

    class _BoomRequest:
        async def is_disconnected(self):
            raise RuntimeError("receive boom")

    async def scenario():
        lock = asyncio.Lock()
        with pytest.raises(RuntimeError):
            await _acquire_chat_lock(_BoomRequest(), lock, "rid-boom")
        assert lock.locked() is False, "异常路径也必须释放用户锁"
        await lock.acquire()
        lock.release()

    asyncio.run(scenario())
    print("✓ 断连探测异常时归还用户锁")


def test_leased_streaming_response_settles_after_drain():
    """流式响应在 stop_turn（drain）之后才执行断连标记回调。"""
    from webapp.server import _LeasedStreamingResponse

    order: list[str] = []
    drained = {"value": False}

    class _Lease:
        async def stop_turn(self):
            await asyncio.sleep(0)
            drained["value"] = True
            order.append("stop_turn")

        def after_stop(self):
            assert drained["value"] is True, "after_stop 必须在 drain 完成之后执行"
            order.append("after_stop")

        def release(self):
            order.append("release")

    async def _content():
        yield b"data: {}\n\n"

    async def _run():
        response = _LeasedStreamingResponse(_content(), _Lease())
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        async def receive():
            # 必须有真实的挂起点：立即返回的协程不让出事件循环，Starlette
            # 的 listen_for_disconnect 会把它变成忙循环，响应体永远发不出去
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await response({"type": "http", "method": "GET"}, receive, send)
        return sent

    sent = asyncio.run(_run())
    assert order == ["stop_turn", "after_stop", "release"], order
    assert any(message.get("type") == "http.response.body" for message in sent)
    print("✓ 流式响应在 drain 之后执行收尾回调")


def test_stream_abort_flag_recorded_as_499():
    """流式断连（终态未送达）由响应包装器打标：日志 / 指标记 499、不计耗时。"""
    from utils.metrics import REQUEST_METRICS
    from webapp.server import RequestContextMiddleware

    REQUEST_METRICS.reset()

    async def stream_app(scope, receive, send):
        scope.setdefault("state", {})["aligo_stream_aborted"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: {}\n\n"})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestContextMiddleware(stream_app)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.sleep(0)
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [(b"host", b"127.0.0.1")],
    }
    asyncio.run(middleware(scope, receive, send))
    snapshot = REQUEST_METRICS.snapshot()
    assert snapshot["by_status"].get("499") == 1, snapshot
    assert "200" not in snapshot["by_status"], snapshot
    assert snapshot["duration_samples"] == 0, "499 不能计入耗时分位"
    assert any(m.get("type") == "http.response.start" for m in sent)
    REQUEST_METRICS.reset()
    print("✓ 流式断连标记记 499（线上保持 200）")


def test_leased_streaming_abort_flags_and_closes_generator():
    """send 抛错（断连）时：显式关闭生成器、打断连标记、租约照常释放。"""
    from webapp.server import _LeasedStreamingResponse

    events: list[str] = []

    class _Lease:
        async def stop_turn(self):
            events.append("stop")

        def after_stop(self):
            events.append("after")

        def release(self):
            events.append("release")

    async def body():
        try:
            yield b"data: {}\n\n"
            yield b"data: {}\n\n"
        finally:
            events.append("closed")

    async def _run():
        # 不再依赖 turn_started（幂等回放流 / 首帧发送前的中断也要记账）
        state = {"terminal_delivered": False}
        response = _LeasedStreamingResponse(body(), _Lease(), stream_state=state)
        sent: list[dict] = []

        async def send(message):
            if message.get("type") == "http.response.body" and message.get("body"):
                if sent:
                    raise OSError("broken pipe")
            sent.append(message)

        async def receive():
            await asyncio.sleep(0)
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/stream",
            "headers": [(b"host", b"127.0.0.1")],
            "asgi": {"spec_version": "2.4"},
            "state": {},
        }
        try:
            await response(scope, receive, send)
        except Exception:  # noqa: BLE001 - 断连异常向上传播给服务器
            pass
        return scope

    scope = asyncio.run(_run())
    assert scope["state"].get("aligo_stream_aborted") is True, scope
    assert "closed" in events, "断连后必须显式关闭生成器（取消心跳 / 落定状态）"
    assert events[-1] == "release", events

    # 终态已送达的完整流：send 抛错发生在所有帧之后也不能打标
    async def _complete_run():
        state = {"terminal_delivered": True}
        response = _LeasedStreamingResponse(body(), _Lease(), stream_state=state)
        scope = {"type": "http", "state": {}}

        async def send(message):
            return None

        async def receive():
            await asyncio.sleep(0)
            return {"type": "http.request"}

        await response(scope, receive, send)
        return scope

    complete_scope = asyncio.run(_complete_run())
    assert not complete_scope["state"].get("aligo_stream_aborted")
    print("✓ 流式断连关闭生成器并打标（终态送达不误报）")


def test_idempotent_replay_survives_open_breaker(monkeypatch):
    """熔断打开时已缓存结果仍可回放（零 LLM 开销）；未缓存的新请求仍 503。"""
    from utils.idempotency import make_key
    from webapp import server as server_module  # noqa: F401 - 确保模块已加载

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_idem_breaker")

    class _DenyBreaker:
        def denies_new_calls(self):
            return True

        def raise_if_open(self):
            raise AssertionError("回放路径不应进入对话")

    try:
        chat_key = make_key(
            user_id, "s-brk", "k-chat", endpoint="chat", message="你好"
        )
        state.idempotency.put(
            chat_key, {"session_id": "s-brk", "reply": "已缓存回答"}
        )
        stream_key = make_key(
            user_id,
            "s-brk",
            "k-stream",
            endpoint="chat_stream",
            message="流式问题",
        )
        state.idempotency.put(
            stream_key,
            {"session_id": "s-brk", "type": "done", "reply": "流式缓存"},
        )
        monkeypatch.setattr(state, "breaker", _DenyBreaker(), raising=False)

        resp = client.post(
            "/api/chat",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "s-brk",
                "idempotency_key": "k-chat",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("x-idempotent-replay") == "1"
        assert resp.json()["reply"] == "已缓存回答"

        denied = client.post(
            "/api/chat",
            json={
                "message": "新问题",
                "user_id": user_id,
                "session_id": "s-brk",
                "idempotency_key": "k-other",
            },
        )
        assert denied.status_code == 503, denied.text

        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "流式问题",
                "user_id": user_id,
                "session_id": "s-brk",
                "idempotency_key": "k-stream",
            },
        ) as stream:
            assert stream.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in stream.iter_lines()
                if line.startswith("data:")
            ]
        assert any(
            event.get("type") == "done" and event.get("idempotent_replay")
            for event in events
        ), events
    finally:
        _cleanup(user_id)
    print("✓ 熔断打开时已缓存结果仍可回放")


def test_breaker_rechecked_when_idem_cache_misses_inside_lock(monkeypatch):
    """入口缓存预检命中、锁内已被淘汰：熔断仍要拒绝（不能回到重路径）。"""
    from utils.idempotency import make_key

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_breaker_recheck")
    key = make_key(user_id, "s-br2", "k-1", endpoint="chat", message="你好")

    calls = {"n": 0}
    real_get = state.idempotency.get

    def flaky_get(entry_key):
        if entry_key == key:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"session_id": "s-br2", "reply": "cached"}
            return None
        return real_get(entry_key)

    class _DenyBreaker:
        def denies_new_calls(self):
            return True

        def raise_if_open(self):
            raise AssertionError("不应进入对话")

    try:
        monkeypatch.setattr(state.idempotency, "get", flaky_get)
        monkeypatch.setattr(state, "breaker", _DenyBreaker(), raising=False)
        response = client.post(
            "/api/chat",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "s-br2",
                "idempotency_key": "k-1",
            },
        )
        assert response.status_code == 503, response.text
    finally:
        _cleanup(user_id)
    print("✓ 锁内缓存 miss 时补熔断复核（503）")


def test_breaker_rechecked_for_request_without_idem_key(monkeypatch):
    """无幂等键的对话请求在锁内同样复核熔断：不能白扣额度、白建会话。"""
    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_breaker_no_key")
    calls = {"n": 0}

    class _LateBreaker:
        def denies_new_calls(self):
            calls["n"] += 1
            return calls["n"] > 1

        def raise_if_open(self):
            raise AssertionError("不应进入对话")

    def _no_session(*args, **kwargs):
        raise AssertionError("不应构造会话")

    try:
        monkeypatch.setattr(state, "breaker", _LateBreaker(), raising=False)
        monkeypatch.setattr(state, "get_session", _no_session, raising=False)
        response = client.post(
            "/api/chat",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "s-nokey",
            },
        )
        assert response.status_code == 503, response.text
        calls["n"] = 0
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "流式问题",
                "user_id": user_id,
                "session_id": "s-nokey",
            },
        ) as stream:
            assert stream.status_code == 503
    finally:
        _cleanup(user_id)
    print("✓ 无幂等键请求在锁内补熔断复核")


def test_chat_lock_checks_disconnect_after_uncontended_acquire():
    """锁无竞争直接拿到时也要检查断连：不能为已断开的客户端构造会话。"""
    from webapp.server import _acquire_chat_lock

    class _Req:
        async def is_disconnected(self):
            return True

    lock = asyncio.Lock()

    async def _run():
        try:
            await _acquire_chat_lock(_Req(), lock, "rid-lock")
        except Exception as exc:  # noqa: BLE001 - HTTPException
            return getattr(exc, "status_code", None)
        return None

    status = asyncio.run(_run())
    assert status == 499, status
    assert not lock.locked(), "断连时已拿到的锁必须释放"
    print("✓ 对话锁拿到后补检断连（499 并释放锁）")


def test_chat_stream_idempotency_replay():
    """流式重复提交只回放 session + done，不重新执行 Agent。"""
    class _CountingModel:
        def __init__(self):
            self.calls = 0
            self._inner = _FakeModel()

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            return await self._inner(messages, **kwargs)

    counting = _CountingModel()
    client = TestClient(create_app(model=UsageTrackingModel(counting)))
    user_id = _unique_user("web_test_idem_stream")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-idem-stream",
            "idempotency_key": "req-stream-1",
        }

        def _events(response):
            return [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]

        with client.stream("POST", "/api/chat/stream", json=payload) as response:
            events = _events(response)
        assert events[-1]["type"] == "done"
        calls_after_first = counting.calls
        assert calls_after_first >= 1

        with client.stream("POST", "/api/chat/stream", json=payload) as response:
            assert response.status_code == 200
            events2 = _events(response)
        assert [event["type"] for event in events2] == ["session", "done"]
        assert events2[0].get("idempotent_replay") is True
        assert events2[-1].get("idempotent_replay") is True
        assert events2[-1]["reply"] == events[-1]["reply"]
        assert counting.calls == calls_after_first
        print("✓ 流式幂等键只回放 session + done")
    finally:
        _cleanup(user_id)


def test_chat_stream_incomplete_marker_replays_error():
    """流式：未完成幂等标记回放 error 终态，不重放 done、不重新执行。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_stream_incomplete")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-idem-stream-inc",
            "idempotency_key": "req-stream-inc",
        }
        state = client.app.state.aligo
        key = make_key(
            user_id,
            "sess-idem-stream-inc",
            "req-stream-inc",
            endpoint="chat_stream",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "session_id": "sess-idem-stream-inc",
                "__incomplete__": True,
                "message": "上一请求已处理但响应未送达，请刷新会话历史",
            },
        )
        with client.stream(
            "POST", "/api/chat/stream", json=payload
        ) as response:
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert [event["type"] for event in events] == ["session", "error"], (
            events
        )
        assert events[-1].get("idempotent_replay") is True
        # 该标记只在整轮已落库时写下：回放必须声明 persisted=true，
        # 前端据此清除「临时提问」标记而不是误判未保存
        assert events[-1].get("persisted") is True, events[-1]
        assert "刷新会话历史" in events[-1]["message"]
    finally:
        _cleanup(user_id)
    print("✓ 流式未完成幂等标记回放 error（带 persisted）")


def test_cross_endpoint_incomplete_marker_returns_409():
    """流式端点写入的「未完成」标记：换到非流式端点同键重试也返回 409。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_cross_chat")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-cross-chat",
            "idempotency_key": "req-cross-chat",
        }
        state = client.app.state.aligo
        key = make_key(
            user_id,
            "sess-cross-chat",
            "req-cross-chat",
            endpoint="chat_stream",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "session_id": "sess-cross-chat",
                "__incomplete__": True,
                "message": "上一请求已处理但响应未送达，请刷新会话历史",
            },
        )
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 409, response.text
        assert "刷新会话历史" in response.json()["detail"]
    finally:
        _cleanup(user_id)
    print("✓ 跨端点未完成标记在非流式端点返回 409")


def test_cross_endpoint_incomplete_marker_replays_error_in_stream():
    """非流式端点写入的「未完成」标记：流式端点同键重试回放 error 终态。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_cross_stream")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-cross-stream",
            "idempotency_key": "req-cross-stream",
        }
        state = client.app.state.aligo
        key = make_key(
            user_id,
            "sess-cross-stream",
            "req-cross-stream",
            endpoint="chat",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "session_id": "sess-cross-stream",
                "__incomplete__": True,
                "message": "上一请求已处理但响应未送达，请刷新会话历史",
            },
        )
        with client.stream(
            "POST", "/api/chat/stream", json=payload
        ) as response:
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert [event["type"] for event in events] == ["session", "error"], (
            events
        )
        assert events[-1].get("persisted") is True
    finally:
        _cleanup(user_id)
    print("✓ 跨端点未完成标记在流式端点回放 error")


def test_cross_endpoint_success_payload_is_not_replayed():
    """另一端点缓存的成功负载形状不同：不能跨端点回放，按新请求执行。"""
    from utils.idempotency import make_key

    client = _client()
    user_id = _unique_user("web_test_idem_cross_success")
    try:
        payload = {
            "message": "你好",
            "user_id": user_id,
            "session_id": "sess-cross-success",
            "idempotency_key": "req-cross-success",
        }
        state = client.app.state.aligo
        key = make_key(
            user_id,
            "sess-cross-success",
            "req-cross-success",
            endpoint="chat_stream",
            message="你好",
        )
        state.idempotency.put(
            key,
            {
                "type": "done",
                "session_id": "sess-cross-success",
                "reply": "cached-must-not-replay",
            },
        )
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200, response.text
        assert response.json().get("reply") != "cached-must-not-replay"
    finally:
        _cleanup(user_id)
    print("✓ 跨端点成功负载不回放，按新请求执行")


def test_terminal_frame_delivered_probe():
    """终态送达探测：未断连=True、已断连=False、探测异常按旧口径=True。"""
    from webapp.server import _terminal_frame_delivered

    class _Req:
        def __init__(self, value):
            self.value = value

        async def is_disconnected(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    async def scenario():
        assert await _terminal_frame_delivered(_Req(False)) is True
        assert await _terminal_frame_delivered(_Req(True)) is False
        assert (
            await _terminal_frame_delivered(_Req(RuntimeError("probe boom")))
            is True
        )

    asyncio.run(scenario())
    print("✓ 终态送达探测（异常不改变流行为）")


def test_stream_internal_error_recorded_as_500():
    """响应体自身抛错：scope 打 failed 标（不是 aborted），中间件记 500、不计耗时。"""
    from utils.metrics import REQUEST_METRICS
    from webapp.server import RequestContextMiddleware, _LeasedStreamingResponse

    REQUEST_METRICS.reset()

    class _Lease:
        async def stop_turn(self):
            return None

        def after_stop(self):
            return None

        def release(self):
            return None

    async def body():
        yield b"data: {}\n\n"
        raise RuntimeError("stream bug")

    async def stream_app(scope, receive, send):
        response = _LeasedStreamingResponse(
            body(), _Lease(), stream_state={"terminal_delivered": False}
        )
        await response(scope, receive, send)

    middleware = RequestContextMiddleware(stream_app)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.sleep(0)
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [(b"host", b"127.0.0.1")],
        "asgi": {"spec_version": "2.3"},
        "state": {},
    }
    try:
        asyncio.run(middleware(scope, receive, send))
        raise AssertionError("流内异常必须向上传播")
    except RuntimeError as propagated:
        # 交给 uvicorn 的占位异常不能携带原始错误文本：ServerErrorMiddleware
        # 会原样再抛，uvicorn 的 exc_info 会把未脱敏 traceback 再写一遍
        assert "stream bug" not in str(propagated), propagated
        assert "请求 ID" in str(propagated), propagated
    assert scope["state"].get("aligo_stream_failed") is True, scope
    assert not scope["state"].get("aligo_stream_aborted"), scope
    snapshot = REQUEST_METRICS.snapshot()
    assert snapshot["by_status"].get("500") == 1, snapshot
    assert "499" not in snapshot["by_status"], snapshot
    assert snapshot["duration_samples"] == 0, "流式 500 不能计入延迟分位"
    assert any(m.get("type") == "http.response.start" for m in sent)
    REQUEST_METRICS.reset()
    print("✓ 流内异常按 500 记账（不再伪装客户端中止）")


def test_cancel_turn_with_budget_ignores_turn_failure():
    """回合在取消前已自行失败：收尾不能把它的异常抛出顶替断连口径。"""
    from webapp.server import _cancel_turn_with_budget

    async def scenario():
        async def failing():
            raise RuntimeError("turn failed")

        task = asyncio.ensure_future(failing())
        await asyncio.sleep(0)
        # 不得抛出：端点的断连 / 取消记账继续按原路径进行
        await _cancel_turn_with_budget(None, task)
        assert task.done()

    asyncio.run(scenario())
    print("✓ 收尾时回合已失败不顶替断连口径")


def test_ask_cancellable_probe_error_stops_turn():
    """断连探测自身抛异常：先取消并等待回合停稳，再向外抛。"""
    from webapp.server import _ask_cancellable_on_disconnect

    cancelled = {"value": False}

    async def long_turn():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise

    class _BadRequest:
        async def is_disconnected(self):
            raise RuntimeError("receive boom")

    async def scenario():
        try:
            await _ask_cancellable_on_disconnect(
                long_turn(), _BadRequest(), poll_interval=0.01
            )
        except RuntimeError as exc:
            assert "receive boom" in str(exc)
        else:  # pragma: no cover - 明确失败
            raise AssertionError("探测异常必须向外传播")

    asyncio.run(scenario())
    assert cancelled["value"] is True, "探测异常时在途回合必须被取消"
    print("✓ 断连探测异常时先停稳回合再抛出")


def test_chat_stream_disconnect_after_session_build_returns_499(monkeypatch):
    """取锁后、回合启动前客户端断开：返回 499，不为无人接收的请求开回合。"""
    from starlette.requests import Request

    calls = {"n": 0}

    async def fake_is_disconnected(self):
        calls["n"] += 1
        # 取锁路径的探测放行；会话构造后的复检报告断开
        return calls["n"] >= 2

    monkeypatch.setattr(Request, "is_disconnected", fake_is_disconnected)
    client = _client()
    user_id = _unique_user("web_test_stream_dc_build")
    try:
        response = client.post(
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "sess-dc-build",
            },
        )
        assert response.status_code == 499, response.text[:300]
        assert calls["n"] >= 2
    finally:
        _cleanup(user_id)
    print("✓ 会话构造后断连返回 499")


def test_chat_stream_task_without_terminal_emits_error(monkeypatch):
    """回合协程无终态退出（BaseException）：消费循环兜底发 error 而不是永久挂起。"""
    from agents.conversation import ConversationRuntime

    class _Fatal(BaseException):
        pass

    async def _fatal(self, message, on_event=None, turn_id="", **_kwargs):
        raise _Fatal("boom")

    monkeypatch.setattr(ConversationRuntime, "ask", _fatal)
    client = _client()
    user_id = _unique_user("web_test_stream_no_terminal")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "sess-no-terminal",
            },
        ) as response:
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert events, "至少应有 session 与 error 帧"
        assert events[-1]["type"] == "error", events
        assert "内部错误" in events[-1]["message"]
    finally:
        _cleanup(user_id)
    print("✓ 回合无终态退出时兜底发送 error")


def test_chat_creates_session_when_missing():
    user_id = _unique_user("web_test_chat")
    try:
        response = _client().post(
            "/api/chat", json={"message": "你好", "user_id": user_id}
        )
        assert response.status_code == 200
        session_id = response.json()["session_id"]
        assert isinstance(session_id, str) and len(session_id) == 12
        print("✓ 未提供会话时自动创建")
    finally:
        _cleanup(user_id)


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


def test_tombstone_ttl_covers_inflight_turn_lease():
    """墓碑有效期不短于回合占用租约：迟到的落库在整段租约内都被否决。"""
    from webapp.server import AligoWebApp

    assert AligoWebApp.tombstone_ttl_sec >= AligoWebApp.session_turn_leak_sec
    print("✓ 墓碑 TTL 覆盖在途回合生命周期")


def test_cli_tombstone_default_ttl_matches_web():
    """CLI 共享墓碑默认 TTL 必须与 Web 一致：CLI 删除同样要挡住迟到落库。"""
    from utils import tombstones
    from webapp.server import AligoWebApp

    assert (
        tombstones.DEFAULT_TOMBSTONE_TTL_SEC
        == AligoWebApp.tombstone_ttl_sec
    ), "两端墓碑 TTL 漂移会让 CLI 删除只保护 5 分钟"
    print("✓ CLI 与 Web 墓碑默认 TTL 一致")


def test_stream_and_chat_sanitize_model_surrogates(monkeypatch):
    """模型输出含孤立代理项：SSE 帧与 JSON 响应都必须转义后才能编码。

    SSE / JSONResponse 都按 UTF-8 传输：孤立代理项会让 200 流没有
    error/done 直接中断（Starlette 编码抛 UnicodeEncodeError）。
    """
    from agents.conversation import ConversationRuntime

    lonely = chr(0xD800)

    async def _fake_ask(self, message, on_event=None, turn_id="", **_kwargs):
        if on_event:
            on_event(
                {
                    "type": "plan",
                    "intents": [],
                    "reasoning": "推理" + lonely,
                    "schedule": [],
                }
            )
        return {
            "reply": "回答" + lonely,
            "agents": [],
            "usage": {},
            "elapsed": 0.0,
        }

    monkeypatch.setattr(ConversationRuntime, "ask", _fake_ask)
    client = _client()
    user_id = _unique_user("web_test_surrogate")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-sur"},
        ) as response:
            assert response.status_code == 200
            raw = b"".join(response.iter_bytes())
        text = raw.decode("utf-8")  # 编码失败会在这里抛 UnicodeDecodeError
        assert "ud800" in text, "孤立代理项必须转义成可传输文本"
        events = [
            json.loads(line[5:].strip())
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        assert events[-1]["type"] == "done", events[-1]
        assert lonely not in events[1]["reasoning"]
        assert lonely not in events[-1]["reply"]

        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-sur2"},
        )
        assert chat.status_code == 200, chat.text
        assert lonely not in chat.json()["reply"]
        assert "ud800" in chat.json()["reply"]
    finally:
        _cleanup(user_id)
    print("✓ SSE / JSON 响应转义模型输出的孤立代理项")


def test_ask_cancellable_on_disconnect():
    """非流式回合在客户端断开后取消；正常完成返回结果。"""
    import asyncio

    from webapp.server import _ask_cancellable_on_disconnect

    class _Req:
        def __init__(self, disconnect_after):
            self.calls = 0
            self.disconnect_after = disconnect_after

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= self.disconnect_after

    async def scenario():
        cancelled = {"v": False}

        async def slow():
            try:
                await asyncio.sleep(10)
                return {"reply": "late"}
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        result, disconnected = await _ask_cancellable_on_disconnect(
            slow(), _Req(2), poll_interval=0.01
        )
        assert (result, disconnected) == (None, True), (result, disconnected)
        assert cancelled["v"] is True, "断连后必须在途回合被取消"

        async def quick():
            return {"reply": "ok"}

        result, disconnected = await _ask_cancellable_on_disconnect(
            quick(), _Req(999), poll_interval=0.01
        )
        assert result == {"reply": "ok"} and disconnected is False

    asyncio.run(scenario())
    print("✓ 非流式回合在客户端断开后取消")


def test_disconnect_wiring_shares_budget_and_cancels_once():
    """server 接线：断连取消前建立共享预算；stop_turn 只显式 cancel 一次。"""
    import asyncio

    from webapp.server import _SessionTurnLease, _ask_cancellable_on_disconnect

    class _Req:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls >= 2

    class _Runtime:
        def __init__(self, order):
            self.order = order
            self.deadlines: list[float | None] = []

        def begin_drain_cleanup(self, deadline=None):
            self.deadlines.append(deadline)
            self.order.append("begin")

        async def drain_pending_persist(self, timeout_sec=10.0):
            return True

    class _FakeTask:
        def __init__(self, order):
            self.cancels = 0
            self.order = order

        def done(self):
            return False

        def cancel(self):
            self.cancels += 1
            self.order.append("explicit-cancel")

        def __await__(self):
            if False:  # pragma: no cover - 只用于成为生成器
                yield
            return None

    class _Session:
        runtime = None

        def end_turn(self):
            pass

    async def scenario():
        order: list[str] = []

        async def turn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # 预算必须在取消投递前建立：回滚 / 暂存等待才能扣减同一截止时刻
                assert order and order[0] == "begin", order
                order.append("cancel")
                raise

        runtime = _Runtime(order)
        result, disconnected = await _ask_cancellable_on_disconnect(
            turn(), _Req(), poll_interval=0.01, runtime=runtime
        )
        assert (result, disconnected) == (None, True), (result, disconnected)
        assert order == ["begin", "cancel"], order

        # 端点自身被取消（服务关闭 / 上层取消）：同样先建预算，取消后等
        # 回合收尾完成才向外抛——否则端点 finally 的 drain 会在收尾任务
        # 建立前返回，用户锁先于旧回合释放
        class _NeverDisconnect:
            async def is_disconnected(self):
                return False

        order2: list[str] = []

        async def slow_turn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                order2.append("cancelled")
                await asyncio.sleep(0.05)
                order2.append("cleanup-done")
                raise

        runtime2 = _Runtime(order2)
        wrapper = asyncio.ensure_future(
            _ask_cancellable_on_disconnect(
                slow_turn(), _NeverDisconnect(), poll_interval=0.01, runtime=runtime2
            )
        )
        await asyncio.sleep(0.05)
        wrapper.cancel()
        try:
            await wrapper
        except asyncio.CancelledError:
            pass
        assert order2 == ["begin", "cancelled", "cleanup-done"], order2

        # stop_turn：显式 cancel 只发一次；两次调用共享同一 drain 截止时刻
        lock = asyncio.Lock()
        await lock.acquire()
        lease = _SessionTurnLease(lock, _Session())
        fake_task = _FakeTask(order)
        lease.bind_turn(fake_task)
        await lease.stop_turn()
        deadline = lease._drain_deadline
        await lease.stop_turn()
        assert fake_task.cancels == 1, fake_task.cancels
        assert lease._drain_deadline == deadline, "两次 stop_turn 必须共享同一预算"
        lease.release()

    asyncio.run(scenario())
    print("✓ 断连接线先建预算后取消，stop_turn 单次 cancel 且共享预算")


def test_chat_disconnect_returns_499(monkeypatch):
    """非流式对话断连：返回 499（而非 200 假成功），指标按 499 记账。"""
    from utils.metrics import REQUEST_METRICS
    from webapp import server as server_module

    async def fake_ask(coro, *args, **kwargs):
        coro.close()
        return None, True

    monkeypatch.setattr(server_module, "_ask_cancellable_on_disconnect", fake_ask)
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    user_id = _unique_user("web_test_disconnect_499")
    REQUEST_METRICS.reset()
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "dc-1"},
        )
        assert response.status_code == 499, response.text[:300]
        data = response.json()
        assert data.get("cancelled") is True and data.get("session_id"), data
        snapshot = REQUEST_METRICS.snapshot()
        assert snapshot["by_status"].get("499") == 1, snapshot
        assert snapshot["by_status"].get("200", 0) == 0, snapshot
    finally:
        _cleanup(user_id)
        REQUEST_METRICS.reset()
    print("✓ 非流式断连返回 499 并按 499 记账")


def test_acquire_chat_lock_disconnect_raises_499():
    """排队等用户锁期间客户端断开：抛 499 而不是 503（响应已无人接收）。"""
    from fastapi import HTTPException

    from webapp.server import _acquire_chat_lock

    class _Gone:
        async def is_disconnected(self):
            return True

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        try:
            await _acquire_chat_lock(_Gone(), lock, "rid-test", timeout=0.05)
            raise AssertionError("断开连接时应抛出 HTTPException")
        finally:
            lock.release()

    try:
        asyncio.run(scenario())
    except HTTPException as exc:
        assert exc.status_code == 499, exc.status_code
    print("✓ 等锁期间断连抛 499")


def test_stream_and_chat_report_unpersisted_turn(monkeypatch):
    """墓碑否决的回合：done / 响应必须带 persisted=false，不能伪装成功。"""
    from agents.conversation import ConversationRuntime

    async def _vetoed_ask(self, message, on_event=None, turn_id="", **_kwargs):
        return {
            "reply": "回答",
            "agents": [],
            "usage": {},
            "elapsed": 0.1,
            "persisted": False,
        }

    monkeypatch.setattr(ConversationRuntime, "ask", _vetoed_ask)
    client = _client()
    user_id = _unique_user("web_test_veto_signal")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-veto"},
        ) as response:
            assert response.status_code == 200
            raw = b"".join(response.iter_bytes())
        text = raw.decode("utf-8")
        events = [
            json.loads(line[5:].strip())
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        assert events[-1]["type"] == "done", events[-1]
        assert events[-1]["persisted"] is False, events[-1]

        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "sess-veto2"},
        )
        assert chat.status_code == 200, chat.text
        assert chat.json()["persisted"] is False, chat.json()
    finally:
        _cleanup(user_id)
    print("✓ 未落库回合的 persisted=false 透传到 SSE 与 JSON 响应")


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


def test_status_survives_overflowing_elapsed_history():
    """历史 _meta.elapsed 溢出 / NaN 时状态接口不能变 500（avg_elapsed 保持有限）。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_elapsed_overflow")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_messages(
                [
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {"results": [], "_meta": {"elapsed": 1e308}}
                        ),
                        "session_id": "s-elapsed",
                    },
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {"results": [], "_meta": {"elapsed": 1e308}}
                        ),
                        "session_id": "s-elapsed",
                    },
                    {
                        "role": "assistant",
                        # 旧版本导入的 NaN 历史数据（json.loads 接受 NaN 字面量）
                        "content": '{"results": [], "_meta": {"elapsed": NaN}}',
                        "session_id": "s-elapsed",
                    },
                ]
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        status = _client().get("/api/status", params={"user_id": user_id})
        assert status.status_code == 200, status.text
        assert status.json()["usage"]["avg_elapsed"] == 0
        print("✓ 溢出 / NaN 的历史耗时不再让状态接口 500")
    finally:
        _cleanup(user_id)


def test_aware_timestamp_text_normalizes_backends():
    """JSON naive 与 PG aware 时间戳都输出为同一绝对时刻的 aware ISO。"""
    from utils.timestamps import to_aware
    from webapp.server import _aware_timestamp_text

    import re

    naive = _aware_timestamp_text("2026-11-05T10:01:00")
    assert naive != "2026-11-05T10:01:00", "naive 输入必须补上偏移"
    assert re.search(r"[+-]\d{2}:\d{2}$", naive), naive
    assert to_aware(naive).tzinfo is not None
    assert to_aware(naive) == to_aware("2026-11-05T10:01:00")
    aware = _aware_timestamp_text("2026-11-05T10:01:00+00:00")
    assert aware == "2026-11-05T10:01:00+00:00"
    assert _aware_timestamp_text("t") == "t"
    assert _aware_timestamp_text(None) is None
    print("✓ 会话时间戳归一为 aware ISO")


def test_aware_trip_view_normalizes_timestamp_and_conflicts():
    """行程视图（含 conflicts 子项）的 timestamp 与消息同口径归一。"""
    from datetime import datetime

    from webapp.server import _aware_trip_view, _aware_trip_views

    trip = {
        "trip_id": "t1",
        "start_date": "2026-11-28",
        "timestamp": "2026-11-28T11:00:00",
        "conflicts": [
            {"trip_id": "t2", "timestamp": "2026-11-28T12:00:00"}
        ],
    }
    view = _aware_trip_view(trip)
    assert view["timestamp"] != "2026-11-28T11:00:00"
    assert datetime.fromisoformat(view["timestamp"]).tzinfo is not None
    conflict = view["conflicts"][0]
    assert datetime.fromisoformat(conflict["timestamp"]).tzinfo is not None
    # 只复制视图，不改存储行
    assert trip["timestamp"] == "2026-11-28T11:00:00"
    assert _aware_trip_views([trip])[0]["trip_id"] == "t1"
    assert _aware_trip_views(None) is None
    print("✓ 行程视图时间戳归一（含冲突子项）")


def test_chat_request_accepts_browser_timezone():
    """ChatRequest 接受可选时区（相对日期按浏览器时区解释的入口）。"""
    from webapp.server import ChatRequest

    assert (
        ChatRequest(message="明天去上海", user_id="u1", timezone="Asia/Shanghai")
        .timezone
        == "Asia/Shanghai"
    )
    assert ChatRequest(message="hi", user_id="u1").timezone is None
    print("✓ 对话请求携带浏览器时区")


def test_session_updated_at_uses_max_timestamp():
    """会话 updated_at 取消息最大时间戳：恢复旧备份不会让排序回拨。"""
    from webapp.server import _group_sessions

    history = [
        {
            "session_id": "s1",
            "role": "user",
            "content": "一",
            "timestamp": "2026-11-05T10:00:00",
        },
        {
            "session_id": "s1",
            "role": "assistant",
            "content": "二",
            "timestamp": "2026-11-05T10:01:00",
        },
        {
            "session_id": "s1",
            "role": "user",
            "content": "恢复的旧消息",
            "timestamp": "2026-11-01T09:00:00",
        },
        {
            "session_id": "s2",
            "role": "user",
            "content": "另一个会话",
            "timestamp": "2026-11-04T09:00:00",
        },
    ]
    sessions = _group_sessions(history, 1_000_000)
    by_id = {item["session_id"]: item for item in sessions}
    # 输出归一为 aware ISO：naive 本地时间补上服务器偏移（前端 new Date()
    # 才能按绝对时刻换算到浏览器时区）
    from utils.timestamps import to_aware

    import re

    updated = by_id["s1"]["updated_at"]
    assert updated.startswith("2026-11-05T10:01:00"), updated
    assert re.search(r"[+-]\d{2}:\d{2}$", updated), (
        "输出必须带时区偏移（恒等实现不能通过）"
    )
    assert to_aware(updated) == to_aware("2026-11-05T10:01:00")
    assert to_aware(updated).tzinfo is not None
    assert sessions[0]["session_id"] == "s1", "按最大时间戳倒序排列"
    print("✓ 会话 updated_at 取最大时间戳（恢复旧备份不回拨排序）")


def test_limiter_degraded_uses_probe_for_idle_buckets():
    """健康检查对降级桶走 probe：低频桶不需要再收到请求也能恢复上报。"""
    from webapp.server import _limiter_degraded

    class _Probed:
        def probe(self):
            return True

    class _ProbedDown:
        def probe(self):
            return False

    class _Legacy:
        degraded = True

    assert _limiter_degraded(_Probed()) is False
    assert _limiter_degraded(_ProbedDown()) is True
    assert _limiter_degraded(_Legacy()) is True, "无 probe 的实现回退到 degraded 标记"
    print("✓ 健康检查对降级桶优先主动探测")


def test_stream_abort_waits_for_pending_persist(monkeypatch):
    """SSE 中断时：落库线程未结束前不得释放用户锁（否则删除 / 新回合并发）。"""
    import contextlib

    import httpx

    from context.memory_manager import MemoryManager

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    user_id = _unique_user("web_abort_persist")
    started = threading.Event()
    release = threading.Event()
    original_persist = MemoryManager.persist_turn

    def blocked_persist(self, user_content, assistant_content, should_commit=None):
        started.set()
        release.wait(10)
        original_persist(
            self, user_content, assistant_content, should_commit=should_commit
        )

    monkeypatch.setattr(MemoryManager, "persist_turn", blocked_persist)
    lock = state.lock_for(user_id)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:

            async def consume():
                async with client.stream(
                    "POST",
                    "/api/chat/stream",
                    json={
                        "message": "你好",
                        "user_id": user_id,
                        "session_id": "sess-abort",
                    },
                ) as response:
                    async for _ in response.aiter_raw():
                        pass

            task = asyncio.create_task(consume())
            assert await asyncio.to_thread(started.wait, 10), "未进入落库阶段"
            task.cancel()
            # 落库线程仍被阻塞：用户锁必须保持持有（响应收尾不得提前放锁）
            for _ in range(25):
                await asyncio.sleep(0.02)
            assert lock.locked(), "落库未完成前释放了用户锁"
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not lock.locked():
                    break
            assert not lock.locked(), "落库完成后应释放用户锁"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _cleanup(user_id)
    print("✓ SSE 中断等待在途落库线程结束后才释放用户锁")


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
    long_category = "长分类" * 80  # 240 字：历史超长分类仍必须可筛选
    response = _client().get(
        "/api/knowledge/documents", params={"category": long_category}
    )
    assert response.status_code == 200, response.text
    assert calls["category"] == long_category
    print("✓ 分类筛选接受库内历史超长分类（200 只约束新写入）")


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
        async def ask(self, message, on_event=None, turn_id="", **_kwargs):
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


def test_redacted_exc_log_text_masks_key_in_traceback():
    """兜底日志文本（含 traceback）必须先脱敏，否则 exc_info 会绕开参数。"""
    from webapp.server import _redacted_exc_log_text

    key = "sk-CANARY1234567890abcdef"
    try:
        raise RuntimeError(
            f"Error code: 401 - Incorrect API key provided: {key}"
        )
    except RuntimeError as exc:
        text = _redacted_exc_log_text(exc)
    assert key not in text, text
    assert "***" in text and "RuntimeError" in text, text
    assert "Traceback" in text, "保留栈帧便于排查"


def test_unhandled_exception_log_is_redacted(caplog):
    """500 兜底日志不能把网关回显的 API Key 写进应用日志（不走 exc_info）。"""
    import logging

    from webapp.server import RequestContextMiddleware

    canary = "sk-CANARY1234567890abcdef"

    async def boom_app(scope, receive, send):
        raise RuntimeError(
            f"Error code: 401 - Incorrect API key provided: {canary}"
        )

    middleware = RequestContextMiddleware(boom_app)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.sleep(0)
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/crash",
        "headers": [(b"host", b"127.0.0.1")],
    }
    with caplog.at_level(logging.ERROR):
        asyncio.run(middleware(scope, receive, send))
    assert canary not in caplog.text, caplog.text
    assert "***" in caplog.text
    assert any(
        message.get("type") == "http.response.start"
        and message.get("status") == 500
        for message in sent
    ), sent
    print("✓ 500 兜底日志脱敏上游密钥")


def test_chat_stream_error_log_is_redacted(monkeypatch, caplog):
    """流式兜底日志不能把网关回显的 API Key 写进应用日志。"""
    import logging

    canary = "sk-CANARY1234567890abcdef"
    with caplog.at_level(logging.WARNING):
        message = _stream_error_message(
            monkeypatch,
            RuntimeError(
                "Error code: 401 - Incorrect API key provided: "
                f"{canary}. upstream"
            ),
        )
    assert canary not in message
    assert "服务器内部错误" in message
    assert canary not in caplog.text, caplog.text
    assert "***" in caplog.text
    print("✓ 流式兜底日志脱敏上游密钥")


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


def test_chat_stream_quarantine_and_case_conflict_messages(monkeypatch):
    """SSE 错误事件同样走持久化文案分层：隔离类保留恢复指引、
    大小写冲突给可执行提示且不回显已有账号。"""
    from utils.errors import MemoryPersistenceError

    quarantined = _stream_error_message(
        monkeypatch,
        MemoryPersistenceError(
            "记忆文件损坏并已隔离备份（data/memory/u1.json.corrupt-1）"
        ),
    )
    assert "隔离备份" in quarantined and "恢复" in quarantined, quarantined

    case_conflict = _stream_error_message(
        monkeypatch,
        MemoryPersistenceError(
            "用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同（已有 alice）"
        ),
    )
    assert "大小写" in case_conflict, case_conflict
    assert "存储暂时不可用" not in case_conflict
    assert "alice" not in case_conflict
    print("✓ SSE 隔离 / 大小写文案分层")


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
        def __init__(self, user_id, session_id, model, breaker, persist_guard=None):
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
    """PG 冷却期：删除类与行程新增/编辑必须 503，不能静默作用在 JSON 回退库上。"""
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
    # 错误文案统一为「…，请稍后再试」：异常细节只进日志，不回显内部信息
    assert "请稍后再试" in response.json()["detail"]
    # 删除失败：墓碑必须撤销，同一 id 仍可继续对话
    assert app.state.aligo.is_recently_deleted(user_id, "s-cool") is False

    response = client.delete(f"/api/sessions/s-cool/last-turn?user_id={user_id}")
    assert response.status_code == 503, response.text

    response = client.delete(f"/api/trips/trip_1?user_id={user_id}")
    assert response.status_code == 503, response.text

    # 行程列表与状态 / 导出同口径：冷却期不能静默展示回退 JSON
    response = client.get(f"/api/trips?user_id={user_id}")
    assert response.status_code == 503, response.text

    response = client.delete(f"/api/preferences/home_location?user_id={user_id}")
    assert response.status_code == 503, response.text

    # 新增 / 编辑行程同样不能静默写进回退 JSON：PG 恢复后读 PG，行程会
    # 「凭空消失」（与删除 / 导入 / CLI 的严格语义一致）
    response = client.post(
        "/api/trips",
        json={
            "user_id": user_id,
            "destination": "杭州",
            "start_date": "2026-12-01",
        },
    )
    assert response.status_code == 503, response.text
    response = client.put(
        "/api/trips/trip_1",
        json={"user_id": user_id, "purpose": "出差"},
    )
    assert response.status_code == 503, response.text

    # 偏好与会话元数据写入同样不能作用在回退 JSON 上（恢复后偏好消失、
    # 标题/置顶/归档被写到看不到的库）
    response = client.put(
        "/api/preferences/home_location",
        json={"user_id": user_id, "value": "北京"},
    )
    assert response.status_code == 503, response.text
    for path, payload in (
        ("/api/sessions/s-cool/title", {"user_id": user_id, "title": "标题"}),
        ("/api/sessions/s-cool/pin", {"user_id": user_id, "pinned": True}),
        ("/api/sessions/s-cool/archive", {"user_id": user_id, "archived": True}),
    ):
        response = client.put(path, json=payload)
        assert response.status_code == 503, (path, response.text)
    print("✓ PG 冷却期删除类接口统一 503 且不撤销墓碑；行程/偏好/会话元数据写入同样 503")
    # 导出同样不能把回退 JSON（空 / 陈旧）当成功备份写出——恢复时才发现
    # 备份里没有数据
    for path in (
        f"/api/export?user_id={user_id}",
        f"/api/sessions/export?user_id={user_id}&format=html",
        f"/api/sessions/s-cool/export?user_id={user_id}&format=md",
    ):
        response = client.get(path)
        assert response.status_code == 503, (path, response.text)
    print("✓ PG 冷却期删除类接口统一 503 且不撤销墓碑；行程新增/编辑与导出同样 503")


def test_export_sanitizes_surrogate_and_non_finite():
    """历史数据含孤立代理项 / NaN：各导出接口不 500、产物可解析。"""
    import json as json_module

    from webapp import server as server_module

    user_id = _unique_user("web_export_sanitize")
    memory_dir = Path(server_module.MEMORY_DIR)
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / f"{user_id}.json"
    data = {
        "user_id": user_id,
        "preferences": {},
        "chat_history": [
            {
                "role": "user",
                "content": "正常",
                "session_id": "s1",
                "timestamp": "2026-01-01T00:00:00",
            },
            {
                "role": "assistant",
                "content": "坏" + chr(0xD800) + "字符",
                "session_id": "s1",
                "timestamp": "2026-01-01T00:00:01",
            },
            {
                # 旧数据形态：assistant 内容是 JSON 文本，内部转义出真代理项；
                # 渲染时会 json.loads 还原，出口必须再次转义（否则会话详情 500）
                "role": "assistant",
                "content": (
                    '{"results":[{"agent_name":"A","status":"success",'
                    '"data":{"answer":"坏\\ud800答"}}],"status":"completed"}'
                ),
                "session_id": "s1",
                "timestamp": "2026-01-01T00:00:02",
            },
        ],
        "trip_history": [],
        "statistics": {"total_messages": 3, "avg": float("inf")},
    }
    path.write_text(json_module.dumps(data), encoding="utf-8")
    client = _client()
    try:
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        contents = [item["content"] for item in payload["chat_history"]]
        assert any("\\ud800" in content for content in contents), contents
        assert payload["statistics"]["avg"] is None

        # 会话详情 / 列表：渲染后的 content 与标题必须可 UTF-8 传输
        detail = client.get("/api/sessions/s1", params={"user_id": user_id})
        assert detail.status_code == 200, detail.text[:300]
        for message in detail.json()["messages"]:
            message["content"].encode("utf-8")
        listing = client.get("/api/sessions", params={"user_id": user_id})
        assert listing.status_code == 200, listing.text[:300]
        for item in listing.json()["sessions"]:
            str(item.get("title", "")).encode("utf-8")

        response = client.get(
            "/api/export", params={"user_id": user_id, "format": "md"}
        )
        assert response.status_code == 200, response.text[:300]

        response = client.get(
            "/api/sessions/export", params={"user_id": user_id, "format": "md"}
        )
        assert response.status_code == 200, response.text[:300]
        response = client.get(
            f"/api/sessions/s1/export?user_id={user_id}&format=html"
        )
        assert response.status_code == 200, response.text[:300]
        md_export = client.get(
            f"/api/sessions/s1/export?user_id={user_id}&format=md"
        )
        assert md_export.status_code == 200, md_export.text[:300]
        md_export.text.encode("utf-8")
    finally:
        # 旧格式数据会触发破坏性迁移：一并清理 .migrate-backup 副本
        _cleanup(user_id)
    print("✓ 导出清洗孤立代理项与 NaN（JSON / Markdown / 会话导出）")


def test_chat_message_cap_surfaces_cleanup_hint(monkeypatch):
    """聊天消息条数硬上限：普通端点回 409 + 可执行清理提示（不是
    「存储暂时不可用」），流式 error 事件同样保留该提示。"""
    from utils import user_import as module
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    hint = server_module._persistence_failure_detail(
        MemoryPersistenceError(
            "聊天消息总数已达上限 200000，请先清理历史消息"
        ),
        "rid",
    )
    assert "清理历史消息" in hint and "存储暂时不可用" not in hint, hint
    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    client = _client()
    user_id = _unique_user("web_chat_cap")
    try:
        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "s1"},
        )
        assert first.status_code == 200, first.text[:200]
        second = client.post(
            "/api/chat",
            json={"message": "再来", "user_id": user_id, "session_id": "s1"},
        )
        assert second.status_code == 409, second.text[:300]
        assert "清理历史消息" in second.json()["detail"]
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "流式", "user_id": user_id, "session_id": "s2"},
        ) as response:
            assert response.status_code == 200
            events = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        errors = [event for event in events if event.get("type") == "error"]
        assert errors and "清理历史消息" in errors[-1]["message"], events[-3:]
    finally:
        _cleanup(user_id)
    print("✓ 聊天消息上限回 409 并保留清理提示")


def test_import_reads_export_snapshot_once(monkeypatch):
    """预检与合并复核共用同一导出快照（dry-run 每次请求只读一次）；
    真实写入在导入互斥锁内再取一次快照做并发体积复核（两次并发导入
    各自锁外通过后合计越限会让账号永久无法导出）。"""
    from utils import user_import as module

    calls = {"n": 0}
    real = module._store_export_snapshot

    def counting(store):
        calls["n"] += 1
        return real(store)

    monkeypatch.setattr(module, "_store_export_snapshot", counting)
    client = _client()
    user_id = _unique_user("web_import_snapshot_once")
    try:
        response = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": 1},
            json={"backup": {"preferences": {"home_location": "北京"}}},
        )
        assert response.status_code == 200, response.text
        assert calls["n"] == 1, calls
        calls["n"] = 0
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"home_location": "北京"}}},
        )
        assert response.status_code == 200, response.text
        assert calls["n"] == 2, calls
    finally:
        _cleanup(user_id)
    print("✓ 导入预检共用快照，真实写入在导入锁内复核一次")


def test_import_lock_volume_rejection_maps_to_413(monkeypatch):
    """锁内体积复核拒绝映射 413：ImportVolumeError 是 ValueError 子类，
    except 顺序必须让子类分支先命中（此前 413 / 专用日志不可达）。

    非 dry_run 只在导入互斥锁内复核一次（锁外预检不再重复执行整库
    合并复核），因此第一次调用即返回超限、必须映射 413。
    """
    from utils import user_import as module

    calls = {"n": 0}

    def fake_merged(store, sections, **kwargs):
        calls["n"] += 1
        return "约 60.0 MB 超过导入上限 50 MB"

    monkeypatch.setattr(module, "merged_backup_limit_problem", fake_merged)
    client = _client()
    user_id = _unique_user("web_import_lock_volume")
    try:
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"home_location": "北京"}}},
        )
        assert response.status_code == 413, response.text
        detail = response.json()["detail"]
        assert "恢复中止" in detail and "请求 ID" in detail, detail
        assert calls["n"] == 1, calls
    finally:
        _cleanup(user_id)
    print("✓ 导入锁内体积拒绝返回 413（不是可重试 503 / 400）")


def test_export_refuses_backup_over_import_limits(monkeypatch):
    """超出导入上限的备份返回 413（不产出永远无法恢复的文件）。"""
    from utils import user_import as module

    # 写层硬上限同样生效：先按「一轮 = 两条消息」的上限写入，
    # 再收紧上限模拟存量账号已超过导出上限（写路径无法再越限）
    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    client = _client()
    user_id = _unique_user("web_export_oversize")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "s1"},
        )
        assert response.status_code == 200
        monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 413, response.text[:300]
        assert "导入上限" in response.json()["detail"]
    finally:
        _cleanup(user_id)
    print("✓ 超限备份返回 413")


def test_export_degrades_knowledge_over_limit_only(monkeypatch):
    """仅知识轴超限时省略知识段继续导出（提示头）；用户数据轴超限仍 413。

    知识库是全局存量，可被知识管理导入推到导出上限之外；不能让之后所有
    with_knowledge 导出永久 413，也不能借降级掩盖用户数据本身的超限。
    降级产物必须仍可被 /api/import 恢复用户数据。
    """
    from context.storage import create_long_term_memory
    from utils import user_import as module
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [
            {"id": 1, "content": "片段一", "metadata": {}},
            {"id": 2, "content": "片段二", "metadata": {}},
        ],
    )
    client = _client()
    user_id = _unique_user("web_export_omit_knowledge")
    target_id = _unique_user("web_export_omit_knowledge_restore")
    try:
        # 预置一份用户数据：降级备份必须仍能把它恢复到另一账号
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("预算怎么报", "按差旅标准报销", "sess-omit-kb")
        # 知识轴超限（上限收紧到 1 条），用户数据本身在限额内：降级导出成功
        monkeypatch.setattr(module, "_MAX_KNOWLEDGE", 1)
        response = client.get(
            "/api/export", params={"user_id": user_id, "with_knowledge": 1}
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert "knowledge" not in payload
        assert response.headers.get("x-knowledge-omitted") == "over-limit"

        restored = client.post(
            "/api/import",
            params={"user_id": target_id},
            json={"backup": payload},
        )
        assert restored.status_code == 200, restored.text[:300]
        detail = client.get(
            "/api/sessions/sess-omit-kb", params={"user_id": target_id}
        )
        assert detail.status_code == 200, detail.text[:300]
        texts = [message["content"] for message in detail.json()["messages"]]
        assert "预算怎么报" in texts and "按差旅标准报销" in texts, (
            "降级备份必须能恢复用户数据"
        )

        # 用户数据本身也超限：省略知识后仍然 413（不掩盖用户数据轴）
        monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
        response = client.get(
            "/api/export", params={"user_id": user_id, "with_knowledge": 1}
        )
        assert response.status_code == 413, response.text[:300]
        assert "导入上限" in response.json()["detail"]
    finally:
        _cleanup(user_id)
        _cleanup(target_id)
    print("✓ 仅知识轴超限时省略知识段导出（用户数据轴超限仍 413）")


def test_export_degrades_when_knowledge_planning_fails(monkeypatch):
    """分片规划失败（知识库忙）也按降级导出，头值给出 planning-failed。"""
    from utils import knowledge_admin
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [{"content": "长文档", "metadata": {}}],
    )

    def boom(*args, **kwargs):
        raise RuntimeError("kb busy")

    monkeypatch.setattr(knowledge_admin, "import_documents", boom)
    client = _client()
    user_id = _unique_user("web_export_planning_failed")
    try:
        response = client.get(
            "/api/export", params={"user_id": user_id, "with_knowledge": 1}
        )
        assert response.status_code == 200, response.text[:300]
        assert "knowledge" not in response.json()
        assert response.headers.get("x-knowledge-omitted") == "planning-failed"
    finally:
        _cleanup(user_id)
    print("✓ 知识分片规划失败时降级导出（planning-failed）")


def test_knowledge_export_sanitizes_non_finite(monkeypatch):
    """知识导出里的 NaN / Infinity 必须清洗，产物能被标准 JSON 回读。"""
    import json as json_module

    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [
            {
                "id": 1,
                "content": "住宿标准",
                "metadata": {"score": float("inf")},
            }
        ],
    )
    client = _client()
    response = client.get("/api/knowledge/export")
    assert response.status_code == 200, response.text[:300]

    def _reject(value):
        raise AssertionError(f"非标准 JSON 常量: {value}")

    payload = json_module.loads(response.text, parse_constant=_reject)
    assert payload["documents"][0]["metadata"]["score"] is None
    print("✓ 知识导出清洗非有限数（标准 JSON 可回读）")


def test_knowledge_export_rejects_over_panel_import_limit(monkeypatch):
    """整库序列化前的体积闸门：超 50MB 面板导入上限直接 413。"""
    from utils import user_import as user_import_module
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [{"id": 1, "content": "x" * 200, "metadata": {}}],
    )
    monkeypatch.setattr(user_import_module, "MAX_BACKUP_BYTES", 64)
    client = _client()
    response = client.get("/api/knowledge/export")
    assert response.status_code == 413, response.text[:300]
    assert "面板导入上限" in response.json()["detail"]
    print("✓ 知识导出超面板导入上限 413（不先序列化整库）")


def test_export_omits_oversized_knowledge_segment(monkeypatch):
    """with_knowledge 导出的知识段单独超限：直接省略并带响应头，避免先
    序列化整份超限备份。"""
    from utils import user_import as user_import_module
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [
            {"id": index, "content": "x" * 200, "metadata": {}}
            for index in range(10)
        ],
    )
    monkeypatch.setattr(user_import_module, "MAX_BACKUP_BYTES", 1024)
    client = _client()
    user_id = _unique_user("web_export_omit_kb")
    try:
        response = client.get(
            "/api/export",
            params={"user_id": user_id, "with_knowledge": 1},
        )
        assert response.status_code == 200, response.text[:300]
        assert response.headers.get("x-knowledge-omitted") == "over-limit"
        assert "knowledge" not in response.json()
    finally:
        _cleanup(user_id)
    print("✓ 知识段超限时导出省略并提示 over-limit")


def test_export_refuses_while_restore_in_progress():
    """恢复写入期间同账号导出返回 409：不会产出半截快照。"""
    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_export_during_restore")
    other_id = _unique_user("web_export_during_restore_other")
    state.begin_user_import(user_id)
    try:
        for path in (
            "/api/export",
            "/api/sessions/export",
            "/api/sessions/s1/export",
            "/api/trips/export",
            "/api/trips/calendar",
        ):
            response = client.get(path, params={"user_id": user_id})
            assert response.status_code == 409, (path, response.text)
            assert "正在恢复备份" in response.json()["detail"], (
                path,
                response.text,
            )
        # 别的账号不受影响
        response = client.get("/api/export", params={"user_id": other_id})
        assert response.status_code == 200, response.text
        # 被拒绝的导出不占用导出槽位（槽位没有泄漏）
        acquired: list[bool] = []
        try:
            acquired = [
                state.export_slots.acquire(blocking=False) for _ in range(2)
            ]
            assert all(acquired), "409 拒绝不应占用导出槽位"
        finally:
            for got_slot in acquired:
                if got_slot:
                    state.export_slots.release()
    finally:
        state.end_user_import(user_id)
        _cleanup(user_id)
        _cleanup(other_id)
    # 标记清除后可正常导出
    response = client.get("/api/export", params={"user_id": user_id})
    assert response.status_code == 200, response.text
    print("✓ 恢复进行中导出 409，结束后恢复可用")


def test_import_marks_user_while_restoring(monkeypatch):
    """真实恢复写入期间标记用户；dry-run 不标记；失败也清除标记。"""
    from utils import user_import as module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_import_marker")
    seen: dict[str, bool] = {}
    real_import = module.import_user_data
    real_preview = module.preview_user_import

    def wrapped_import(store, sections, **kwargs):
        seen["import"] = state.is_user_import_active(user_id)
        return real_import(store, sections, **kwargs)

    def wrapped_preview(store, sections, **kwargs):
        seen["preview"] = state.is_user_import_active(user_id)
        return real_preview(store, sections, **kwargs)

    monkeypatch.setattr(module, "import_user_data", wrapped_import)
    monkeypatch.setattr(module, "preview_user_import", wrapped_preview)
    try:
        response = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": 1},
            json={"backup": {"preferences": {"home_location": "北京"}}},
        )
        assert response.status_code == 200, response.text
        assert seen.get("preview") is False

        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"home_location": "北京"}}},
        )
        assert response.status_code == 200, response.text
        assert seen.get("import") is True
        assert state.is_user_import_active(user_id) is False

        def failing_import(store, sections, **kwargs):
            seen["failed"] = state.is_user_import_active(user_id)
            raise ValueError("注入的恢复失败")

        monkeypatch.setattr(module, "import_user_data", failing_import)
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"home_location": "上海"}}},
        )
        assert response.status_code == 400, response.text
        assert seen.get("failed") is True
        assert state.is_user_import_active(user_id) is False
    finally:
        _cleanup(user_id)
    print("✓ 恢复写入期间标记用户，dry-run 不标记，失败清除标记")


def test_export_rejects_snapshot_when_restore_starts_mid_read(monkeypatch):
    """入口检查通过后、读取期间才开始恢复：读完再确认并丢弃快照（409）。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_export_restore_race")
    real_load = server_module._load_user_data

    def load_with_restore_start(uid, strict=False):
        data = real_load(uid, strict=strict)
        state.begin_user_import(uid)
        return data

    monkeypatch.setattr(
        server_module, "_load_user_data", load_with_restore_start
    )
    try:
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 409, response.text
        assert "正在恢复备份" in response.json()["detail"]
    finally:
        state.end_user_import(user_id)
        _cleanup(user_id)
    print("✓ 读取期间开始恢复：导出丢弃快照并返回 409")


def test_export_detects_restore_completed_during_read(monkeypatch):
    """恢复在读取期间开始并结束（标记已清）：纪元变化同样让导出作废。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_export_restore_epoch")
    real_load = server_module._load_user_data

    def load_with_full_restore(uid, strict=False):
        data = real_load(uid, strict=strict)
        # 模拟一次完整恢复在读取期间发生：结束标记已清，但纪元已推进
        state.begin_user_import(uid)
        state.end_user_import(uid)
        return data

    monkeypatch.setattr(
        server_module, "_load_user_data", load_with_full_restore
    )
    try:
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "发生过备份恢复" in detail, detail
        # 恢复已结束：立即重试即可，无需 Retry-After
        assert "Retry-After" not in response.headers
    finally:
        _cleanup(user_id)
    print("✓ 读取期间开始并结束的恢复：纪元变化让导出作废")


def test_export_endpoints_detect_restore_started_mid_read(monkeypatch):
    """会话 / 行程导出与 with_knowledge 的知识读取：读取中途才开始恢复
    同样让本次导出作废（覆盖每个端点的读完复核，而不只是入口检查）。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_export_mid_read")
    real_open = server_module._open_long_term

    class _RestoreOnReadStore:
        """第一次读会话 / 行程后开始一次恢复，模拟读取中途才开始写入。"""

        def __init__(self, store):
            self._store = store

        def __getattr__(self, name):
            return getattr(self._store, name)

        def _start_restore(self):
            state.begin_user_import(user_id)

        def get_chat_history(self, *args, **kwargs):
            data = self._store.get_chat_history(*args, **kwargs)
            self._start_restore()
            return data

        def get_trip_history(self, *args, **kwargs):
            data = self._store.get_trip_history(*args, **kwargs)
            self._start_restore()
            return data

    def open_with_restore(uid, *args, **kwargs):
        return _RestoreOnReadStore(real_open(uid, *args, **kwargs))

    monkeypatch.setattr(server_module, "_open_long_term", open_with_restore)
    try:
        for path in (
            "/api/sessions/export",
            "/api/sessions/s1/export",
            "/api/trips/export",
            "/api/trips/calendar",
        ):
            response = client.get(path, params={"user_id": user_id})
            assert response.status_code == 409, (path, response.text)
            assert "正在恢复备份" in response.json()["detail"], (
                path,
                response.text,
            )
            # 清掉标记：下一轮只能靠端点内的读完复核命中（入口不拦）
            state.end_user_import(user_id)

        # with_knowledge：用户数据读完后、知识片段读取期间才开始恢复
        def start_restore_then_documents():
            state.begin_user_import(user_id)
            return []

        monkeypatch.setattr(
            server_module, "export_documents", start_restore_then_documents
        )
        response = client.get(
            "/api/export", params={"user_id": user_id, "with_knowledge": 1}
        )
        assert response.status_code == 409, response.text
        assert "正在恢复备份" in response.json()["detail"]
    finally:
        state.end_user_import(user_id)
        _cleanup(user_id)
    print("✓ 各导出端点读完复核：读取中途开始的恢复返回 409")


def test_export_filenames_disambiguate_non_ascii_ids():
    """中文 ID 清洗后追加短哈希；单会话导出文件名带用户维度。"""
    import re as re_module

    from webapp.server import _safe_filename

    first = _safe_filename("张三")
    second = _safe_filename("李四")
    assert first != second, (first, second)
    assert re_module.fullmatch(r"_+_[0-9a-f]{6}", first), first
    assert _safe_filename("alice") == "alice"
    # 与 CLI safe_export_user 同一策略：大小写变体（大小写不敏感文件系统
    # 上撞同一目录项）与「清洗后形如 <名>_<6 hex>」的重叠空间都要消歧
    assert _safe_filename("Alice") != _safe_filename("alice"), (
        _safe_filename("Alice"),
        _safe_filename("alice"),
    )
    assert _safe_filename("张三") != _safe_filename("___ced07f"), (
        _safe_filename("张三"),
        _safe_filename("___ced07f"),
    )
    long_id = "a" * 40
    cleaned = _safe_filename(long_id)
    assert len(cleaned) == 24 + 1 + 6 and cleaned.startswith("a" * 24), cleaned

    client = _client()
    user_id = _unique_user("张三")
    try:
        chat = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "s1"},
        )
        assert chat.status_code == 200

        response = client.get("/api/export", params={"user_id": user_id})
        disposition = response.headers["content-disposition"]
        assert re_module.search(r"_[0-9a-f]{6}\.json", disposition), disposition

        response = client.get(
            f"/api/sessions/s1/export?user_id={user_id}&format=md"
        )
        assert response.status_code == 200, response.text[:200]
        disposition = response.headers["content-disposition"]
        assert re_module.search(r"aligo_.+_[0-9a-f]{6}_s1\.md", disposition), (
            disposition
        )
    finally:
        _cleanup(user_id)
    print("✓ 导出文件名去撞名（短哈希 + 用户维度）")


def test_knowledge_status_masks_path_and_rate_limits(monkeypatch):
    """/api/knowledge 不返回本机路径；挂起写锁期间快速返回「正在写入」。"""
    import tempfile as tempfile_module
    import threading
    from pathlib import Path as pathlib_path

    import utils.knowledge_admin as admin_module
    import utils.knowledge_stats as stats_module

    client = _client()
    data = client.get("/api/knowledge").json()
    assert "path" not in data, data
    assert data.get("available") in (True, False)

    # 用临时 KB 目录 + 假客户端让状态查询必然走到读锁分支：写锁繁忙
    # 断言在 CI / 干净环境（本机库未初始化）也同样执行，不再提前 return
    with tempfile_module.TemporaryDirectory() as tmp:
        db = pathlib_path(tmp) / "milvus_lite.db"
        db.mkdir()

        class _FakeClient:
            def has_collection(self, name):
                return True

            def get_collection_stats(self, name):
                return {"row_count": 0}

        monkeypatch.setattr(stats_module, "default_db_path", lambda: db)
        monkeypatch.setattr(
            stats_module, "get_milvus_client", lambda path: _FakeClient()
        )
        holder = threading.Thread(
            target=lambda: admin_module._WRITE_LOCK.acquire()
        )
        holder.start()
        holder.join()
        try:
            busy = client.get("/api/knowledge").json()
        finally:
            admin_module._WRITE_LOCK.release()
        assert busy.get("available") is False, busy
        assert "正在写入" in busy.get("reason", ""), busy
    print("✓ /api/knowledge 脱敏且写锁繁忙时快速返回")


def test_doctor_detail_redacts_paths_and_tokens():
    """/api/doctor 详情脱敏：项目绝对路径 / 其它本机路径 / URL 凭据与查询串不回显。"""
    from webapp import server as server_module

    detail = (
        f"技能目录 {server_module.PROJECT_ROOT}/.claude/skills；"
        r"依赖 C:\venv\Lib\site-packages\torch\__init__.py 与 "
        "/usr/lib/python3.12/site-packages/x.py；"
        "网关 https://user:secret@llm.example/v1?api-key=SECRET"
    )
    cleaned = server_module._redact_doctor_detail(detail)
    assert "user:secret" not in cleaned
    assert "api-key" not in cleaned and "SECRET" not in cleaned
    assert str(server_module.PROJECT_ROOT) not in cleaned
    assert "<项目>" in cleaned and "llm.example" in cleaned
    assert "venv" not in cleaned and "torch" not in cleaned
    assert "/usr/lib" not in cleaned
    assert "<路径>" in cleaned
    print("✓ doctor 详情脱敏（路径 / 凭据 / 查询串）")
def test_side_effect_get_rejects_cross_site():
    """带副作用 GET（doctor / search / eval / weather）拒绝浏览器跨站请求。"""
    client = _client()
    headers = {"Sec-Fetch-Site": "cross-site"}
    for path in (
        "/api/doctor",
        "/api/knowledge/search?q=test",
        "/api/knowledge/eval",
        "/api/weather?city=北京",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, (path, response.text[:200])
        assert response.json()["detail"] == "跨站请求被拒绝", path
    # 非浏览器客户端（脚本 / CLI）不带 Sec-Fetch-Site：不受影响
    assert client.get("/api/metrics").status_code == 200
    print("✓ 带副作用 GET 拒绝跨站请求")


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


def test_delete_session_clears_execution_checkpoints(monkeypatch):
    """DELETE 会话同时清理执行检查点（内含子智能体结果），不等 TTL。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_ckpt_clear")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("你好", "在", "s-ckpt")
        called: list[tuple[str, str]] = []
        monkeypatch.setattr(
            server_module,
            "clear_session_checkpoints",
            lambda u, s: called.append((u, s)) or 0,
        )
        response = client.delete(
            "/api/sessions/s-ckpt", params={"user_id": user_id}
        )
        assert response.status_code == 200, response.text
        assert called == [(user_id, "s-ckpt")]
    finally:
        _cleanup(user_id)
    print("✓ 删除会话清理执行检查点")


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


def test_session_export_sanitizes_custom_title():
    """自定义标题含换行 / 尖括号：导出时折叠空白并转义，不能伪造结构。"""
    client = _client()
    user_id = _unique_user("web_test_export_title")
    try:
        assert (
            client.post(
                "/api/chat",
                json={"message": "你好", "user_id": user_id, "session_id": "sess-title"},
            ).status_code
            == 200
        )
        title = "第一行\n## 用户\n伪造内容 <script>alert(1)</script>"
        renamed = client.put(
            "/api/sessions/sess-title/title",
            json={"user_id": user_id, "title": title},
        )
        assert renamed.status_code == 200, renamed.text
        export = client.get(
            "/api/sessions/sess-title/export", params={"user_id": user_id}
        )
        assert export.status_code == 200
        lines = export.text.splitlines()
        assert lines[0].startswith("# 第一行 ## 用户 伪造内容 &lt;script&gt;"), lines[0]
        assert "<script>" not in export.text, "标题里的尖括号必须转义"
        assert len([line for line in lines if line == "## 用户"]) == 1, (
            "标题不能再造出一个正文段落"
        )
        print("✓ 单会话导出清洗自定义标题（折叠换行 / 转义尖括号）")
    finally:
        _cleanup(user_id)


def test_import_rejects_when_existing_totals_would_exceed_limits(monkeypatch):
    """存量 + 备份越过导出上限：返回可修正的 409，而不是通用 503/400。"""
    from webapp import server as server_module

    class _FullStore:
        def get_statistics(self):
            return {"total_messages": 200000, "total_trips": 0}

        def get_trip_history(self, limit=None):
            return []

        def get_chat_history(self, limit=None):
            return []

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _FullStore(),
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_cap")},
        json={
            "backup": {
                "chat_history": [
                    {"role": "user", "content": "hi", "session_id": "s1"}
                ]
            }
        },
    )
    assert response.status_code == 409, response.text[:300]
    assert "上限" in response.json()["detail"]

def test_import_accepts_utf8_bom_backup():
    """带 UTF-8 BOM 的备份与 CLI utf-8-sig 一致，不能被 422 拒绝。"""
    client = _client()
    user_id = _unique_user("web_test_import_bom")
    try:
        body = b"\xef\xbb\xbf" + json.dumps(
            {"backup": {"preferences": {"home_location": "北京"}}},
            ensure_ascii=False,
        ).encode("utf-8")
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text[:300]
        assert response.json()["preferences"] == 1
    finally:
        _cleanup(user_id)


def test_import_rejects_oversized_request_body(monkeypatch):
    """请求体超过 64MB 中间件上限时提前 413。

    端点不再按「50MB + 4KB」预检请求体字节数：备份上限按导出对象衡量，
    以 Unicode 转义发送的请求体可达对象体积的近 2 倍（见下一个用例），
    请求体只受 64MB 硬上限约束。
    """
    from webapp import server as server_module

    monkeypatch.setitem(server_module._BODY_LIMIT_OVERRIDES, "/api/import", 64)
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_big")},
        content=b"x" * 4096,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413, response.text[:300]


def test_import_accepts_escaped_body_over_backup_limit(monkeypatch):
    """Unicode 转义放大的请求体不按备份字节上限拒绝（对象本身达标即可）。

    requests 默认 ensure_ascii=True：CJK 内容在请求体里按转义序列展开，
    体积近乎翻倍。按请求体字节预检会把 27 MB 的合法备份（57 MB 请求体）
    误判为超过 50 MB。
    """
    from utils import user_import as user_import_module

    backup = {"preferences": {"home_location": "北" * 2000}}
    escaped = json.dumps({"backup": backup}, ensure_ascii=True).encode("utf-8")
    raw = json.dumps({"backup": backup}, ensure_ascii=False).encode("utf-8")
    limit = 10_000
    assert len(raw) < limit < len(escaped), (len(raw), len(escaped))
    monkeypatch.setattr(user_import_module, "MAX_BACKUP_BYTES", limit)
    client = _client()
    user = _unique_user("web_test_import_escape")
    try:
        response = client.post(
            "/api/import",
            params={"user_id": user, "dry_run": True},
            content=escaped,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text[:300]
        assert response.json()["preferences"] == 1
    finally:
        _cleanup(user)
    print("✓ 转义放大的请求体不再被字节上限误拒")


def test_import_rejects_backup_over_export_size(monkeypatch):
    """请求体紧凑体积达标、按导出格式（indent=2）却超限的备份必须拒绝。"""
    from utils import user_import as user_import_module

    monkeypatch.setattr(
        user_import_module,
        "backup_limit_problem",
        lambda backup, **kwargs: "消息 1 条超过导入上限（模拟导出体积超限）",
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_export_cap")},
        json={"backup": {"preferences": {"home_location": "北京"}}},
    )
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    # 知识轴超限现在只是降级导出，文案统一提示先清理再加导，不再断言
    # 「永久无法导出」
    assert "导入上限" in detail and "请先清理部分数据" in detail, detail
    print("✓ 恢复前按导出口径复核体积上限")


def test_markdown_export_strips_terminal_controls():
    """Web 用户数据 Markdown 导出剥离 ESC / OSC / BEL（CLI 同口径）。"""
    from webapp import server as server_module

    cleaned = server_module._md_inline(
        "a" + chr(27) + "]52;c;x" + chr(7) + "b"
    )
    assert chr(27) not in cleaned and chr(7) not in cleaned
    assert "&lt;script&gt;" in server_module._md_inline("<script>")
    # 链接 / 图片语法必须被打破：每个 ] 都带反斜杠转义，](url) 不成形
    link = server_module._md_inline("[点我](https://phish.example)")
    assert link.count("]") == link.count("\\]")
    assert "\\](https" in link
    # 用户可控值里的 ``\]`` 必须先转义反斜杠：否则会拼成 ``\\]``，CommonMark
    # 解析为「转义反斜杠 + 未转义右括号」，链接重新闭合
    tricky = server_module._md_inline("[点我\\](https://phish.example)")
    closing = tricky.index("](https")
    assert tricky[closing - 3 : closing] == "\\\\\\", tricky
    print("✓ Markdown 导出剥离终端控制序列")


def test_user_data_markdown_tolerates_non_string_fields():
    """历史脏数据（非字符串日期 / archived_sessions 含 dict）不能让 MD 导出 500。"""
    from webapp import server as server_module

    data = {
        "preferences": {},
        "trip_history": [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": 20260101,
                "end_date": None,
                "purpose": 1,
            }
        ],
        "chat_history": [],
        "statistics": {},
        "archived_sessions": [{"bad": "shape"}, "s1"],
    }
    text = server_module._user_data_markdown("u1", data)
    assert "20260101" in text, text
    print("✓ 用户数据 Markdown 导出容忍非字符串字段")


def test_session_title_helper_matches_old_expression_and_scans_prefix():
    """标题裁剪与旧表达式逐字符一致；大输入只扫描标题前缀。"""
    import time

    from webapp import server as server_module

    cases = [
        "  你好   世界  ",
        "a" * 100,
        ("词 " * 5000).strip(),
        "a " * 5000,
        "\n\t ",
        "",
    ]
    for content in cases:
        expected = " ".join(str(content).split())[:40]
        assert (
            server_module._session_title_from_content(content) == expected
        ), (content[:20], expected)

    huge = ("a " * 2_000_000).strip()
    started = time.monotonic()
    title = server_module._session_title_from_content(huge)
    elapsed = time.monotonic() - started
    assert title == " ".join(huge.split())[:40]
    assert elapsed < 0.5, f"大消息标题裁剪耗时 {elapsed:.2f}s"
    assert server_module._id_set([{"bad": "shape"}, [1], "s1", 2]) == {"s1", "2"}
    print("✓ 会话标题裁剪语义一致且只扫描前缀")


def test_session_detail_tolerates_dirty_archived_sessions():
    """archived_sessions 含 dict/list 脏条目时，会话仍可列出与打开。"""
    client = _client()
    user_id = _unique_user("web_test_dirty_archive")
    session_id = "sess-dirty-archive"
    try:
        from context.storage import create_long_term_memory

        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("你好", "你好呀", session_id)
        path = Path(MEMORY_DIR) / f"{user_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["archived_sessions"] = [{"bad": "shape"}, [1, 2], session_id]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        listing = client.get("/api/sessions", params={"user_id": user_id})
        assert listing.status_code == 200, listing.text
        detail = client.get(
            f"/api/sessions/{session_id}", params={"user_id": user_id}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["messages"], detail.text
        print("✓ archived_sessions 脏条目不影响会话列表 / 详情")
    finally:
        _cleanup(user_id)


def test_import_skips_reserved_session_id():
    """备份里的 session_id="export" 不能落库（否则详情路由永远被静态路由遮蔽）。"""
    client = _client()
    user_id = _unique_user("web_test_import_reserved")
    try:
        backup = {
            "chat_history": [
                {"role": "user", "content": "保留字会话", "session_id": "export"},
                {"role": "assistant", "content": "回答", "session_id": "export"},
                {"role": "user", "content": "正常会话", "session_id": "sess-ok"},
            ],
            "session_titles": {"export": "坏标题", "sess-ok": "好标题"},
            "pinned_sessions": ["export"],
            "trip_history": [
                # 只有行程、没有消息的保留字会话：关联也要清掉，否则面板
                # 里出现一条永远打不开的行程会话链接
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2027-03-01",
                    "session_id": "export",
                }
            ],
        }
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert any("保留字" in item for item in body.get("skipped") or []), body
        sessions = client.get("/api/sessions", params={"user_id": user_id}).json()[
            "sessions"
        ]
        ids = [item["session_id"] for item in sessions]
        assert "sess-ok" in ids and "export" not in ids, ids
        trips = client.get("/api/trips", params={"user_id": user_id}).json()["trips"]
        assert trips and trips[0].get("session_id") in (None, ""), trips
        detail = client.get(
            "/api/sessions/sess-ok", params={"user_id": user_id}
        )
        assert detail.status_code == 200, detail.text
        print("✓ 备份导入跳过保留字 session_id（含标题 / 置顶 / 行程关联）")
    finally:
        _cleanup(user_id)


def test_chat_stream_persists_client_turn_id():
    """客户端回合标识写进助手消息 _meta（停止后前端精确对账用）。"""
    client = _client()
    user_id = _unique_user("web_test_turn_id")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "你好",
                "user_id": user_id,
                "session_id": "sess-turn",
                "client_turn_id": "turn-abc123",
            },
        ) as response:
            assert response.status_code == 200
            for _ in response.iter_lines():
                pass
        detail = client.get(
            "/api/sessions/sess-turn", params={"user_id": user_id}
        ).json()
        assistants = [
            message for message in detail["messages"] if message["role"] == "assistant"
        ]
        assert assistants and assistants[-1]["meta"]["client_turn_id"] == "turn-abc123"

        bad = client.post(
            "/api/chat",
            json={
                "message": "你好",
                "user_id": user_id,
                "client_turn_id": "含 空格",
            },
        )
        assert bad.status_code == 422, "非法回合标识应被拒绝"
        print("✓ 流式对话把 client_turn_id 写入助手消息元数据")
    finally:
        _cleanup(user_id)


def test_trips_export_respects_status_filter():
    """导出（CSV / HTML）与列表同口径应用状态过滤。"""
    import csv
    import io
    from datetime import date, timedelta

    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_trips_status_export")
    today = date.today()
    past = (today - timedelta(days=30)).isoformat()
    active_start = (today - timedelta(days=1)).isoformat()
    future = (today + timedelta(days=30)).isoformat()
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {"origin": "北京", "destination": "上海", "start_date": past, "end_date": past}
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "广州",
                    "start_date": active_start,
                    "end_date": future,
                }
            )
            store.save_trip_history(
                {"origin": "北京", "destination": "成都", "start_date": future}
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        client = _client()
        active = client.get(
            "/api/trips/export", params={"user_id": user_id, "status": "active"}
        )
        rows = list(csv.reader(io.StringIO(active.content.decode("utf-8-sig"))))
        assert len(rows) == 2 and rows[1][1] == "广州", rows

        past_html = client.get(
            "/api/trips/export",
            params={"user_id": user_id, "status": "past", "format": "html"},
        )
        assert "上海" in past_html.text
        assert "广州" not in past_html.text and "成都" not in past_html.text

        past_ics = client.get(
            "/api/trips/export",
            params={"user_id": user_id, "status": "past", "format": "ics"},
        )
        assert past_ics.headers["content-type"].startswith("text/calendar")
        assert past_ics.text.count("BEGIN:VEVENT") == 1
        assert "上海" in past_ics.text and "广州" not in past_ics.text

        bad = client.get(
            "/api/trips/export", params={"user_id": user_id, "status": "bogus"}
        )
        assert bad.status_code == 422
        print("✓ 行程导出（CSV / HTML / ICS）应用状态过滤")
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


def test_preference_put_strips_type_and_rejects_blank():
    """PUT 规范化类型：首尾空格写入规范键，纯空白 422。

    此前 ` home_location` / `home_location ` 会落库成与规范键并存的影子
    偏好（精确键查找读不到，却计入类型上限）。
    """
    client = _client()
    user_id = _unique_user("web_test_pref_strip")
    try:
        padded = client.put(
            "/api/preferences/%20home_location",
            json={"user_id": user_id, "value": "上海"},
        )
        assert padded.status_code == 200, padded.text
        assert padded.json()["type"] == "home_location", padded.json()
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {"home_location": "上海"}, status["preferences"]
        blank = client.put(
            "/api/preferences/%20%20",
            json={"user_id": user_id, "value": "x"},
        )
        assert blank.status_code == 422, blank.text
        trail = client.put(
            "/api/preferences/home_location%20",
            json={"user_id": user_id, "value": "北京"},
        )
        assert trail.status_code == 200, trail.text
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {"home_location": "北京"}, status["preferences"]
        print("✓ PUT 规范化偏好类型并拒绝纯空白")
    finally:
        _cleanup(user_id)


def test_preference_put_edits_shadow_key_clears_original():
    """编辑影子键（历史备份 / 旧 API 写入的 " home_location"）时清掉原键。

    否则规范键拿到新值、被编辑行仍回显旧值，同一偏好并存两份并继续
    占用类型上限名额。
    """
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_pref_shadow")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_preference(" home_location", "旧影子值")
            store.save_preference("home_location", "北京")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.put(
            "/api/preferences/%20home_location",
            json={"user_id": user_id, "value": "上海"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["type"] == "home_location", response.json()
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {"home_location": "上海"}, status["preferences"]

        # 写规范键（不是编辑影子行）同样清理其它入口留下的影子行
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_preference(" home_location", "旧影子值")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        canonical = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "杭州"},
        )
        assert canonical.status_code == 200, canonical.text
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {"home_location": "杭州"}, status["preferences"]
        print("✓ 编辑影子偏好时清掉原键（不并存两份）")
    finally:
        _cleanup(user_id)


def test_preference_put_rejects_dot_types():
    """PUT 拒绝 "." / ".."：JSON 后端下次加载会丢弃这类键（200 后偏好消失）。"""
    client = _client()
    user_id = _unique_user("web_test_pref_dot")
    try:
        for raw in ("%2E", "%2E%2E"):
            response = client.put(
                f"/api/preferences/{raw}",
                json={"user_id": user_id, "value": "x"},
            )
            assert response.status_code == 422, (raw, response.text)
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert status["preferences"] == {}, status["preferences"]
        print("✓ PUT 拒绝 . / .. 偏好类型")
    finally:
        _cleanup(user_id)


def test_visible_preferences_hides_unmanageable_keys():
    """面板可见口径隐藏不可寻址的键（含 /、控制字符、点号、超长）。"""
    from webapp.server import _visible_preferences

    visible = _visible_preferences(
        {
            "home_location": "上海",
            "a/b": "x",
            "bad" + chr(0x0A) + "key": "y",
            ".": "z",
            "": "w",
            "k" * 65: "v",
        }
    )
    assert visible == {"home_location": "上海"}, visible
    # 列表内的空容器与 null / 空白项同口径（下一步追加时会被合并层丢弃）
    lists = _visible_preferences(
        {
            "hotel_brands": ["汉庭", None, "", {}, []],
            "empty_only": [None, {}, []],
        }
    )
    assert lists == {"hotel_brands": ["汉庭"]}, lists
    print("✓ 面板隐藏不可管理的偏好键与空值条目")


def test_preference_put_cleans_hidden_blank_entries():
    """写入偏好时顺带清理面板不可见的空值条目（释放类型上限名额）。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_pref_heal")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_preference("ghost_type", None)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "上海"},
        )
        assert response.status_code == 200, response.text
        status = client.get("/api/status", params={"user_id": user_id}).json()
        assert "ghost_type" not in status["preferences"]
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert "ghost_type" not in store.get_preference(), (
                "空值偏好应被清理而不是仅隐藏"
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        print("✓ 写入偏好时清理空值条目")
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


def test_session_search_handles_dotted_capital_i():
    """搜索含 İ 的关键字：助手消息 / 标题命中的会话都不能丢，片段不错位。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_search_i")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        # A：用户提问不含关键字，仅助手消息命中
        store.add_chat_message("user", "帮我规划下周出差", "s-assistant")
        store.add_chat_message("assistant", "推荐顺路去 İstanbul 看看", "s-assistant")
        # B：首条用户消息命中（默认标题同样来自它）
        store.add_chat_message("user", "İstanbul 天气怎么样", "s-user")
        store.add_chat_message("assistant", "当地多云", "s-user")
        # C：仅自定义标题命中，消息本身不含关键字
        store.add_chat_message("user", "随便聊聊", "s-title")
        assert store.set_session_title("s-title", "İstanbul 计划")
        close = getattr(store, "close", None)
        if callable(close):
            close()
        client = _client()
        data = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "İstanbul"}
        ).json()
        sessions = {item["session_id"]: item for item in data["sessions"]}
        assert set(sessions) == {"s-assistant", "s-user", "s-title"}, sessions
        assert "İstanbul" in sessions["s-assistant"]["snippet"], "助手消息命中要有片段"
        assert sessions["s-assistant"]["match_index"] == 1, "命中在助手消息上"
        assert "İstanbul" in sessions["s-user"]["snippet"]
        assert sessions["s-user"]["match_index"] == 0

        # 小写查询同样命中三个会话（大小写不敏感不能因修复而收窄）
        lower = client.get(
            "/api/sessions", params={"user_id": user_id, "q": "istanbul"}
        ).json()
        lower_sessions = {item["session_id"]: item for item in lower["sessions"]}
        assert set(lower_sessions) == {"s-assistant", "s-user", "s-title"}, lower_sessions
        assert "İstanbul" in lower_sessions["s-assistant"]["snippet"]
        print("✓ 会话搜索兼容 İ 的大小写与标题命中")
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


def test_trips_import_api(monkeypatch):
    """POST /api/trips/import：CSV / ICS 文本导入、dry-run、错误映射与 JSON body 要求。"""
    from webapp import server as server_module

    class _Store:
        def __init__(self, existing=None):
            self.existing = list(existing or [])
            self.saved = None
            self.closed = False

        def get_trip_history(self, limit=None):
            return list(self.existing)

        def save_trip_histories(self, rows, **kwargs):
            self.saved = list(rows)
            return (len(rows), 0)

        def close(self):
            self.closed = True

    client = _client()
    user = _unique_user("web_trip_import")
    csv_text = (
        "出发地,目的地,出发日期,返程日期,目的\n"
        "北京,上海,2026-12-01,2026-12-03,出差\n"
    )
    store = _Store()
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: store)
    url = f"/api/trips/import?user_id={user}"
    response = client.post(url, json={"content": csv_text})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["format"] == "csv"
    assert data["dry_run"] is False
    assert (data["rows"], data["created"], data["updated"]) == (1, 1, 0)
    assert store.saved and store.saved[0]["destination"] == "上海"
    assert store.closed

    # dry-run：按已存在行程模拟更新，不写库
    store = _Store(
        [
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-03",
                "purpose": "出差",
            }
        ]
    )
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: store)
    preview = client.post(f"{url}&dry_run=1", json={"content": csv_text})
    assert preview.status_code == 200
    preview_data = preview.json()
    assert preview_data["dry_run"] is True
    assert (preview_data["created"], preview_data["updated"]) == (0, 1)
    assert store.saved is None, "dry-run 不能写库"

    # ICS 自动识别
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            "UID:web-import-api",
            "SUMMARY:北京 → 广州（出差）",
            "DTSTART:20261210T090000",
            "DTEND:20261211T180000",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    store = _Store()
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: store)
    ics_response = client.post(url, json={"content": ics})
    assert ics_response.status_code == 200
    assert ics_response.json()["format"] == "ics"
    assert store.saved and store.saved[0]["destination"] == "广州"

    # 没有可导入项：400 带具体原因
    store = _Store()
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: store)
    empty = client.post(url, json={"content": "不是行程"})
    assert empty.status_code == 400
    assert "没有可导入的行程" in empty.json()["detail"]

    # 写入失败：503
    class _Boom(_Store):
        def save_trip_histories(self, rows, **kwargs):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, **kwargs: _Boom()
    )
    failed = client.post(url, json={"content": csv_text})
    assert failed.status_code == 503
    assert "导入行程失败" in failed.json()["detail"]

    # 表单 POST 不能触发导入（必须是 JSON body）
    form = client.post(url, data={"content": csv_text})
    assert form.status_code == 422

    assert 'id="tripImport"' in client.get("/").text, "面板必须提供「导入行程」入口"
    print("✓ /api/trips/import 文本导入（CSV / dry-run / ICS / 400 / 503 / 422）")


def test_trips_import_runs_in_import_executor_and_maps_errors(monkeypatch):
    """行程导入在专用执行器执行；ValueError 400 / 内容上限 422 / 背压 503。"""
    import threading

    from webapp import server as server_module

    class _Store:
        def close(self):
            pass

    names = []

    def fake_import(store, text, dry_run=False, force_ics=False, user_tz=None):
        names.append(threading.current_thread().name)
        if text == "row-cap":
            raise ValueError("单次最多导入 5000 条行程，请拆分文件后再导入")
        return {
            "dry_run": dry_run,
            "format": "csv",
            "rows": 1,
            "created": 1,
            "updated": 0,
            "skipped": [],
            "skipped_total": 0,
        }

    monkeypatch.setattr(server_module, "import_trips_from_text", fake_import)
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: _Store())
    client = _client()
    state = client.app.state.aligo
    user = _unique_user("web_trip_exec")

    response = client.post(
        f"/api/trips/import?user_id={user}", json={"content": "x"}
    )
    assert response.status_code == 200, response.text
    assert names and names[0].startswith("aligo-import"), names

    bad = client.post(
        f"/api/trips/import?user_id={user}", json={"content": "row-cap"}
    )
    assert bad.status_code == 400
    assert "5000" in bad.json()["detail"]

    over = client.post(
        f"/api/trips/import?user_id={user}",
        json={"content": "a" * 500_001},
    )
    assert over.status_code == 422, "超过 50 万字符必须在校验层拒绝"

    tokens = [
        state.import_queue_slots.acquire(),
        state.import_queue_slots.acquire(),
    ]
    assert all(tokens)
    try:
        busy = client.post(
            f"/api/trips/import?user_id={user}", json={"content": "x"}
        )
        assert busy.status_code == 503
        assert "已有导入任务" in busy.json()["detail"]
    finally:
        for _ in tokens:
            state.import_queue_slots.release()
    assert len(names) == 2, "背压 / 422 不应执行导入"
    print("✓ /api/trips/import 专用执行器 / 400 / 422 / 背压 503")


def test_trips_import_rate_limited(monkeypatch):
    """/api/trips/import 计入导入限流桶：超额直接 429，不进入导入执行器。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    class _Store:
        def close(self):
            pass

    calls = []

    def fake_import(store, text, dry_run=False, force_ics=False, user_tz=None):
        calls.append(dry_run)
        return {
            "dry_run": dry_run,
            "format": "csv",
            "rows": 1,
            "created": 1,
            "updated": 0,
            "skipped": [],
            "skipped_total": 0,
        }

    monkeypatch.setattr(server_module, "import_trips_from_text", fake_import)
    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, **kwargs: _Store())
    client = _client()
    state = client.app.state.aligo
    state.import_limiter = RateLimiter(limit=1, window_sec=60)
    user = _unique_user("web_trip_rl")
    url = f"/api/trips/import?user_id={user}"

    allowed = client.post(url, json={"content": "x"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.headers.get("x-ratelimit-limit") == "1"
    assert allowed.headers.get("x-ratelimit-remaining") == "0"

    rejected = client.post(url, json={"content": "x"})
    assert rejected.status_code == 429
    assert rejected.headers.get("retry-after")
    assert rejected.headers.get("x-ratelimit-remaining") == "0"
    assert calls == [False], "被限流的请求不应进入导入逻辑"
    print("✓ /api/trips/import 导入限流 429")


def test_trips_import_maps_strict_store_failure(monkeypatch):
    """配置存储不可用（strict）时导入返回 503 存储暂时不可用，而不是被包装。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    def boom(*args, **kwargs):
        raise MemoryPersistenceError("postgres down")

    monkeypatch.setattr(server_module, "create_long_term_memory", boom)
    client = _client()
    user = _unique_user("web_trip_strict")
    response = client.post(
        f"/api/trips/import?user_id={user}", json={"content": "x"}
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == "存储暂时不可用，请稍后再试", detail
    print("✓ /api/trips/import 存储 strict 失败映射 503")


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


def test_trip_update_legacy_unpadded_dates(monkeypatch):
    """存量未补零出发日期：仅改目的时冲突照常计算、补填返程不被误拒。"""
    from webapp import server as server_module

    class _Store:
        def __init__(self):
            self.trips = [
                {
                    "trip_id": "trip_legacy",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-1-5",
                    "purpose": "出差",
                },
                {
                    "trip_id": "trip_2",
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-06",
                },
            ]
            self.updates = []

        def get_trip_history(self, limit=None):
            return list(self.trips)

        def update_trip(self, trip_id, fields):
            self.updates.append((trip_id, fields))
            return True

        def close(self):
            pass

    store = _Store()
    monkeypatch.setattr(
        server_module, "_open_long_term", lambda uid, strict=False: store
    )
    client = _client()
    updated = client.put(
        "/api/trips/trip_legacy",
        json={"user_id": "u-legacy", "purpose": "会议"},
    )
    assert updated.status_code == 200, updated.text
    assert [item["trip_id"] for item in updated.json()["conflicts"]] == [
        "trip_2"
    ], updated.json()
    filled = client.put(
        "/api/trips/trip_legacy",
        json={"user_id": "u-legacy", "end_date": "2026-01-07"},
    )
    assert filled.status_code == 200, filled.text
    print("✓ 存量未补零日期的 Web 更新与冲突判断")


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


def test_post_last_turn_accepts_long_expect():
    """POST body 形式的 expect 不受 6000 字查询上限：面板编辑 / 重新生成
    能处理 CLI / 备份导入产生的超长提问。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_post_long")
    long_text = "长" * 7000
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", long_text, "s-post-long")
            store.add_chat_message("assistant", "收到", "s-post-long")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        # 查询串形式超 6000 被 FastAPI 拒绝（面板已改用 POST body）
        too_long = client.delete(
            "/api/sessions/s-post-long/last-turn",
            params={"user_id": user_id, "expect": long_text},
        )
        assert too_long.status_code == 422

        mismatch = client.post(
            "/api/sessions/s-post-long/last-turn",
            json={"user_id": user_id, "expect": "不匹配的提问"},
        )
        assert mismatch.status_code == 200
        assert mismatch.json()["removed"] == 0
        assert mismatch.json()["mismatch"] is True

        response = client.post(
            "/api/sessions/s-post-long/last-turn",
            json={"user_id": user_id, "expect": long_text},
        )
        assert response.status_code == 200, response.text
        assert response.json()["removed"] == 2
        assert response.json()["mismatch"] is False
        print("✓ POST last-turn 支持超长 expect")
    finally:
        _cleanup(user_id)


def test_trips_api_status_filter():
    """/api/trips?status= 与面板状态徽标同口径；非法值 422。"""
    client = _client()
    user_id = _unique_user("web_test_trips_status")
    try:
        for destination, start in (
            ("past", "2000-01-01"),
            ("future", "2099-01-01"),
        ):
            created = client.post(
                "/api/trips",
                json={
                    "user_id": user_id,
                    "destination": destination,
                    "start_date": start,
                },
            )
            assert created.status_code == 201, created.text
        past = client.get(
            "/api/trips", params={"user_id": user_id, "status": "past"}
        ).json()
        assert past["status"] == "已结束"
        assert [trip["destination"] for trip in past["trips"]] == ["past"]
        upcoming = client.get(
            "/api/trips", params={"user_id": user_id, "status": "upcoming"}
        ).json()
        assert upcoming["status"] == "即将出行"
        assert [trip["destination"] for trip in upcoming["trips"]] == ["future"]
        bad = client.get(
            "/api/trips", params={"user_id": user_id, "status": "someday"}
        )
        assert bad.status_code == 422
        print("✓ /api/trips status 过滤")
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

        # format=html：与 CLI sessions-export --html 同一渲染口径
        html_export = client.get(
            "/api/sessions/export", params={"user_id": user_id, "format": "html"}
        )
        assert html_export.status_code == 200
        disposition = html_export.headers.get("content-disposition", "")
        assert "attachment" in disposition and ".html" in disposition
        assert html_export.text.startswith("<!doctype html>")
        assert "<title>Aligo 会话导出</title>" in html_export.text
        assert "杭州行程" in html_export.text
        assert "&amp;lt;" not in html_export.text, "标题只应转义一次"
        bad_format = client.get(
            "/api/sessions/export", params={"user_id": user_id, "format": "pdf"}
        )
        assert bad_format.status_code == 422

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
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
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
        lock = state.locks.get(user_id)
        assert lock is None or lock.locked() is False, "流结束后必须释放用户锁"
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
        long_value = client.put(
            "/api/preferences/home_location",
            json={"user_id": user_id, "value": "x" * 501},
        )
        assert long_value.status_code == 200, (
            "存量 / 导入的长值必须能原样保存（与 /api/status 回显一致）"
        )
        # 面板未修改直接保存的对象 / 数值列表必须能原样写回，否则这些
        # 存量偏好永远无法保存（此前对象一律 422）
        obj = client.put(
            "/api/preferences/profile",
            json={"user_id": user_id, "value": {"seat": "window", "tier": 2}},
        )
        assert obj.status_code == 200, obj.text
        assert obj.json()["value"] == {"seat": "window", "tier": 2}
        numeric = client.put(
            "/api/preferences/scores",
            json={"user_id": user_id, "value": [1, 2, 3]},
        )
        assert numeric.status_code == 200, numeric.text
        assert numeric.json()["value"] == [1, 2, 3]
        oversize = client.put(
            "/api/preferences/oversize",
            json={"user_id": user_id, "value": "x" * 11000},
        )
        assert oversize.status_code == 422
        nested_nan = client.put(
            "/api/preferences/nested_nan",
            content='{"user_id": "' + user_id + '", "value": {"a": NaN}}',
            headers={"Content-Type": "application/json"},
        )
        assert nested_nan.status_code == 422
        print("✓ 偏好新增 / 覆盖 / 删除 / 参数校验")
    finally:
        _cleanup(user_id)


def test_preference_writes_invalidate_all_caches(monkeypatch):
    """面板 PUT / DELETE 偏好与备份导入后清空所有会话的偏好快照缓存。"""
    from webapp import server as server_module

    calls: list[int] = []
    monkeypatch.setattr(
        server_module,
        "_invalidate_preference_caches",
        lambda: calls.append(1),
    )
    client = _client()
    user_id = _unique_user("web_test_pref_cache")
    try:
        response = client.put(
            "/api/preferences/hotel_brands",
            json={"user_id": user_id, "value": ["汉庭"]},
        )
        assert response.status_code == 200, response.text
        response = client.delete(
            "/api/preferences/hotel_brands", params={"user_id": user_id}
        )
        assert response.status_code == 200, response.text
        assert len(calls) >= 2, "PUT / DELETE 后都应清空会话偏好缓存"
        # 备份导入直写同样要清空：不经过各会话的 MemoryManager
        before_import = len(calls)
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"home_location": "上海"}}},
        )
        assert response.status_code == 200, response.text
        assert len(calls) == before_import + 1, (
            "备份导入成功后必须清空会话偏好缓存"
        )
    finally:
        _cleanup(user_id)
    print("✓ 面板偏好写入 / 备份导入清空会话缓存")


def test_preference_put_rejects_blank_values():
    """与 CLI pref-set / 面板口径一致：空值 PUT 返回 422（清空用 DELETE）。"""
    client = _client()
    user_id = _unique_user("web_test_blank_pref")
    try:
        for value in ("", "   ", [], ["  "], [None]):
            response = client.put(
                "/api/preferences/home_location",
                json={"user_id": user_id, "value": value},
            )
            assert response.status_code == 422, (value, response.status_code)
        # 0 / False 是合法偏好，仍可写入
        assert (
            client.put(
                "/api/preferences/budget_level",
                json={"user_id": user_id, "value": 0},
            ).status_code
            == 200
        )
        print("✓ 偏好 PUT 拒绝空值（0 / False 保留）")
    finally:
        _cleanup(user_id)


def test_status_and_export_hide_blank_preference_items():
    """恢复备份带入的 [null] / 空白串不再出现在 /api/status 与 Markdown 导出。"""
    client = _client()
    user_id = _unique_user("web_test_pref_null")
    try:
        backup = {
            "preferences": {
                "hotel_brands": ["东航", None],
                "home_location": "   ",
                "airlines": [""],
            }
        }
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        preferences = client.get(
            "/api/status", params={"user_id": user_id}
        ).json()["preferences"]
        assert preferences == {"hotel_brands": ["东航"]}, preferences
        markdown = client.get(
            "/api/export", params={"user_id": user_id, "format": "md"}
        ).text
        assert "东航" in markdown
        assert "None" not in markdown
        assert "、null" not in markdown
        print("✓ 面板 / 导出隐藏空白与 null 偏好项")
    finally:
        _cleanup(user_id)


def test_import_rejects_non_finite_usage():
    """备份 _meta.usage 含 NaN / Infinity：消息被跳过，/api/status 不会永久 500。"""
    client = _client()
    user_id = _unique_user("web_test_nan_usage")
    try:
        backup = {
            "chat_history": [
                {"role": "user", "content": "正常提问", "session_id": "s-nan"},
                {
                    "role": "assistant",
                    "content": '{"reply": "x", "_meta": {"usage": {"calls": 1, "input_tokens": NaN}}}',
                    "session_id": "s-nan",
                },
                {
                    "role": "assistant",
                    "content": '{"reply": "y", "_meta": {"usage": {"output_tokens": Infinity}}}',
                    "session_id": "s-nan",
                },
            ]
        }
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        status = client.get("/api/status", params={"user_id": user_id})
        assert status.status_code == 200, status.text
        assert status.json()["usage"]["calls"] == 0
        print("✓ 备份 NaN 用量被拒绝，状态接口保持可用")
    finally:
        _cleanup(user_id)


def test_status_survives_hostile_turn_meta():
    """坏 _meta（NaN total_tokens / agents 非列表 / 超大数）不能让状态与导出 500。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_bad_meta")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "hi", "s-meta")
            store.add_chat_message(
                "assistant",
                '{"reply": "x", "_meta": {"usage": {"calls": 1, "total_tokens": NaN}, "agents": 5}}',
                "s-meta",
            )
            store.add_chat_message(
                "assistant",
                '{"reply": "y", "_meta": {"usage": {"calls": 1, "total_tokens": Infinity}, "agents": true, "elapsed": 10}}',
                "s-meta",
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        status = client.get("/api/status", params={"user_id": user_id})
        assert status.status_code == 200, status.text
        usage = status.json()["usage"]
        assert usage["calls"] == 2 and usage["total_tokens"] == 0, usage
        markdown = client.get(
            "/api/export", params={"user_id": user_id, "format": "md"}
        )
        assert markdown.status_code == 200, markdown.text
        print("✓ 坏 _meta 不再让状态 / Markdown 导出 500")
    finally:
        _cleanup(user_id)


def test_import_rejects_non_list_agents():
    """备份 _meta.agents 非列表：该消息被跳过并在错误中说明。"""
    client = _client()
    user_id = _unique_user("web_test_bad_agents")
    try:
        backup = {
            "chat_history": [
                {"role": "user", "content": "hi", "session_id": "s-ag"},
                {
                    "role": "assistant",
                    "content": '{"reply": "x", "_meta": {"agents": 5}}',
                    "session_id": "s-ag",
                },
            ]
        }
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        errors = response.json().get("skipped") or []
        assert any("智能体列表" in error for error in errors), response.json()
        status = client.get("/api/status", params={"user_id": user_id})
        assert status.status_code == 200
        print("✓ 备份非列表 agents 被拒绝且状态保持可用")
    finally:
        _cleanup(user_id)


def test_import_parse_exception_maps_400(monkeypatch):
    """备份解析抛非预期异常（如越界年份）时返回 400 而不是 500。"""
    from utils import user_import as import_module

    def boom(_payload):
        raise OverflowError("year out of range")

    monkeypatch.setattr(import_module, "parse_user_export_data", boom)
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_parse_boom")},
        json={"backup": {"chat_history": []}},
    )
    assert response.status_code == 400, response.text
    assert "解析失败" in response.json()["detail"]
    print("✓ 备份解析异常映射 400")


def test_rate_limit_info_reports_import_bucket_degraded():
    """任一限流桶回退时 /api/health 的 rate_limit_degraded 必须为 True。"""
    import types

    from webapp import server as server_module

    state = types.SimpleNamespace(
        rate_limiter=types.SimpleNamespace(
            backend="redis", limit=1, degraded=False, window_sec=60
        ),
        ip_limiter=types.SimpleNamespace(limit=2, degraded=False),
        kb_limiter=types.SimpleNamespace(limit=3, degraded=False),
        weather_limiter=types.SimpleNamespace(limit=4, degraded=False),
        import_limiter=types.SimpleNamespace(limit=5, degraded=True),
        read_limiter=types.SimpleNamespace(limit=6, degraded=False),
        write_limiter=types.SimpleNamespace(limit=7, degraded=False),
    )
    info = server_module._rate_limit_info(state)
    assert info["degraded"] is True
    assert info["kb_import_limit"] == 5
    assert info["read_limit"] == 6, "读接口桶的额度也要在健康信息里可见"
    assert info["write_limit"] == 7, "写接口桶的额度也要在健康信息里可见"

    # 单独读接口桶回退同样计入汇总（此前 read_limiter 不在统计里）
    state.import_limiter = types.SimpleNamespace(limit=5, degraded=False)
    state.read_limiter = types.SimpleNamespace(limit=6, degraded=True)
    info = server_module._rate_limit_info(state)
    assert info["degraded"] is True

    # 只有写桶降级（Redis 故障只被写接口触发）也必须计入
    state.read_limiter = types.SimpleNamespace(limit=6, degraded=False)
    state.write_limiter = types.SimpleNamespace(limit=7, degraded=True)
    info = server_module._rate_limit_info(state)
    assert info["degraded"] is True, "写桶单独降级不能被健康汇总漏掉"
    print("✓ 导入 / 读 / 写接口限流桶降级计入健康汇总")


def test_rate_limit_info_reports_corrupt_keys_without_degrading():
    """键级损坏（WRONGTYPE）单独暴露，不并入桶级 degraded。"""
    import types

    from webapp import server as server_module

    def _bucket(limit, corrupt=()):
        return types.SimpleNamespace(
            backend="redis",
            limit=limit,
            degraded=False,
            window_sec=60,
            corrupt_keys=corrupt,
        )

    state = types.SimpleNamespace(
        rate_limiter=_bucket(1, ("aligo:ratelimit:user:k1",)),
        ip_limiter=_bucket(2),
        kb_limiter=_bucket(3),
        weather_limiter=_bucket(4),
        import_limiter=_bucket(5),
        read_limiter=_bucket(6),
        write_limiter=_bucket(7),
    )
    info = server_module._rate_limit_info(state)
    assert info["degraded"] is False, "键级损坏不并入桶级 degraded"
    assert info["corrupt_keys"] == 1
    assert info["corrupt_key_samples"] == ["aligo:ratelimit:user:k1"]

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    data = TestClient(app).get("/api/health").json()
    assert data.get("rate_limit_corrupt_keys") == 0
    print("✓ 键级损坏单独暴露（不并入桶级 degraded）")


def test_chat_maps_circuit_open_and_storage_errors(monkeypatch):
    """/api/chat 的 CircuitOpenError / MemoryPersistenceError 都映射 503。"""
    from utils.circuit_breaker import CircuitOpenError
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_chat_errors")

    async def open_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise CircuitOpenError()

    monkeypatch.setattr(server_module.ConversationRuntime, "ask", open_ask)
    response = client.post(
        "/api/chat", json={"message": "你好", "user_id": user_id}
    )
    assert response.status_code == 503
    assert "服务暂时不可用" in response.json()["detail"]

    async def broken_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise MemoryPersistenceError("disk full")

    monkeypatch.setattr(server_module.ConversationRuntime, "ask", broken_ask)
    response = client.post(
        "/api/chat", json={"message": "你好", "user_id": user_id}
    )
    assert response.status_code == 503
    assert "存储暂时不可用" in response.json()["detail"]
    print("✓ /api/chat 熔断 / 存储错误映射 503")


def test_chat_quarantine_error_detail_mentions_recovery(monkeypatch):
    """隔离类持久化失败的 503 文案保留「隔离备份 / 恢复」提示。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_chat_quarantine")

    async def broken_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise MemoryPersistenceError(
            "记忆文件损坏并已隔离备份（data/memory/u1.json.corrupt-1）"
        )

    monkeypatch.setattr(server_module.ConversationRuntime, "ask", broken_ask)
    response = client.post(
        "/api/chat", json={"message": "你好", "user_id": user_id}
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "隔离备份" in detail and "恢复" in detail, detail
    print("✓ /api/chat 隔离错误 503 文案保留恢复提示")


def test_import_strict_open_and_failure_mappings(monkeypatch):
    """/api/import 以 strict=True 打开存储；失败分别映射两条 503 文案。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_import_fail")
    open_flags = []
    original_open = server_module._open_long_term

    def failing_open(target_user, strict=False, **kwargs):
        open_flags.append((strict, kwargs.get("allow_quarantined")))
        raise MemoryPersistenceError("disk full")

    monkeypatch.setattr(server_module, "_open_long_term", failing_open)
    response = client.post(
        "/api/import",
        params={"user_id": user_id},
        json={"backup": {"preferences": {"home_location": "北京"}}},
    )
    assert response.status_code == 503
    assert "存储暂时不可用" in response.json()["detail"]
    assert open_flags == [(True, True)], (
        "恢复必须 strict 且 allow_quarantined 打开"
    )

    def boom_open(target_user, strict=False, allow_quarantined=False):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(server_module, "_open_long_term", boom_open)
    response = client.post(
        "/api/import",
        params={"user_id": user_id},
        json={"backup": {"preferences": {"home_location": "北京"}}},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "恢复失败，请稍后重试"

    # 真实的 _open_long_term(strict=True) 把 MemoryPersistenceError 转成
    # HTTPException(503)；这条 503 必须原样上抛，不能被包装成「恢复失败」
    monkeypatch.setattr(server_module, "_open_long_term", original_open)

    def failing_factory(*args, **kwargs):
        raise MemoryPersistenceError("postgres down")

    monkeypatch.setattr(server_module, "create_long_term_memory", failing_factory)
    strict_failed = client.post(
        "/api/import",
        params={"user_id": user_id},
        json={"backup": {"preferences": {"home_location": "北京"}}},
    )
    assert strict_failed.status_code == 503
    assert strict_failed.json()["detail"] == "存储暂时不可用，请稍后再试"
    print("✓ /api/import strict 打开与失败映射")


def test_import_write_quarantine_maps_503_detail(monkeypatch):
    """恢复写库中途进入隔离态（save_preference 抛隔离错误）时，503 文案
    必须保留隔离备份提示，而不是通用「存储暂时不可用」。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    class _QuarantineStore:
        def get_preference(self):
            return {}

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def save_preference(self, key, value):
            raise MemoryPersistenceError(
                "记忆主文件缺失且存在损坏隔离备份（data/memory/u.json.corrupt-1）"
            )

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _QuarantineStore(),
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_quarantine")},
        json={"backup": {"preferences": {"home_location": "北京"}}},
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "隔离备份" in detail and "恢复" in detail, detail
    print("✓ /api/import 写库隔离错误 503 保留恢复提示")


def test_import_dry_run_read_failure_maps_503_sanitized(monkeypatch):
    """dry-run 预演读取既有数据失败：503 脱敏文案，不能是 400 或回显
    底层存储错误（主机 / 端口 / 用户）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    class _BrokenReadStore:
        def get_preference(self):
            return {}

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def get_trip_history(self, limit=None):
            raise MemoryPersistenceError(
                'PostgreSQL 操作失败：connection to server at "db.internal", '
                'port 5432 failed: password authentication failed for user "aligo"'
            )

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _BrokenReadStore(),
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={
            "user_id": _unique_user("web_test_import_dry_read"),
            "dry_run": 1,
        },
        json={
            "backup": {
                "trip_history": [
                    {
                        "origin": "上海",
                        "destination": "成都",
                        "start_date": "2026-05-01",
                    }
                ]
            }
        },
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "存储暂时不可用" in detail, detail
    assert "db.internal" not in detail and "5432" not in detail, detail
    print("✓ dry-run 预演读失败映射 503 且脱敏")


def test_persistence_detail_transient_read_is_generic():
    """瞬时读锁失败（OSError 分流文案）不能报「数据损坏」。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    detail = server_module._persistence_failure_detail(
        MemoryPersistenceError(
            "长期记忆暂时不可读（文件可能被占用）（data/memory/u.json）："
            "[Errno 13] Permission denied"
        ),
        "rid-transient",
    )
    assert "损坏" not in detail, detail
    assert "存储暂时不可用" in detail, detail
    print("✓ 瞬时读失败按通用 503 文案，不误报数据损坏")


def test_open_long_term_no_backup_sentinel_message(monkeypatch):
    """标记损坏且无可用备份：503 文案给「未能隔离备份」而不是引导
    用户恢复不存在的备份。"""
    from webapp import server as server_module

    class _DummyStore:
        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _DummyStore(),
    )
    monkeypatch.setattr(
        server_module,
        "_quarantine_marker_for",
        lambda user_id, store: server_module._QUARANTINE_NO_BACKUP_MARKER,
    )
    with pytest.raises(server_module.HTTPException) as info:
        server_module._open_long_term("u-no-backup")
    detail = info.value.detail
    assert "未能隔离备份" in detail, detail
    assert "备份文件恢复" not in detail, detail

    mapped = server_module._persistence_failure_detail(
        server_module.MemoryPersistenceError(
            "记忆文件损坏且未找到可用隔离备份，请稍后重试或联系维护"
        ),
        "rid-nb",
    )
    assert "未能隔离备份" in mapped, mapped
    print("✓ 无可用隔离备份走「未能隔离备份」文案")


def test_import_limit_check_uses_parsed_sections(monkeypatch):
    """Web 导入自检与 CLI 同口径：按解析后的可恢复段复核，解析会丢弃的
    非法行不再让同一备份在 Web 413、CLI 可恢复。"""
    from utils import user_import as module

    monkeypatch.setattr(module, "_MAX_MESSAGES", 2)
    client = _client()
    user_id = _unique_user("web_test_import_parsed")
    backup = {
        "backup": {
            "chat_history": [
                {"role": "user", "content": "q1", "session_id": "s1"},
                {"role": "assistant", "content": "a1", "session_id": "s1"},
                {"role": "system", "content": "非法角色行", "session_id": "s1"},
            ]
        }
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json=backup
        )
        assert response.status_code == 200, response.text[:300]
        assert response.json()["messages_added"] == 2
    finally:
        _cleanup(user_id)
    print("✓ 导入自检按解析后段复核（与 CLI 同口径）")


def test_import_partial_error_reports_counts(monkeypatch):
    """/api/import 的部分提交（PartialImportError）如实报告已提交条数。"""
    from utils.errors import PartialImportError
    from webapp import server as server_module

    class _PartialStore:
        def get_preference(self):
            return {}

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def get_trip_history(self, limit=None):
            return []

        def save_trip_histories(self, trips, **kwargs):
            raise PartialImportError(
                "批量保存在第 2 条后失败：connection lost", 1, 1
            )

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _PartialStore(),
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_partial")},
        json={
            "backup": {
                "trip_history": [
                    {
                        "origin": "上海",
                        "destination": "成都",
                        "start_date": "2026-05-01",
                    }
                ]
            }
        },
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "已提交 1 条新增、1 条更新" in detail, detail
    assert "可安全重跑" in detail
    print("✓ /api/import 部分提交如实报告条数")


def test_trips_import_partial_error_reports_counts(monkeypatch):
    """/api/trips/import 的部分提交（PartialImportError）如实报告条数。"""
    from utils.errors import PartialImportError
    from webapp import server as server_module

    class _PartialStore:
        def get_trip_history(self, limit=None):
            return []

        def save_trip_histories(self, trips, **kwargs):
            raise PartialImportError(
                "批量保存在第 2 条后失败：db down", 1, 0
            )

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _PartialStore(),
    )
    client = _client()
    response = client.post(
        "/api/trips/import",
        params={"user_id": _unique_user("web_test_trips_partial")},
        json={"content": "出发地,目的地,出发日期\n上海,成都,2026-05-01\n"},
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "已提交 1 条新增、0 条更新" in detail, detail
    assert "可安全重跑" in detail
    print("✓ /api/trips/import 部分提交如实报告条数")


def test_trips_import_capacity_maps_conflict(monkeypatch):
    """/api/trips/import 的容量越限与实时新增同口径：409 而不是 400。"""
    from utils.errors import AccountCapacityError
    from webapp import server as server_module

    class _FullStore:
        def get_trip_history(self, limit=None):
            return []

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _FullStore(),
    )

    def over_cap(store, content, dry_run=False, user_tz=None):
        raise AccountCapacityError(
            "导入后行程总数将超过上限 5000（现有 5000 + 新增 1），"
            "请先清理历史行程后重试"
        )

    monkeypatch.setattr(server_module, "import_trips_from_text", over_cap)
    client = _client()
    response = client.post(
        "/api/trips/import",
        params={"user_id": _unique_user("web_test_trips_cap")},
        json={"content": "出发地,目的地,出发日期\n上海,成都,2026-05-01\n"},
    )
    assert response.status_code == 409, response.text[:300]
    assert "上限" in response.json()["detail"]
    print("✓ /api/trips/import 容量越限映射 409")


def test_web_session_pending_and_no_backup_messages(monkeypatch, tmp_path):
    """聊天会话构造守卫的 pending / 无备份文案（此前 0 覆盖）：不得
    引导用户恢复不存在的备份。"""
    from config import STORAGE_CONFIG
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)

    class _FakeLongTerm:
        def __init__(self, pending=False, active=None):
            self.db_path = str(tmp_path / "u-ws.json")
            self._pending = pending
            self._active = active

        def get_recovered_corrupt_backup(self):
            return None

        def has_pending_corrupt_quarantine(self):
            return self._pending

        def get_active_quarantine_backup(self):
            return self._active

    class _FakeMemory:
        def __init__(self, long_term):
            self.long_term = long_term

    class _FakeRuntime:
        def __init__(self, long_term):
            self.memory = _FakeMemory(long_term)

        def close(self):
            pass

    def make_session(long_term):
        monkeypatch.setattr(
            server_module,
            "ConversationRuntime",
            lambda **kwargs: _FakeRuntime(long_term),
        )
        return server_module._WebSession("u-ws", "s1", None, None)

    with pytest.raises(MemoryPersistenceError) as info:
        make_session(_FakeLongTerm(pending=True))
    assert "尚未完成" in str(info.value)
    assert "备份文件恢复" not in str(info.value)

    with pytest.raises(MemoryPersistenceError) as info:
        make_session(
            _FakeLongTerm(active=server_module._QUARANTINE_NO_BACKUP_MARKER)
        )
    assert "未找到可用隔离备份" in str(info.value)
    assert "备份文件恢复" not in str(info.value)
    print("✓ 会话构造的 pending / 无备份文案")


def test_import_checks_limit_after_dropping_sessions(monkeypatch):
    """50MB 自检必须在剔除墓碑 / 保留字会话之后（与 CLI 同序）：
    被剔除消息的大体积不能让同一备份在 Web 413。"""
    from utils import user_import as module

    def fake_limit(payload, **kwargs):
        contents = [
            message.get("content")
            for message in payload.get("chat_history") or []
            if isinstance(message, dict)
        ]
        if any(
            isinstance(content, str) and len(content) > 1000
            for content in contents
        ):
            return "约 60 MB 超过导入上限 50 MB"
        return None

    monkeypatch.setattr(module, "backup_limit_problem", fake_limit)
    client = _client()
    user_id = _unique_user("web_test_import_tomb_limit")
    state = client.app.state.aligo
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-dead",
    )
    backup = {
        "backup": {
            "chat_history": [
                {
                    "role": "user",
                    "content": "x" * 4000,
                    "session_id": "s-dead",
                },
                {"role": "user", "content": "keep", "session_id": "s-ok"},
            ],
            "session_titles": {"s-dead": "旧标题", "s-ok": "新标题"},
        }
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json=backup
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["messages_added"] == 1
        assert any("最近被删除" in item for item in payload["skipped"])
    finally:
        _cleanup(user_id)
    print("✓ 导入自检在剔除会话之后执行（与 CLI 同序）")


def test_import_queue_dropped_sessions_not_rolled_back(monkeypatch):
    """排队期间被剔除的会话不进入回滚候选：不得出现「已回滚 0 条」
    噪音或多余的 delete_chat_session 调用。"""
    from webapp import server as server_module

    deleted_calls = []

    class _Store:
        def get_preference(self):
            return {}

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def get_trip_history(self, limit=None):
            return []

        def ensure_persisted(self):
            pass

        def delete_chat_session(self, session_id):
            deleted_calls.append(session_id)
            return 0

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _Store(),
    )
    client = _client()
    user_id = _unique_user("web_test_import_queue_drop")
    state = client.app.state.aligo
    answers = {"s-queue": [False, True]}

    def is_recently_deleted(uid, sid):
        if uid != user_id:
            return False
        seq = answers.get(sid)
        if not seq:
            return sid == "s-queue"
        return seq.pop(0)

    monkeypatch.setattr(state, "is_recently_deleted", is_recently_deleted)
    backup = {
        "backup": {
            "chat_history": [
                {"role": "user", "content": "队列期删除", "session_id": "s-queue"}
            ]
        }
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json=backup
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["messages_added"] == 0
        assert any("排队期间" in item for item in payload["skipped"]), payload
        assert not any("已回滚" in item for item in payload["skipped"]), payload
        assert deleted_calls == [], deleted_calls
    finally:
        _cleanup(user_id)
    print("✓ 队列期剔除的会话不误回滚")


def test_import_quarantined_no_user_data_reports_incomplete(monkeypatch):
    """隔离账号 + 备份只有保留字会话的消息：ok=false 且带
    recovery_incomplete，不调用 ensure_persisted（保留隔离指引）。"""
    from webapp import server as server_module

    ensured = []

    class _QStore:
        def get_preference(self):
            return {}

        def export_snapshot(self):
            return {
                "chat_history": [],
                "trip_history": [],
                "preferences": {},
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def get_trip_history(self, limit=None):
            return []

        def get_fresh_quarantine_backup(self):
            return "data/memory/u.json.corrupt-1"

        def ensure_persisted(self):
            ensured.append(True)

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda user_id, strict=False, **kwargs: _QStore(),
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_incomplete")},
        json={
            "backup": {
                "chat_history": [
                    {"role": "user", "content": "x", "session_id": "export"}
                ]
            }
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False, payload
    assert payload.get("recovery_incomplete") is True, payload
    assert ensured == [], "未写回用户数据时不能重建主文件"
    print("✓ 隔离账号空恢复 Web 报告未完成")


def test_create_trip_write_failure_maps_503(monkeypatch):
    """POST /api/trips 写入失败映射 503（与更新 / 删除一致）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            return []

        def save_trip_history(self, trip, **kwargs):
            raise MemoryPersistenceError("disk full")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
    )
    client = _client()
    response = client.post(
        "/api/trips",
        json={
            "user_id": _unique_user("web_test_trip_fail"),
            "destination": "上海",
            "start_date": "2026-12-01",
        },
    )
    assert response.status_code == 503
    assert "添加行程失败" in response.json()["detail"]
    print("✓ POST /api/trips 写入失败 503")


def test_knowledge_update_error_mappings(monkeypatch):
    """PUT /api/knowledge/documents 的 400 / 503 映射。"""
    from webapp import server as server_module

    client = _client()
    # 请求省略 metadata 时会先读取现有记录合并（与 CLI kb-update 一致）；
    # 这里注入存在的片段，错误映射测试不依赖本地知识库文件
    monkeypatch.setattr(
        server_module,
        "get_document",
        lambda doc_id: {
            "id": doc_id,
            "content": "旧内容",
            "metadata": {"category": "交通"},
        },
    )
    merged = {}

    def raise_value(doc_id, content, metadata=None, **kwargs):
        merged["metadata"] = metadata
        raise ValueError("旧数据没有 group")

    monkeypatch.setattr(server_module, "update_document", raise_value)
    response = client.put(
        "/api/knowledge/documents/1", json={"content": "新内容"}
    )
    assert response.status_code == 400
    assert "旧数据没有 group" in response.json()["detail"]
    assert merged["metadata"] == {"category": "交通"}, "省略 metadata 应沿用库中现有值"

    def raise_missing(doc_id, content, metadata=None, **kwargs):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "update_document", raise_missing)
    response = client.put(
        "/api/knowledge/documents/1", json={"content": "新内容"}
    )
    assert response.status_code == 503
    assert "init_knowledge_base.py" in response.json()["detail"]

    def raise_boom(doc_id, content, metadata=None, **kwargs):
        raise RuntimeError("embed down")

    monkeypatch.setattr(server_module, "update_document", raise_boom)
    response = client.put(
        "/api/knowledge/documents/1", json={"content": "新内容"}
    )
    assert response.status_code == 503
    assert "更新失败" in response.json()["detail"]
    print("✓ PUT 知识片段 400 / 503 映射")


def test_knowledge_update_without_metadata_sets_expect_baseline(monkeypatch):
    """PUT 省略 metadata 时以读到的快照作为并发基线，防止旧值覆盖并发修改。"""
    from webapp import server as server_module

    client = _client()
    monkeypatch.setattr(
        server_module,
        "get_document",
        lambda doc_id: {
            "id": doc_id,
            "content": "旧内容",
            "metadata": {"category": "交通", "title": "市内交通"},
        },
    )
    captured = {}

    def fake_update(doc_id, content, metadata=None, **kwargs):
        captured["metadata"] = metadata
        captured["expect_metadata"] = kwargs.get("expect_metadata")
        return {"id": doc_id}

    monkeypatch.setattr(server_module, "update_document", fake_update)
    response = client.put(
        "/api/knowledge/documents/1", json={"content": "新内容"}
    )
    assert response.status_code == 200
    assert captured["metadata"] == {"category": "交通", "title": "市内交通"}
    assert captured["expect_metadata"] == {
        "category": "交通",
        "title": "市内交通",
    }
    print("✓ 省略 metadata 的 PUT 带上并发基线")


def test_knowledge_update_accepts_legacy_long_expect_metadata(monkeypatch):
    """更新 / 乐观并发快照的元数据只做精确比较：历史超长值可原样提交。"""
    from webapp import server as server_module

    captured = {}

    def fake_update(doc_id, content, metadata=None, **kwargs):
        captured["metadata"] = metadata
        captured["expect_metadata"] = kwargs.get("expect_metadata")
        return {"status": "success", "id": doc_id, "parts": 1}

    monkeypatch.setattr(server_module, "update_document", fake_update)
    long_value = "长" * 240
    response = _client().put(
        "/api/knowledge/documents/1",
        json={
            "content": "新内容",
            "metadata": {"category": "短", "title": "旧"},
            "expect": "旧内容",
            "expect_parts": 1,
            "expect_group": "",
            "expect_metadata": {"category": long_value, "title": "旧"},
        },
    )
    assert response.status_code == 200, response.text
    assert captured["expect_metadata"]["category"] == long_value, captured
    assert captured["metadata"]["category"] == "短", captured

    # 新建接口保持严格 200 字上限
    response = _client().post(
        "/api/knowledge/documents",
        json={"content": "x", "metadata": {"category": "长" * 201}},
    )
    assert response.status_code == 422, response.text
    print("✓ 更新快照接受历史超长元数据，新建仍严格")


def test_static_assets_no_cache_header():
    """/static/ 响应必须带 Cache-Control: no-cache（发版后不能吃到旧脚本）。"""
    response = _client().get("/static/app.js")
    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")
    print("✓ 静态资源 no-cache 头")


def test_import_skips_recently_deleted_sessions(monkeypatch):
    """墓碑期内被删除的会话：恢复时其消息与元数据整体跳过并在 skipped 说明。"""
    client = _client()
    user_id = _unique_user("web_test_import_tomb")
    state = client.app.state.aligo
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-del",
    )
    backup = {
        "chat_history": [
            {"role": "user", "content": "已删会话", "session_id": "s-del"},
            {"role": "user", "content": "正常会话", "session_id": "s-ok"},
        ],
        "session_titles": {"s-del": "旧标题", "s-ok": "新标题"},
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["messages_added"] == 1
        assert any("最近被删除" in item for item in payload["skipped"])
    finally:
        # 直跑自动发现现在会执行本用例：经真实 /api/import 写入 MEMORY_DIR，
        # 必须清理，否则每次运行遗留一对测试用户文件与锁文件
        _cleanup(user_id)
    print("✓ /api/import 跳过墓碑期内的已删除会话")


def test_import_all_sessions_skipped_reports_not_ok(monkeypatch):
    """备份条目全部落在墓碑期会话：什么都没恢复，ok 必须为 false（与
    CLI「没有可恢复的数据」退出码 1 同口径）。"""
    client = _client()
    user_id = _unique_user("web_test_import_all_skipped")
    state = client.app.state.aligo
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-del",
    )
    backup = {
        "chat_history": [
            {"role": "user", "content": "已删会话", "session_id": "s-del"},
        ],
        "session_titles": {"s-del": "旧标题"},
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["ok"] is False, payload
        assert payload["messages_added"] == 0
        assert any(
            "没有可恢复的数据" in item for item in payload["skipped"]
        ), payload["skipped"]
    finally:
        _cleanup(user_id)
    print("✓ /api/import 全部跳过时 ok=false 并说明原因")


def test_import_rolls_back_sessions_deleted_during_write(monkeypatch):
    """恢复写库期间其它实例删除会话：写完后复查墓碑并整会话回滚。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_import_late")
    state = client.app.state.aligo
    flags = {"late": False}
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-late" and flags["late"],
    )
    from utils import user_import as user_import_module

    original_import = user_import_module.import_user_data

    def import_then_delete(store, sections, **kwargs):
        counts = original_import(store, sections, **kwargs)
        flags["late"] = True
        return counts

    monkeypatch.setattr(user_import_module, "import_user_data", import_then_delete)
    backup = {
        "chat_history": [
            {"role": "user", "content": "恢复期间被删", "session_id": "s-late"}
        ],
        "session_titles": {"s-late": "恢复期间被删"},
        "pinned_sessions": ["s-late"],
        "archived_sessions": ["s-late"],
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["messages_added"] == 0, payload
        # 会话整体回滚时，标题 / 置顶 / 归档随 delete_chat_session 一并清除：
        # 响应里的计数必须同步扣减，不能仍报「已恢复 1 个标题」
        assert payload["titles"] == 0, payload
        assert payload["pinned"] == 0, payload
        assert payload["archived"] == 0, payload
        assert any("写入期间被删除" in item for item in payload["skipped"]), payload[
            "skipped"
        ]
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert store.get_chat_history(session_id="s-late") == [], (
                "恢复写库期间被删除的会话不能留下消息"
            )
            assert "s-late" not in (store.get_session_titles() or {}), (
                "回滚必须连标题一起清除"
            )
            assert "s-late" not in (store.get_pinned_sessions() or [])
            assert "s-late" not in (store.get_archived_sessions() or [])
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
    finally:
        _cleanup(user_id)
    print("✓ /api/import 回滚恢复写入期间被删除的会话")


def test_import_rollback_failure_reports_not_ok(monkeypatch):
    """迟到回滚失败：被删会话的消息已复活，ok 必须为 false（不能只记日志）。"""
    from context.long_term_memory import LongTermMemory
    from utils.errors import MemoryPersistenceError
    from utils import user_import as user_import_module

    client = _client()
    user_id = _unique_user("web_test_import_rbfail")
    state = client.app.state.aligo
    flags = {"late": False}
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-late" and flags["late"],
    )
    original_import = user_import_module.import_user_data

    def import_then_delete(store, sections, **kwargs):
        counts = original_import(store, sections, **kwargs)
        flags["late"] = True
        return counts

    monkeypatch.setattr(user_import_module, "import_user_data", import_then_delete)
    original_delete = LongTermMemory.delete_chat_session

    def failing_delete(self, session_id):
        if str(session_id) == "s-late":
            raise MemoryPersistenceError("PostgreSQL 操作失败：connection lost")
        return original_delete(self, session_id)

    monkeypatch.setattr(LongTermMemory, "delete_chat_session", failing_delete)
    backup = {
        "chat_history": [
            {"role": "user", "content": "回滚会失败", "session_id": "s-late"}
        ]
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is False, payload
        assert payload["rollback_failed"] is True, payload
        assert any("回滚失败" in item for item in payload["skipped"]), payload[
            "skipped"
        ]
        # 消息确实仍在库中：计数如实保留，语义由 ok/rollback_failed 表达
        assert payload["messages_added"] >= 1, payload
    finally:
        _cleanup(user_id)
    print("✓ /api/import 回滚失败 → ok=false + rollback_failed")


def test_import_rollback_counts_only_applied_entries(monkeypatch):
    """迟到回滚按本次实际生效集合扣减，不按备份段（否则误减存活会话）。"""
    from context.long_term_memory import LongTermMemory
    from utils import user_import as user_import_module

    client = _client()
    user_id = _unique_user("web_test_import_applied")
    state = client.app.state.aligo
    flags = {"late": False, "pre_deleted": False}
    monkeypatch.setattr(
        state,
        "is_recently_deleted",
        lambda uid, sid: uid == user_id and sid == "s-del" and flags["late"],
    )
    # 模拟「消息已写入、元数据尚未写入」时其它实例删除 s-del：
    # set_session_titles 执行前整会话删除，s-del 的标题 / 置顶 / 归档不会
    # 生效（但备份段里仍有它们）
    original_titles = LongTermMemory.set_session_titles

    def titles_after_delete(self, titles, **kwargs):
        if not flags["pre_deleted"]:
            flags["pre_deleted"] = True
            self.delete_chat_session("s-del")
        return original_titles(self, titles, **kwargs)

    monkeypatch.setattr(LongTermMemory, "set_session_titles", titles_after_delete)
    original_import = user_import_module.import_user_data

    def import_then_flag(store, sections, **kwargs):
        counts = original_import(store, sections, **kwargs)
        flags["late"] = True
        return counts

    monkeypatch.setattr(user_import_module, "import_user_data", import_then_flag)
    backup = {
        "chat_history": [
            {"role": "user", "content": "被删会话", "session_id": "s-del"},
            {"role": "user", "content": "存活会话", "session_id": "s-ok"},
        ],
        "session_titles": {"s-del": "删", "s-ok": "存"},
        "pinned_sessions": ["s-del", "s-ok"],
        "archived_sessions": ["s-del", "s-ok"],
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["messages_added"] == 1, payload
        assert payload["titles"] == 1, payload
        assert payload["pinned"] == 1, payload
        assert payload["archived"] == 1, payload
        assert any("写入期间被删除" in item for item in payload["skipped"])
        from context.storage import create_long_term_memory

        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert store.get_chat_history(session_id="s-del") == []
            assert store.get_session_titles().get("s-ok") == "存"
            assert "s-ok" in (store.get_pinned_sessions() or [])
            assert "s-ok" in (store.get_archived_sessions() or [])
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
    finally:
        _cleanup(user_id)
    print("✓ 迟到回滚只扣本次实际生效的条目")


def test_storage_failure_does_not_double_log(caplog):
    """调用方已用具体上下文记录异常：_storage_failure 只构造 503，不再重复。"""
    import logging

    from webapp.server import _storage_failure

    with caplog.at_level(logging.WARNING):
        error = _storage_failure("读取行程失败", RuntimeError("boom"))
    assert error.status_code == 503
    assert "读取行程失败" in str(error.detail)
    assert caplog.text == "", caplog.text


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
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
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

    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore())
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


def test_trip_calendar_uid_stable_after_edit():
    """面板编辑行程后重新「加入日历」：UID 不变（客户端更新原事件）。"""
    client = _client()
    user_id = _unique_user("web_test_uid_stable")
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-07",
                "purpose": "会议",
            },
        )
        assert created.status_code == 201, created.text
        trip_id = created.json()["trip_id"]

        def uid():
            response = client.get(
                f"/api/trips/{trip_id}/calendar",
                params={"user_id": user_id},
            )
            assert response.status_code == 200, response.text
            return next(
                line
                for line in response.text.splitlines()
                if line.startswith("UID:")
            )

        before = uid()
        edited = client.put(
            f"/api/trips/{trip_id}",
            json={
                "user_id": user_id,
                "start_date": "2026-12-09",
                "end_date": "2026-12-11",
                "destination": "杭州",
            },
        )
        assert edited.status_code == 200, edited.text
        assert uid() == before, "编辑日期 / 路线后日历 UID 必须保持不变"
        purpose_only = client.put(
            f"/api/trips/{trip_id}",
            json={"user_id": user_id, "purpose": "休假"},
        )
        assert purpose_only.status_code == 200, purpose_only.text
        assert uid() == before
        print("✓ 面板编辑行程后日历 UID 保持稳定")
    finally:
        _cleanup(user_id)


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


def test_ics_export_reports_skipped_dateless_trips():
    """ICS 导出跳过缺出发日期的行程：响应头带条数，前端据此提示。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_ics_skipped")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-11-09",
                    "end_date": "2026-11-10",
                    "purpose": "出差",
                }
            )
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "广州",
                    "start_date": "",
                    "end_date": "",
                    "purpose": "待定",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        calendar = _client().get(
            "/api/trips/calendar", params={"user_id": user_id}
        )
        assert calendar.status_code == 200
        assert calendar.text.count("BEGIN:VEVENT") == 1, "缺日期行程不产生事件"
        assert calendar.headers.get("x-aligo-export-skipped") == "1"

        filtered = _client().get(
            "/api/trips/export", params={"user_id": user_id, "format": "ics"}
        )
        assert filtered.status_code == 200
        assert filtered.headers.get("x-aligo-export-skipped") == "1", (
            "format=ics 与 /api/trips/calendar 同一口径"
        )
        print("✓ ICS 导出通报告被跳过的缺日期行程")
    finally:
        _cleanup(user_id)


def test_trips_export_reports_truncated_end_and_unimportable():
    """返程日期无法解析 / 回导会被拒绝的数据：Web 导出用响应头提前提示。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    user_id = _unique_user("web_test_ics_truncated_end")
    clean_user = _unique_user("web_test_ics_clean")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-11-09",
                    "end_date": "待定",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        clean_store = create_long_term_memory(clean_user, str(MEMORY_DIR))
        try:
            clean_store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-11-12",
                    "end_date": "2026-11-13",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(clean_store, "close", None)
            if callable(close):
                close()

        calendar = _client().get(
            "/api/trips/calendar", params={"user_id": user_id}
        )
        assert calendar.status_code == 200
        assert calendar.text.count("BEGIN:VEVENT") == 1
        assert calendar.headers.get("x-aligo-export-skipped") == "0"
        assert calendar.headers.get("x-aligo-export-truncated-end") == "1", (
            "返回头必须告知返程日期被截成单日"
        )
        # X-ALIGO-TRIP-END 让导出的 ICS 仍可被本应用回导，不降低身份
        assert calendar.headers.get("x-aligo-export-unimportable") == "0"
        assert "DTEND;VALUE=DATE:20261110" in calendar.text, "日历仍按单日导出"
        # 文件名带用户维度：多账号脚本下载不再互相覆盖（与 CLI 同口径）
        safe_user = server_module._safe_filename(user_id, "user")
        assert (
            f'aligo_{safe_user}_trips.ics'
            in calendar.headers.get("content-disposition", "")
        )

        filtered = _client().get(
            "/api/trips/export", params={"user_id": user_id, "format": "ics"}
        )
        assert filtered.status_code == 200
        assert filtered.headers.get("x-aligo-export-truncated-end") == "1"
        assert filtered.headers.get("x-aligo-export-unimportable") == "0"

        single = _client().get(
            "/api/trips/trip_1/calendar", params={"user_id": user_id}
        )
        assert single.status_code == 200
        assert single.headers.get("x-aligo-export-truncated-end") == "1"
        assert single.headers.get("x-aligo-export-unimportable") == "0"
        assert (
            f'aligo_{safe_user}_trip_1.ics'
            in single.headers.get("content-disposition", "")
        )

        clean = _client().get(
            "/api/trips/calendar", params={"user_id": clean_user}
        )
        assert clean.headers.get("x-aligo-export-truncated-end") == "0"
        assert clean.headers.get("x-aligo-export-unimportable") == "0"
        clean_safe = server_module._safe_filename(clean_user, "user")
        assert clean_safe != safe_user
        assert (
            f'aligo_{clean_safe}_trips.ics'
            in clean.headers.get("content-disposition", "")
        )
        print("✓ Web 导出通报返程截断 / 回导不可用 + 文件名含用户维度")
    finally:
        _cleanup(user_id)
        _cleanup(clean_user)


def test_trips_csv_export_reports_unimportable_rows():
    """CSV 台账导出的回导自检响应头与 CLI trips-export 同口径。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    user_id = _unique_user("web_test_csv_unimportable")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-11-09",
                    "end_date": "2026-11-10",
                    "purpose": "出差",
                }
            )
            # 倒置日期：CSV 导入会整行拒绝（JSON 存储不校验日期，可写入）
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "广州",
                    "start_date": "2026-11-20",
                    "end_date": "2026-11-18",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        response = _client().get(
            "/api/trips/export", params={"user_id": user_id}
        )
        assert response.status_code == 200
        assert response.headers.get("x-aligo-export-unimportable") == "1"
        assert "广州" in response.text, "只告警，不得改变导出字节"
        safe_user = server_module._safe_filename(user_id, "user")
        assert (
            f'aligo_{safe_user}_trips.csv'
            in response.headers.get("content-disposition", "")
        )
        print("✓ CSV 导出带回导自检头（脏行仍在正文中）")
    finally:
        _cleanup(user_id)


def test_trips_status_filter_uses_client_today():
    """status 过滤接受面板上报的浏览器本地日期，与状态徽标同口径。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_today")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2099-01-01",
                    "end_date": "2099-01-02",
                    "purpose": "出差",
                }
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        inside = _client().get(
            "/api/trips",
            params={
                "user_id": user_id,
                "status": "active",
                "today": "2099-01-01",
            },
        )
        assert inside.status_code == 200 and inside.json()["total"] == 1
        outside = _client().get(
            "/api/trips",
            params={
                "user_id": user_id,
                "status": "active",
                "today": "2098-12-31",
            },
        )
        assert outside.status_code == 200 and outside.json()["total"] == 0
        exported = _client().get(
            "/api/trips/export",
            params={
                "user_id": user_id,
                "status": "active",
                "today": "2099-01-01",
                "format": "ics",
            },
        )
        assert exported.text.count("BEGIN:VEVENT") == 1
        default_export = _client().get(
            "/api/trips/export",
            params={"user_id": user_id, "status": "active", "format": "ics"},
        )
        assert default_export.text.count("BEGIN:VEVENT") == 0, "缺省仍是服务器日期"
        bad = _client().get(
            "/api/trips",
            params={"user_id": user_id, "status": "active", "today": "2099-02-30"},
        )
        assert bad.status_code == 422
        print("✓ status 过滤按面板 today 计算（缺省服务器日期不变）")
    finally:
        _cleanup(user_id)


def test_trips_import_ics_error_includes_event_index():
    """全坏 ICS：400 detail 用「事件N」而不是丢失序号或误标行号。"""
    user_id = _unique_user("web_test_import_ics_detail")
    try:
        bad_ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:北京 → 上海（会议）\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:北京 → 广州（会议）\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        response = _client().post(
            "/api/trips/import",
            params={"user_id": user_id},
            json={"content": bad_ics},
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "事件1" in detail and "事件2" in detail, detail
        assert "事件0" not in detail
        print("✓ ICS 导入 400 带事件序号")
    finally:
        _cleanup(user_id)


def test_trips_import_uses_reported_timezone():
    """面板上报的浏览器时区参与定时事件取日期（容器 UTC 也不漂一天）。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_import_tz")
    try:
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "BEGIN:VEVENT\r\nUID:tz\r\nDTSTART:20261110T233000Z\r\n"
            "SUMMARY:北京 → 上海（会议）\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        response = _client().post(
            "/api/trips/import",
            params={"user_id": user_id},
            json={"content": ics, "timezone": "Asia/Shanghai"},
        )
        assert response.status_code == 200, response.text
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            trips = store.get_trip_history(limit=None)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        assert trips and trips[0]["start_date"] == "2026-11-11", trips
        too_long = _client().post(
            "/api/trips/import",
            params={"user_id": user_id},
            json={"content": ics, "timezone": "A" * 65},
        )
        assert too_long.status_code == 422
        print("✓ Web 导入按上报时区换算定时事件日期")
    finally:
        _cleanup(user_id)


def test_trips_import_returns_merge_warnings():
    """跨行错位引号：dry-run 预览返回 warnings，不影响 skipped 计数。"""
    user_id = _unique_user("web_test_import_warnings")
    try:
        text = (
            "出发地,目的地,出发日期,返程日期,目的\n"
            '广州,"深圳,2026-12-02\n北京,上海",2026-12-03,2026-12-03,会议\n'
        )
        response = _client().post(
            "/api/trips/import",
            params={"user_id": user_id, "dry_run": 1},
            json={"content": text},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["skipped_total"] == 0
        assert payload["warnings_total"] == 1
        assert payload["warnings"][0]["line"] == 2
        print("✓ Web 导入预览透传跨行告警")
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

    seen_strict = {"value": None}

    def _open(user_id, strict=False, **kwargs):
        seen_strict["value"] = strict
        return _BrokenStore()

    monkeypatch.setattr(server_module, "_open_long_term", _open)
    client = _client()
    for path in ("/api/trips/calendar", "/api/trips/trip_1/calendar"):
        response = client.get(path, params={"user_id": "u-ics"})
        assert response.status_code == 503, path
    assert seen_strict["value"] is True, (
        "日历导出必须以 strict 打开：PG 故障期不能返回回退 JSON 的空日历"
    )
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


def test_create_trip_preserves_purpose_on_duplicate_submit():
    """重复提交同一行程（未提供目的）不能把用户改过的目的重置为「出差」。"""
    user_id = _unique_user("web_test_trip_purpose")
    client = _client()
    base = {
        "user_id": user_id,
        "origin": "北京",
        "destination": "上海",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
    }
    try:
        first = client.post("/api/trips", json={**base, "purpose": "探亲"})
        assert first.status_code == 201, first.text
        assert first.json()["created"] is True

        # 未提供目的且命中既有行程：保留原目的
        second = client.post("/api/trips", json=base)
        assert second.status_code == 201, second.text
        assert second.json()["created"] is False
        trips = client.get("/api/trips", params={"user_id": user_id}).json()["trips"]
        assert trips[0]["purpose"] == "探亲", trips

        # 全新路线未提供目的：仍落默认「出差」
        third = client.post(
            "/api/trips",
            json={
                **base,
                "destination": "杭州",
                "start_date": "2026-12-05",
                "end_date": "2026-12-06",
            },
        )
        assert third.status_code == 201, third.text
        assert third.json()["created"] is True
        trips = client.get("/api/trips", params={"user_id": user_id}).json()["trips"]
        purposes = {trip["destination"]: trip.get("purpose") for trip in trips}
        assert purposes["杭州"] == "出差", purposes

        # 显式提供目的仍按用户输入覆盖
        fourth = client.post("/api/trips", json={**base, "purpose": "出差"})
        assert fourth.status_code == 201, fourth.text
        trips = client.get("/api/trips", params={"user_id": user_id}).json()["trips"]
        assert trips[0]["purpose"] == "出差", trips
        print("✓ 重复提交保留目的，新行程默认「出差」")
    finally:
        _cleanup(user_id)


def test_create_trip_created_flag_uses_write_lock_result(monkeypatch):
    """锁外窗口里的其它新增不能把「更新」误报为「新增」。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    user_id = _unique_user("web_test_trip_created_flag")
    store = create_long_term_memory(user_id, str(MEMORY_DIR))
    store.save_trip_history(
        {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "purpose": "会议",
        }
    )

    class _RacyStore:
        def __init__(self, inner):
            self.inner = inner

        def get_trip_history(self, limit=None):
            return self.inner.get_trip_history(limit=limit)

        def save_trip_history(
            self, record, *, max_total=None, created_out=None, default_new_purpose=None
        ):
            # 模拟窗口里另一进程先插入无关行程：长度差口径会误判为新增
            self.inner.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2027-01-01",
                    "end_date": "2027-01-02",
                }
            )
            return self.inner.save_trip_history(
                record,
                max_total=max_total,
                created_out=created_out,
                default_new_purpose=default_new_purpose,
            )

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda uid, strict=False, **kwargs: _RacyStore(store),
    )
    client = _client()
    try:
        response = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["created"] is False, response.json()
        # 未提供目的且命中既有行程：目的不被重置
        trips = store.get_trip_history(limit=None)
        kept = next(trip for trip in trips if trip["destination"] == "上海")
        assert kept["purpose"] == "会议", kept
        print("✓ created 取自写锁内结果（并发窗口不误报）")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
        _cleanup(user_id)


def test_trip_write_readback_failure_returns_saved_with_warning(monkeypatch):
    """写入已提交后的重读失败按成功 + warning 返回（与 CLI 同口径）。

    此前 Web 一律 503「添加 / 更新行程失败」，用户重试会重复新增。
    """
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    user_id = _unique_user("web_test_trip_readback")

    class _FlakyStore:
        def __init__(self, inner):
            self.inner = inner
            self.reads = 0

        def get_trip_history(self, limit=None):
            self.reads += 1
            if self.reads >= 2:
                raise RuntimeError("read boom")
            return self.inner.get_trip_history(limit=limit)

        def save_trip_history(self, record, **kwargs):
            return self.inner.save_trip_history(record, **kwargs)

        def update_trip(self, trip_id, fields):
            return self.inner.update_trip(trip_id, fields)

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "_open_long_term",
        lambda uid, strict=False, **kwargs: _FlakyStore(
            create_long_term_memory(uid, str(MEMORY_DIR))
        ),
    )
    client = _client()
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "2026-12-02",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["saved"] is True and body["warning"], body
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            trips = store.get_trip_history(limit=None)
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        assert len(trips) == 1, "重读失败不能回滚已提交的行程"

        updated = client.put(
            f"/api/trips/{body['trip_id']}",
            json={"user_id": user_id, "purpose": "会议"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["warning"] and updated.json()["trip"]["purpose"] == "会议"
        print("✓ 行程写后重读失败按成功 + warning 返回（不误报 503）")
    finally:
        _cleanup(user_id)


def test_trip_and_session_title_limits_measured_after_strip():
    """与 CLI / 导入同一计量对象：先 strip 再按码点判上限。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_strip_limits")
    long_origin = "北" * 100
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "  " + long_origin + "  ",
                "destination": "上海",
                "start_date": "2026-12-01",
            },
        )
        assert created.status_code == 201, created.text
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            trip = store.get_trip_history(limit=None)[0]
            store.add_chat_message("user", "hi", session_id="s-title")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        assert trip["origin"] == long_origin, trip

        renamed = client.put(
            "/api/sessions/s-title/title",
            json={"user_id": user_id, "title": "  " + "长" * 60 + "  "},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "长" * 60, renamed.json()
        print("✓ 行程字段 / 会话标题先 strip 再判长度上限")
    finally:
        _cleanup(user_id)


def test_trip_default_purpose_only_applies_to_insert(monkeypatch):
    """默认目的只在存储写锁内确认新增时补（锁外预判会覆盖并发改过的目的）。"""
    from context.storage import create_long_term_memory

    user_id = _unique_user("web_test_default_purpose")
    store = create_long_term_memory(user_id, str(MEMORY_DIR))
    try:
        record = {
            "origin": "北京",
            "destination": "上海",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
        }
        trip_id = store.save_trip_history(record, default_new_purpose="出差")
        saved = next(
            trip
            for trip in store.get_trip_history(limit=None)
            if trip["trip_id"] == trip_id
        )
        assert saved["purpose"] == "出差", saved
        store.update_trip(trip_id, {"purpose": "探亲"})
        store.save_trip_history(record, default_new_purpose="出差")
        saved = next(
            trip
            for trip in store.get_trip_history(limit=None)
            if trip["trip_id"] == trip_id
        )
        assert saved["purpose"] == "探亲", saved
        print("✓ 默认目的只作用于新增（更新保留用户设置）")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
        _cleanup(user_id)


def test_knowledge_busy_refunds_kb_rate_limit(monkeypatch):
    """写锁忙 = 零写入：添加 / 更新 / 删除与导入同口径归还 KB 额度。"""
    from utils.knowledge_admin import KnowledgeBusyError
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user = _unique_user("web_test_kb_busy_refund")
    try:
        def _busy(*args, **kwargs):
            raise KnowledgeBusyError("知识库正在写入（导入 / 编辑 / 删除）")

        monkeypatch.setattr(server_module, "add_document", _busy)
        monkeypatch.setattr(server_module, "delete_document", _busy)
        monkeypatch.setattr(server_module, "update_document", _busy)
        monkeypatch.setattr(server_module, "get_document", _busy)
        for method, path, payload in (
            (
                client.post,
                "/api/knowledge/documents",
                {"content": "x", "metadata": {}},
            ),
            (
                client.put,
                "/api/knowledge/documents/1",
                {"content": "x", "metadata": {}},
            ),
            (client.delete, "/api/knowledge/documents/1", None),
        ):
            # 每个请求一条独立额度：请求内记账后归还，事后 check 不应 429
            state.kb_limiter = RateLimiter(limit=1, window_sec=60)
            response = (
                method(path, json=payload) if payload is not None else method(path)
            )
            assert response.status_code == 503, response.text[:200]
            assert "知识库" in response.json()["detail"]
            assert state.kb_limiter.check("testclient") is None, (
                "忙拒绝未写入，额度必须归还"
            )
        print("✓ 知识库写锁忙 503 归还 KB 额度（与导入同口径）")
    finally:
        _cleanup(user)


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


def test_delete_last_turn_turn_identity():
    """同文提问并发追加时按 client_turn_id 精确删除，不误删对方的新回合。"""
    import json as json_module

    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_last_turn_id")
    session_id = "edit-turn-id"

    def _assistant(turn_id: str) -> str:
        return json_module.dumps(
            {"results": [], "_meta": {"client_turn_id": turn_id}},
            ensure_ascii=False,
        )

    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "问题 A", session_id)
            store.add_chat_message("assistant", _assistant("turn-A"), session_id)
            store.add_chat_message("user", "问题 A", session_id)
            store.add_chat_message("assistant", _assistant("turn-B"), session_id)
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()

        stale = client.delete(
            f"/api/sessions/{session_id}/last-turn",
            params={"user_id": user_id, "expect": "问题 A", "turn_id": "turn-A"},
        )
        assert stale.status_code == 200
        assert stale.json()["removed"] == 0
        assert stale.json()["mismatch"] is True

        stale_post = client.post(
            f"/api/sessions/{session_id}/last-turn",
            json={"user_id": user_id, "expect": "问题 A", "turn_id": "turn-A"},
        )
        assert stale_post.json()["removed"] == 0

        ok = client.post(
            f"/api/sessions/{session_id}/last-turn",
            json={"user_id": user_id, "expect": "问题 A", "turn_id": "turn-B"},
        )
        assert ok.json()["removed"] == 2

        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert len(store.get_chat_history(session_id=session_id)) == 2
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        print("✓ 删除最后一轮按 turn_id 精确匹配（同文提问不误删）")
    finally:
        _cleanup(user_id)


def test_allowed_hosts_env_strips_bom(monkeypatch):
    """文件注入的 ALIGO_ALLOWED_HOSTS 带 BOM 时白名单仍能命中。"""
    from webapp import server as server_module

    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", "﻿example.com")
    assert server_module._host_allowed("example.com"), "BOM 不能让白名单首项失效"
    print("✓ ALIGO_ALLOWED_HOSTS 去 BOM")


def test_allowed_hosts_env_strips_injected_quotes(monkeypatch):
    """成对引号注入的 ALIGO_ALLOWED_HOSTS 仍能命中，不再全站 400。"""
    from webapp import server as server_module

    monkeypatch.setenv("ALIGO_ALLOWED_HOSTS", '"aligo.example.com"')
    assert server_module._host_allowed("aligo.example.com"), "整段引号必须剥离"
    monkeypatch.setenv(
        "ALIGO_ALLOWED_HOSTS", "'x.example.com,*.y.example.com'"
    )
    assert server_module._host_allowed("x.example.com")
    assert server_module._host_allowed("a.y.example.com")
    monkeypatch.setenv(
        "ALIGO_ALLOWED_HOSTS", '"a.example.com","b.example.com"'
    )
    assert server_module._host_allowed("a.example.com"), "逐项引号必须剥离"
    assert server_module._host_allowed("b.example.com")
    print("✓ ALIGO_ALLOWED_HOSTS 去成对引号（整段 / 逐项）")


def test_trusted_proxy_cidrs_env_strips_injected_quotes():
    """逐项引号注入的 TRUSTED_PROXY_CIDRS 仍能命中（与 ALLOWED_HOSTS 同口径）。"""
    from webapp import server as server_module

    parse = server_module._trusted_proxy_networks
    plain = parse("127.0.0.1/32,10.0.0.0/8")
    assert len(plain) == 2, plain
    for quoted in (
        '"127.0.0.1/32","10.0.0.0/8"',
        "'127.0.0.1/32','10.0.0.0/8'",
    ):
        assert parse(quoted) == plain, (quoted, parse(quoted))
    # 整段引号（config._env_str 已剥）与裸值行为不变
    assert parse('"10.0.0.0/8"') == parse("10.0.0.0/8")
    print("✓ TRUSTED_PROXY_CIDRS 去逐项成对引号")


def test_delete_session_busy_lock_refused(monkeypatch):
    """删除会话在用户锁忙碌时有界返回 409，不再排队到前端超时后仍执行。"""
    import asyncio as asyncio_module

    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    client = TestClient(app)

    class _StuckLock:
        async def acquire(self):
            await asyncio_module.sleep(30)

        def release(self):
            pass

    monkeypatch.setattr(state, "lock_for", lambda user_id: _StuckLock())
    monkeypatch.setattr(server_module, "_DELETE_LOCK_TIMEOUT_SEC", 0.05)
    user_id = _unique_user("web_test_delete_busy")
    response = client.delete("/api/sessions/s-busy", params={"user_id": user_id})
    assert response.status_code == 409
    assert "正在生成回复" in response.json()["detail"]
    print("✓ 删除会话在用户锁忙碌时有界返回 409")


def test_sessions_export_rejects_messages_without_session():
    """只有无 session_id 的旧消息时按渲染条数 404，不返回空台账。"""
    user_id = _unique_user("web_test_export_empty_render")
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "旧消息",
                        "timestamp": "2025-01-01T00:00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    try:
        for fmt in ("md", "html"):
            response = client.get(
                "/api/sessions/export",
                params={"user_id": user_id, "format": fmt},
            )
            assert response.status_code == 404, (fmt, response.text)
    finally:
        _cleanup(user_id)
    print("✓ 会话导出按渲染条数判断空")


def test_api_export_cleans_dirty_backup():
    """GET /api/export 的备份必须能被 /api/import 直接恢复（脏数据先清洗）。"""
    from utils.user_import import parse_user_export

    user_id = _unique_user("web_test_export_clean")
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "assistant",
                        "content": (
                            '{"status":"completed",'
                            '"_meta":{"usage":{"calls":NaN}}}'
                        ),
                        "session_id": "s-dirty",
                        "timestamp": "2026-01-01T00:00:00",
                    },
                    {
                        "role": "user",
                        "content": "含\x00NUL",
                        "session_id": "s-dirty",
                        "timestamp": "2026-01-01T00:00:01",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    try:
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 200, response.text
        payload = response.text
        assert "NaN" not in payload and "\x00" not in payload
        sections, errors = parse_user_export(payload)
        assert not errors, errors
        assert len(sections["messages"]) == 2
    finally:
        _cleanup(user_id)
    print("✓ /api/export 备份清洗脏数据后可恢复")


def test_export_with_knowledge_uses_import_chunk_plan(monkeypatch):
    """知识段体积按导入重切分后的分片估算；分片计划超限时省略知识段导出
    （不能产出导入必超限的文件，也不能让用户数据永久 413）。"""
    from context.storage import create_long_term_memory
    from utils import knowledge_admin, user_import
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_export_kb_plan")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("你好", "你好呀", "sess-export-kb")
        monkeypatch.setattr(
            server_module,
            "export_documents",
            lambda: [{"content": "原始长文档", "metadata": {}}],
        )
        calls: dict = {}

        def fake_import(
            rows, dry_run=False, return_pieces=False, **kwargs
        ):
            calls.update(kwargs)
            return {"pieces": [{"content": "x" * 100}] * 100}

        monkeypatch.setattr(knowledge_admin, "import_documents", fake_import)
        monkeypatch.setattr(user_import, "MAX_BACKUP_BYTES", 5000)
        response = client.get(
            "/api/export",
            params={"user_id": user_id, "with_knowledge": 1},
        )
        # 分片计划超限属于知识轴：降级为省略知识段的备份 + 提示头，
        # 用户数据本身仍可导出 / 恢复
        assert response.status_code == 200, response.text
        assert "knowledge" not in response.json()
        assert response.headers.get("x-knowledge-omitted") == "over-limit"
        assert calls.get("ignore_existing") is True, (
            "导出体积预演必须按导入空库规划，不能对源库去重后只剩新增分片"
        )
        raw_problem = user_import.backup_limit_problem(
            {
                "chat_history": [],
                "knowledge": [{"content": "原始长文档", "metadata": {}}],
            },
            crlf=False,
        )
        assert raw_problem is None, raw_problem
    finally:
        _cleanup(user_id)
    print("✓ with_knowledge 导出按导入分片计划估算体积")


def test_export_with_knowledge_filename_and_plain_name(monkeypatch):
    """含知识段的备份下载名带 _with_knowledge 后缀；普通导出的名称不变。"""
    from context.storage import create_long_term_memory
    from utils import knowledge_admin
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_export_kb_name")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("你好", "你好呀", "sess-export-name")
        monkeypatch.setattr(
            server_module,
            "export_documents",
            lambda: [{"content": "知识", "metadata": {}}],
        )
        monkeypatch.setattr(
            knowledge_admin,
            "import_documents",
            lambda rows, dry_run=False, return_pieces=False, **kwargs: {
                "pieces": [{"content": "知识", "metadata": {}}]
            },
        )
        with_kb = client.get(
            "/api/export", params={"user_id": user_id, "with_knowledge": 1}
        )
        assert with_kb.status_code == 200, with_kb.text[:200]
        assert "knowledge" in with_kb.json()
        disposition = with_kb.headers["content-disposition"]
        assert "_with_knowledge.json" in disposition, disposition

        plain = client.get("/api/export", params={"user_id": user_id})
        assert plain.status_code == 200, plain.text[:200]
        plain_disposition = plain.headers["content-disposition"]
        assert "_with_knowledge" not in plain_disposition, plain_disposition
    finally:
        _cleanup(user_id)
    print("✓ 含知识段备份的下载名与普通备份区分")


def test_export_early_rejects_oversized_json_account(monkeypatch):
    """JSON 存储文件已超上限：导出在读取前拒绝，不做全量读取。"""
    from utils import user_import
    from webapp import server as server_module

    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_test_export_oversized")
    path = MEMORY_DIR / f"{user_id}.json"
    try:
        # 合法但超限的记忆文件（早期拒绝不能依赖文件损坏）
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.save_preference("home_location", "x" * 4096)
        assert path.stat().st_size > 1024
        monkeypatch.setattr(user_import, "MAX_BACKUP_BYTES", 1024)
        loads = []
        real_load = server_module._load_user_data
        monkeypatch.setattr(
            server_module,
            "_load_user_data",
            lambda *args, **kwargs: (loads.append(1), real_load(*args, **kwargs))[1],
        )
        response = client.get("/api/export", params={"user_id": user_id})
        assert response.status_code == 413, response.text[:200]
        assert "账号数据约" in response.json()["detail"]
        assert loads == [], "超限账号不应先做全量读取"
        # md 导出是摘要（不用于恢复），不受备份字节上限约束
        md = client.get(
            "/api/export", params={"user_id": user_id, "format": "md"}
        )
        assert md.status_code == 200, md.text[:200]
    finally:
        path.unlink(missing_ok=True)
        _cleanup(user_id)
    print("✓ 超限 JSON 账号的导出在读取前拒绝")


def test_import_dry_run_counts_dropped_knowledge():
    """解析阶段整组丢弃的残缺知识行计入 knowledge_skipped（README 口径）。"""
    client = _client()
    user_id = _unique_user("web_test_import_kb_dropped")
    try:
        backup = {
            "preferences": {"home_location": "北京"},
            "knowledge": [
                {
                    "content": "缺片组的分片",
                    "metadata": {"group": "g1", "part": "1", "parts": "2"},
                },
                {"content": "有效的独立片段", "metadata": {}},
            ],
        }
        response = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": True},
            json={"backup": backup},
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["knowledge_skipped"] == 1, payload
        assert any(
            "知识分片组" in item for item in payload["skipped"]
        ), payload["skipped"]
    finally:
        _cleanup(user_id)
    print("✓ 解析丢弃的知识行计入 knowledge_skipped")


def test_import_skips_oversized_preference_value():
    """恢复按条跳过超大偏好值（面板之后无法再保存的存量值）。"""
    client = _client()
    user_id = _unique_user("web_test_import_pref_big")
    try:
        response = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": True},
            json={
                "backup": {
                    "preferences": {
                        "home_location": "北京",
                        "oversized": "x" * 10001,
                    }
                }
            },
        )
        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        assert payload["preferences"] == 1, payload
        assert any(
            "序列化后超过" in item for item in payload["skipped"]
        ), payload["skipped"]
    finally:
        _cleanup(user_id)
    print("✓ 超大偏好值在恢复时按条跳过")


def test_knowledge_volume_rows_fails_closed_on_planning_error(monkeypatch):
    """知识库规划失败时返回 None：调用方可拒绝或降级，reason_out 标注原因。"""
    from utils import knowledge_admin, user_import

    rows = [{"content": "原始行", "metadata": {}}]

    def boom(*args, **kwargs):
        raise RuntimeError("kb down")

    monkeypatch.setattr(knowledge_admin, "import_documents", boom)
    assert user_import.knowledge_rows_for_volume_check(rows) is None
    reasons: dict = {}
    problem = user_import.export_backup_limit_problem(
        {"knowledge": rows}, reason_out=reasons
    )
    assert problem and "无法按导入规则规划" in problem, problem
    assert reasons.get("knowledge") == "planning-failed", reasons
    print("✓ 知识规划失败时体积校验 fail-closed 且标注原因")


def test_session_detail_tolerates_deep_meta():
    """旧数据 _meta 嵌套数千层：会话详情 / 状态接口不能递归过深而 500。"""
    user_id = _unique_user("web_test_deep_meta")
    deep: dict[str, object] = {"leaf": 1}
    # Python 3.10 的 json 编码器在约 1000 层递归即溢出：取 300 层
    # （远高于清洗上限 50，同时可被 json.dumps 正常编码）
    for _ in range(300):
        deep = {"next": deep}
    content = json.dumps({"status": "completed", "results": [], "_meta": deep})
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "问题",
                        "session_id": "s-deep",
                        "timestamp": "2026-01-01T00:00:00",
                    },
                    {
                        "role": "assistant",
                        "content": content,
                        "session_id": "s-deep",
                        "timestamp": "2026-01-01T00:00:01",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    try:
        response = client.get(
            "/api/sessions/s-deep", params={"user_id": user_id}
        )
        assert response.status_code == 200, response.text
        status = client.get("/api/status", params={"user_id": user_id})
        assert status.status_code == 200, status.text
    finally:
        _cleanup(user_id)
    print("✓ 超深 _meta 不再让会话详情 500")


def test_null_session_id_is_not_a_ghost_session():
    """session_id 为 None 的旧消息不能被当成名为 None 的会话。"""
    user_id = _unique_user("web_test_null_session")
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "无归属旧消息",
                        "session_id": None,
                        "timestamp": "2025-01-01T00:00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    try:
        renamed = client.put(
            "/api/sessions/None/title",
            json={"user_id": user_id, "title": "幽灵标题"},
        )
        assert renamed.status_code == 404, renamed.text
        exported = client.get("/api/export", params={"user_id": user_id}).json()
        assert "None" not in (exported.get("session_titles") or {})
    finally:
        _cleanup(user_id)
    print("✓ None 不是幽灵会话")


def test_integer_session_id_is_manageable():
    """旧数据里非字符串 session_id：列表、详情、改标题、删除按字符串口径一致。"""
    user_id = _unique_user("web_test_int_session")
    path = Path(MEMORY_DIR) / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "整数会话",
                        "session_id": 42,
                        "timestamp": "2026-01-01T00:00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    try:
        listed = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert "42" in [item["session_id"] for item in listed["sessions"]]
        detail = client.get(
            "/api/sessions/42", params={"user_id": user_id}
        )
        assert detail.status_code == 200 and detail.json()["session_id"] == "42"
        renamed = client.put(
            "/api/sessions/42/title",
            json={"user_id": user_id, "title": "整数标题"},
        )
        assert renamed.status_code == 200, renamed.text
        deleted = client.delete("/api/sessions/42", params={"user_id": user_id})
        assert deleted.status_code == 200 and deleted.json()["deleted"] == 1
    finally:
        _cleanup(user_id)
    print("✓ 整数会话 ID 可打开 / 改标题 / 删除")


def test_delete_last_turn_busy_lock_refused(monkeypatch):
    """用户锁被长回合占用时有界返回 409，而不是排队到前端超时后仍执行。"""
    import asyncio as asyncio_module

    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    client = TestClient(app)

    class _StuckLock:
        async def acquire(self):
            await asyncio_module.sleep(30)

        def release(self):
            pass

    monkeypatch.setattr(state, "lock_for", lambda user_id: _StuckLock())
    monkeypatch.setattr(server_module, "_DELETE_LOCK_TIMEOUT_SEC", 0.05)
    user_id = _unique_user("web_test_last_turn_busy")
    response = client.delete(
        "/api/sessions/s-busy/last-turn",
        params={"user_id": user_id, "expect": "问题"},
    )
    assert response.status_code == 409
    assert "正在生成回复" in response.json()["detail"]
    print("✓ 删除最后一轮在用户锁忙碌时有界返回 409")


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
            # 非法结束日期（非真实日历日）不能按字符串比较成「进行中」，
            # 按单日处理；斜杠日期是合法身份口径，不再属于此分支
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "广州",
                    "start_date": (today - timedelta(days=5)).isoformat(),
                    "end_date": "2026-13-40",
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

        html_export = _client().get(
            "/api/trips/export", params={"user_id": user_id, "format": "html"}
        )
        assert html_export.status_code == 200
        assert html_export.headers["content-type"].startswith("text/html")
        assert ".html" in html_export.headers.get("content-disposition", "")
        assert html_export.text.startswith("<!doctype html>")
        assert "2026-12-05 ~ 2026-12-07" in html_export.text
        assert "HYPERLINK(&quot;http://example.invalid&quot;)" in html_export.text
        assert "<script" not in html_export.text
        bad_format = _client().get(
            "/api/trips/export", params={"user_id": user_id, "format": "pdf"}
        )
        assert bad_format.status_code == 422

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
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
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

        ics = client.get(
            "/api/trips/export",
            params={"user_id": user_id, "q": "上海", "format": "ics"},
        )
        assert ics.status_code == 200
        assert ics.headers["content-type"].startswith("text/calendar")
        assert ".ics" in ics.headers.get("content-disposition", "")
        assert ics.text.startswith("BEGIN:VCALENDAR")
        assert ics.text.count("BEGIN:VEVENT") == 1, "过滤后的 ICS 只含命中行程"
        assert "上海" in ics.text and "杭州" not in ics.text

        ics_empty = client.get(
            "/api/trips/export",
            params={"user_id": user_id, "q": "不存在", "format": "ics"},
        )
        assert ics_empty.status_code == 200
        assert ics_empty.text.count("BEGIN:VEVENT") == 0, "无匹配时 ICS 不含事件"
        print("✓ /api/trips/export q 过滤（CSV / ICS）")
    finally:
        _cleanup(user_id)


def test_trips_dedupe_endpoint_dry_run_and_apply(monkeypatch):
    """POST /api/trips/dedupe：dry-run 只预览，正式执行保留最早一条。"""
    from webapp import server as server_module

    class _Store:
        def __init__(self):
            self.trips = [
                {
                    "trip_id": "t1",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-1-5",
                    "end_date": "2026-1-6",
                },
                {
                    "trip_id": "t2",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-06",
                },
                {
                    "trip_id": "t3",
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-02-01",
                    "end_date": "2026-02-02",
                },
            ]
            self.deleted = []

        def get_trip_history(self, limit=None):
            return list(self.trips)

        def reload(self):
            pass

        def delete_trip(self, trip_id):
            for index, trip in enumerate(self.trips):
                if str(trip.get("trip_id")) == trip_id:
                    self.trips.pop(index)
                    self.deleted.append(trip_id)
                    return True
            return False

        def close(self):
            pass

    store = _Store()
    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, **kwargs: store
    )
    client = _client()
    preview = client.post(
        "/api/trips/dedupe",
        json={"user_id": "u-dedupe", "dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["dry_run"] is True and data["would_remove"] == 1
    assert [entry["trip_id"] for entry in data["duplicates"]] == ["t2"]
    assert store.deleted == [], "预览不能删除任何行程"

    # 省略 dry_run 只预览（默认 True）：按 schema 只填必填字段不能静默删除
    omitted = client.post(
        "/api/trips/dedupe", json={"user_id": "u-dedupe"}
    )
    assert omitted.status_code == 200, omitted.text
    omitted_data = omitted.json()
    assert omitted_data["dry_run"] is True and omitted_data["removed"] == 0
    assert store.deleted == [], "省略 dry_run 不能删除任何行程"

    applied = client.post(
        "/api/trips/dedupe", json={"user_id": "u-dedupe", "dry_run": False}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["removed"] == 1
    assert [trip["trip_id"] for trip in store.trips] == ["t1", "t3"]
    print("✓ /api/trips/dedupe 预览与执行（省略 dry_run 默认只预览）")


def test_trips_dedupe_failure_detail_redacts_paths(monkeypatch):
    """去重部分失败：503 详情只留固定文案，不回显路径 / SQL / 表名。"""
    from webapp import server as server_module

    class _Store:
        def get_trip_history(self, limit=None):
            return []

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, **kwargs: _Store()
    )
    leaked = (
        r"PostgreSQL 操作失败：relation \"aligo_trips\" does not exist"
        "\nLINE 1: DELETE FROM aligo_trips WHERE user_id = %s AND id = %s"
    )
    monkeypatch.setattr(
        server_module,
        "apply_trip_dedupe",
        lambda store, entries, verify: (0, leaked),
    )
    client = _client()
    response = client.post(
        "/api/trips/dedupe",
        json={"user_id": "u-dedupe-redact", "dry_run": False},
    )
    assert response.status_code == 503, response.text[:300]
    detail = response.json()["detail"]
    assert "已删除 0 条" in detail, detail
    for secret in ("aligo_trips", "PostgreSQL", "DELETE", "LINE 1"):
        assert secret not in detail, detail
    print("✓ 去重失败详情不回显内部 SQL / 表名")


def test_trips_dedupe_endpoint_errors_and_ambiguous(monkeypatch):
    """读取失败映射 503；同 id 多行程只报告不删除。"""
    from webapp import server as server_module

    class _Boom:
        def get_trip_history(self, limit=None):
            raise RuntimeError("disk gone")

        def reload(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, **kwargs: _Boom()
    )
    response = _client().post(
        "/api/trips/dedupe", json={"user_id": "u-dedupe", "dry_run": False}
    )
    assert response.status_code == 503
    assert "读取行程失败" in response.json()["detail"]

    class _Ambiguous:
        def __init__(self):
            self.trips = [
                {
                    "trip_id": "dup",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-01-05",
                },
                {
                    "trip_id": "dup",
                    "origin": "北京",
                    "destination": "杭州",
                    "start_date": "2026-01-05",
                },
            ]

        def get_trip_history(self, limit=None):
            return list(self.trips)

        def reload(self):
            pass

        def delete_trip(self, trip_id):
            raise AssertionError("歧义 id 不应被删除")

        def close(self):
            pass

    ambiguous = _Ambiguous()
    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, **kwargs: ambiguous
    )
    data = _client().post(
        "/api/trips/dedupe", json={"user_id": "u-dedupe", "dry_run": False}
    ).json()
    assert data["removed"] == 0
    assert data["ambiguous_ids"] == ["dup"]
    print("✓ /api/trips/dedupe 错误映射与歧义 id")


def test_destructive_endpoints_require_json_body():
    """跨站简单请求（form / text-plain）不能触发去重删除与备份恢复写入。"""
    client = _client()
    form = client.post(
        "/api/trips/dedupe",
        data={"user_id": "u-csrf"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 422, form.text

    plain = client.post(
        "/api/import",
        params={"user_id": "u-csrf"},
        content=b'{"backup": {"preferences": {"home_location": "beijing"}}}',
        headers={"content-type": "text/plain"},
    )
    assert plain.status_code == 415, plain.text
    assert "application/json" in plain.json()["detail"]
    # 请求体未读即拒绝：必须让连接关闭，客户端别再继续上传
    assert plain.headers.get("connection") == "close", plain.headers

    # 与 FastAPI 严格校验同口径：application/*+json 放行，非 application
    # 主类型（text/x+json）仍 415
    structured = client.post(
        "/api/import",
        params={"user_id": "u-csrf", "dry_run": 1},
        content='{"backup": {"preferences": {"home_location": "北京"}}}'.encode(
            "utf-8"
        ),
        headers={"content-type": "application/merge-patch+json"},
    )
    assert structured.status_code == 200, structured.text
    textual = client.post(
        "/api/import",
        params={"user_id": "u-csrf"},
        content=b'{"backup": {"preferences": {}}}',
        headers={"content-type": "text/x+json"},
    )
    assert textual.status_code == 415, textual.text
    print("✓ 破坏性接口拒绝跨站简单请求（JSON body + Content-Type 门禁）")


def test_trips_dedupe_concurrent_requests_keep_one(monkeypatch):
    """两个并发去重请求：用户锁 + 存储层计数守卫，同 id 副本只删一次。"""
    from concurrent.futures import ThreadPoolExecutor

    from webapp import server as server_module

    class _Store:
        def __init__(self):
            self.trips = [
                {
                    "trip_id": "dup",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-01-05",
                },
                {
                    "trip_id": "dup",
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-01-05",
                },
            ]

        def get_trip_history(self, limit=None):
            return [dict(trip) for trip in self.trips]

        def reload(self):
            pass

        def delete_duplicate_trip(self, trip_id):
            matches = [
                index
                for index, trip in enumerate(self.trips)
                if trip["trip_id"] == trip_id
            ]
            if len(matches) < 2:
                return False
            self.trips.pop(matches[-1])
            return True

        def close(self):
            pass

    store = _Store()
    monkeypatch.setattr(
        server_module, "_open_long_term", lambda uid, **kwargs: store
    )
    clients = [_client(), _client()]

    def call(client):
        return client.post(
            "/api/trips/dedupe",
            json={"user_id": "u-race", "dry_run": False},
        ).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, clients))
    assert sorted(item["removed"] for item in results) == [0, 1], results
    assert len(store.trips) == 1, "并发去重不能把同 id 副本删光"
    print("✓ 并发去重请求只删一次（保留一条）")


def test_trips_csv_storage_failure_maps_503(monkeypatch):
    from webapp import server as server_module

    class _BrokenStore:
        def get_trip_history(self, limit=None):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
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
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
    )
    client = _client()
    response = client.delete("/api/sessions/s1", params={"user_id": "u1"})
    assert response.status_code == 503
    state = client.app.state.aligo
    assert state.is_recently_deleted("u1", "s1") is False, "删除失败应撤销墓碑"
    print("✓ 删除会话存储失败 → 503（墓碑回滚）")


def test_delete_endpoints_preserve_http_exception_semantics(monkeypatch):
    """422 / 记忆损坏恢复提示不能被重包成通用 503。"""
    client = _client()
    response = client.delete("/api/sessions/s1", params={"user_id": "con"})
    assert response.status_code == 422, response.text[:200]
    assert "user_id" in str(response.json().get("detail", "")), response.text

    response = client.delete(
        "/api/sessions/s1/last-turn",
        params={"user_id": "con", "expect": "x"},
    )
    assert response.status_code == 422, response.text[:200]

    from fastapi import HTTPException as _HTTPException

    from webapp import server as server_module

    def _quarantined(user_id, strict=False, **kwargs):
        raise _HTTPException(
            status_code=503,
            detail="记忆文件损坏并已隔离备份（路径见服务端日志），请使用备份文件恢复后重试",
        )

    monkeypatch.setattr(server_module, "_open_long_term", _quarantined)
    response = client.delete("/api/sessions/s1", params={"user_id": "u1"})
    assert response.status_code == 503, response.text[:200]
    assert "隔离" in response.json()["detail"], response.text
    state = client.app.state.aligo
    assert state.is_recently_deleted("u1", "s1") is False, "HTTPException 分支也要撤销墓碑"
    print("✓ 删除端点保留 422 / 隔离 503 的原始语义")


def test_group_sessions_normalizes_legacy_ids_and_reserved():
    """整数会话 ID 的置顶 / 归档生效、对象条目不 500、保留字会话不列出。"""
    from webapp.server import _group_sessions

    history = [
        {"session_id": 42, "role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00"},
        {"session_id": 0, "role": "user", "content": "zero", "timestamp": "2026-01-02T00:00:00"},
        {"session_id": "export", "role": "user", "content": "x", "timestamp": "2026-01-03T00:00:00"},
        {"session_id": "s1", "role": "user", "content": "yo", "timestamp": "2026-01-04T00:00:00"},
    ]
    sessions = _group_sessions(
        history,
        limit=10,
        pinned=[42, {"bad": 1}],
        archived=[42, [1]],
        archived_mode="only",
    )
    assert [item["session_id"] for item in sessions] == ["42"], sessions
    assert sessions[0]["archived"] is True
    # 归档视图不按置顶排序，但如实报告存储态（与 CLI --archived --json /
    # PUT pin 读回一致；面板按 archived 过滤掉图钉样式）
    assert sessions[0]["pinned"] is True

    listed = _group_sessions(history, limit=10)
    ids = [item["session_id"] for item in listed]
    assert "0" in ids, "整数 0 会话不能被跳过"
    assert "export" not in ids, "保留字会话不可打开，列表不应出现"
    print("✓ 会话列表归一旧 ID / 跳过保留字 / 容忍脏置顶条目")


def test_match_snippet_handles_whitespace_variants():
    """含全角 / 连续空格的查询命中原文后不能返回空片段。"""
    from webapp.server import _match_snippet

    assert _match_snippet("前文 a　b 后文", "a　b"), "全角空格查询必须返回片段"
    assert _match_snippet("前文 a   b 后文", "a   b"), "连续空格查询必须返回片段"
    print("✓ 搜索片段容忍空白差异")


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

    monkeypatch.setattr(server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _Store())
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


def test_sessions_listing_reuses_aggregation_for_repeat_requests(monkeypatch):
    """无查询词的列表刷新复用上次聚合；标题 / 条数变化必须失效重算。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_sessions_cache")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "聚合缓存", "cache-1")
            store.add_chat_message("assistant", "回复", "cache-1")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        first = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert first["total"] == 1

        original = server_module._group_sessions

        def _boom(*args, **kwargs):
            raise AssertionError("重复刷新不应重新聚合全量历史")

        monkeypatch.setattr(server_module, "_group_sessions", _boom)
        second = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert second == first, "缓存命中时响应必须与首次逐字段一致"

        # 标题变化改变指纹：必须重新聚合并反映新标题
        monkeypatch.setattr(server_module, "_group_sessions", original)
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        try:
            assert store.set_session_title("cache-1", "新标题")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        third = client.get("/api/sessions", params={"user_id": user_id}).json()
        assert third["sessions"][0]["title"] == "新标题"
        print("✓ 会话列表聚合缓存命中且元数据变化自动失效")
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
        assert data["sessions_max"] >= 1
        rate = data["rate_limit"]
        assert rate["backend"] in {"memory", "redis"}
        assert rate["configured"] is False
        assert rate["degraded"] is False
        assert rate["user_limit"] >= 1 and rate["ip_limit"] >= 1
        assert rate["kb_limit"] >= 1
        assert rate["kb_import_limit"] >= 60
        assert rate["weather_limit"] >= 1
        assert rate["read_limit"] >= 60
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


def test_content_length_413_metric_labels():
    """Content-Length 413（路由前拒绝）按已知接口归一标签；未知路径归 __unmatched__。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    try:
        for path in ("/api/chat", "/api/no-such-endpoint"):
            client.post(
                path,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "99999999",
                },
            )
        by_path = REQUEST_METRICS.snapshot()["by_path"]
        assert "/api/chat" in by_path, by_path
        assert "__unmatched__" in by_path, by_path
        from webapp.server import _metric_path_before_route

        assert (
            _metric_path_before_route("/api/trips/trip_1")
            == "/api/trips/{trip_id}"
        )
        assert _metric_path_before_route("/api/trips/123") == "__unmatched__"
        print("✓ Content-Length 413 指标标签按接口归一")
    finally:
        REQUEST_METRICS.reset()


def test_pre_route_metric_labels_cover_write_endpoints():
    """路由前 413 / 尾斜杠重定向的标签表覆盖真实接口，未知路径仍归 __unmatched__。"""
    from webapp.server import _metric_path_before_route

    assert (
        _metric_path_before_route("/api/sessions/s-1/title")
        == "/api/sessions/{session_id}/title"
    )
    assert (
        _metric_path_before_route("/api/sessions/s-1/pin")
        == "/api/sessions/{session_id}/pin"
    )
    assert (
        _metric_path_before_route("/api/sessions/s-1/archive")
        == "/api/sessions/{session_id}/archive"
    )
    assert (
        _metric_path_before_route("/api/preferences/hotel")
        == "/api/preferences/{pref_type}"
    )
    assert (
        _metric_path_before_route("/api/sessions/s-1")
        == "/api/sessions/{session_id}"
    )
    assert (
        _metric_path_before_route("/api/knowledge/duplicates/cleanup")
        == "/api/knowledge/duplicates/cleanup"
    )
    assert _metric_path_before_route("/api/evil/whatever") == "__unmatched__"
    print("✓ 路由前指标标签覆盖写接口")


def test_trailing_slash_redirect_metric_label():
    """尾斜杠 307 重定向按目标路由模板计数，而不是 __unmatched__。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = TestClient(
        create_app(model=UsageTrackingModel(_FakeModel())),
        follow_redirects=False,
    )
    try:
        response = client.get("/api/sessions/sess-redirect/")
        assert response.status_code == 307, response.text[:200]
        by_path = REQUEST_METRICS.snapshot()["by_path"]
        assert by_path.get("/api/sessions/{session_id}") == 1, by_path
        assert "__unmatched__" not in by_path, by_path
        print("✓ 尾斜杠 307 重定向按路由模板计数")
    finally:
        REQUEST_METRICS.reset()


def test_host_rejection_is_secured_and_counted():
    """Host 白名单 400 与其它响应一样带安全头，并进入访问日志 / 指标。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    client = _client()
    try:
        response = client.get("/api/health", headers={"Host": "evil.example.com"})
        assert response.status_code == 400, response.text[:200]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        # 扫描器直接打 / 也要计入（非 /api 路径平时不记访问日志）
        root = client.get("/", headers={"Host": "evil.example.com"})
        assert root.status_code == 400
        missing = client.get("/api/definitely-not-a-route")
        assert missing.status_code == 404
        # 方法不符（partial match，端点未执行）与尾斜杠重定向同样不受限流、
        # 可被无限触发：计入请求数，但不能进入延迟分位
        mismatch = client.post("/api/health")
        assert mismatch.status_code == 405
        redirect = client.get("/api/health/", follow_redirects=False)
        assert redirect.status_code in (307, 308)
        snapshot = REQUEST_METRICS.snapshot()
        assert snapshot["by_status"].get("400") == 2, snapshot
        assert snapshot["by_status"].get("404") == 1, snapshot
        assert snapshot["by_status"].get("405") == 1, snapshot
        assert snapshot["by_path"].get("__host_rejected__") == 2, snapshot
        assert snapshot["by_path"].get("__unmatched__") == 1, snapshot
        assert snapshot["duration_samples"] == 0, "探测流量不进入延迟分位"
        print("✓ Host 拒绝 400 计入指标且带安全头")
    finally:
        REQUEST_METRICS.reset()


def test_cancelled_request_recorded_as_499():
    """请求被取消（优雅关闭 / 客户端中止）记 499、不计耗时，且取消向上传播。"""
    from utils.metrics import REQUEST_METRICS
    from webapp.server import RequestContextMiddleware

    REQUEST_METRICS.reset()

    async def cancelled_app(scope, receive, send):
        raise asyncio.CancelledError()

    middleware = RequestContextMiddleware(cancelled_app)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "headers": [(b"host", b"127.0.0.1")],
    }

    async def call():
        try:
            await middleware(scope, receive, send)
        except asyncio.CancelledError:
            return "cancelled"
        return "returned"

    assert asyncio.run(call()) == "cancelled", "取消必须继续向上传播"
    snapshot = REQUEST_METRICS.snapshot()
    assert snapshot["by_status"].get("499") == 1, snapshot
    assert "0" not in snapshot["by_status"], snapshot
    assert snapshot["duration_samples"] == 0, snapshot
    assert sent == [], "响应未开始时取消不应发送任何消息"
    REQUEST_METRICS.reset()
    print("✓ 取消请求记 499 且不计耗时")


def test_client_disconnect_recorded_as_499():
    """客户端中途断开不是服务端故障：记 499、不上报 500、不打 ERROR。"""
    from starlette.requests import ClientDisconnect
    from utils.metrics import REQUEST_METRICS
    from webapp.server import RequestContextMiddleware

    REQUEST_METRICS.reset()

    async def disconnect_app(scope, receive, send):
        raise ClientDisconnect()

    middleware = RequestContextMiddleware(disconnect_app)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/import",
        "headers": [(b"host", b"127.0.0.1")],
    }

    asyncio.run(middleware(scope, receive, send))
    snapshot = REQUEST_METRICS.snapshot()
    assert snapshot["by_status"].get("499") == 1, snapshot
    assert "500" not in snapshot["by_status"], snapshot
    assert sent == []
    REQUEST_METRICS.reset()
    print("✓ 客户端断连记 499 而非 500")


def test_knowledge_write_rejects_nul_and_surrogates():
    """知识写入 schema 与服务端清洗同口径：NUL / 孤立代理项直接 422。"""
    client = _client()
    for bad in ("含\x00内容", "bad " + chr(0xD800)):
        body = json.dumps({"content": bad, "metadata": {}}, ensure_ascii=True)
        response = client.post(
            "/api/knowledge/documents",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text[:200]
    # 面板分类输入上限与后端元数据值上限一致（200 字）
    index_html = (Path(__file__).resolve().parents[1] / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert re.search(r'id="kbCategory"[^>]*maxlength="200"', index_html), (
        "分类输入框 maxlength 必须与后端 200 字上限一致"
    )
    print("✓ 知识写入拒绝 NUL / 孤立代理项，分类上限一致")


def test_kb_list_is_not_live_region():
    """知识列表承载可交互按钮，不能当实时区（整列表会被反复朗读）。

    摘要交给独立的隐藏节点 #kbListStatus 播报。
    """
    index_html = (
        Path(__file__).resolve().parents[1] / "webapp" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    kb_list = re.search(r'<div[^>]*id="kbList"[\s\S]*?>', index_html)
    assert kb_list, "缺少 #kbList"
    tag = kb_list.group(0)
    assert 'role="group"' in tag, tag
    assert "aria-live" not in tag, "列表本身不能再是实时区"
    assert re.search(r'id="kbListStatus"[^>]*role="status"', index_html), (
        "摘要播报节点必须带 role=status"
    )
    # 常驻 hidden 的节点不在无障碍树里，aria-live 播报完全不生效：
    # 必须用 .sr-only 视觉隐藏而不是 hidden 属性
    status_tag = re.search(r'<div[^>]*id="kbListStatus"[^>]*>', index_html).group(0)
    assert "hidden" not in status_tag, status_tag
    assert "sr-only" in status_tag, status_tag
    print("✓ 知识列表不是实时区，摘要独立播报")


def test_knowledge_collection_missing_gets_actionable_error(monkeypatch):
    """半初始化（集缺失）的 503 与 /api/knowledge、doctor 同口径，其它异常仍脱敏。"""
    from webapp import server as server_module

    def missing(*args, **kwargs):
        raise RuntimeError(
            "collection not found[collection=business_travel_knowledge]"
        )

    monkeypatch.setattr(server_module, "list_documents", missing)
    client = _client()
    response = client.get("/api/knowledge/documents")
    assert response.status_code == 503, response.text[:300]
    detail = response.json()["detail"]
    assert "集合不存在" in detail and "init_knowledge_base.py" in detail, detail
    assert "collection not found" not in detail, detail

    def boom(*args, **kwargs):
        raise RuntimeError(r"milvus write failed at C:\secret\path")

    monkeypatch.setattr(server_module, "get_document_group", boom)
    response = client.get("/api/knowledge/documents/1/group")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == "读取失败，请稍后再试", detail
    assert "secret" not in detail
    print("✓ 知识库集合缺失返回可操作提示，其它异常仍脱敏")


def test_health_marks_half_open_breaker_degraded(monkeypatch):
    """HALF_OPEN（仅单飞试探）同样无法正常对话：健康检查必须报 degraded。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app)
    state = app.state.aligo
    monkeypatch.setattr(
        state.breaker, "get_status", lambda: {"state": "half_open"}
    )
    data = client.get("/api/health").json()
    assert data["status"] == "degraded", data
    assert data["circuit_breaker"] == "half_open"
    print("✓ 健康检查把 HALF_OPEN 熔断视为 degraded")


def test_dedupe_endpoint_offloads_rate_limit(monkeypatch):
    """async 端点的限流检查必须走线程池：Redis 慢时不能阻塞事件循环。"""
    from webapp import server as server_module

    calls = []
    real_to_thread = server_module.asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(server_module.asyncio, "to_thread", tracking_to_thread)
    client = _client()
    user_id = _unique_user("web_test_dedupe_offload")
    try:
        response = client.post(
            "/api/trips/dedupe", json={"user_id": user_id, "dry_run": True}
        )
        assert response.status_code == 200, response.text[:300]
        assert "_enforce_read_rate_limit" in calls, calls
        print("✓ /api/trips/dedupe 的限流检查走线程池")
    finally:
        _cleanup(user_id)


def test_half_open_breaker_recovers_through_chat_endpoint():
    """半开期首个对话不能被端点预检占掉唯一试探名额（否则永久 503）。"""
    import time as time_module

    calls = {"n": 0}

    class _CountingModel:
        async def __call__(self, messages, **kwargs):
            calls["n"] += 1
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(_INTENT, ensure_ascii=False),
                    }
                ],
                "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.05},
            }

    app = create_app(model=UsageTrackingModel(_CountingModel()))
    state = app.state.aligo
    client = TestClient(app)
    user_id = _unique_user("web_test_halfopen")
    try:
        baseline = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "ho-0"},
        )
        assert baseline.status_code == 200, baseline.text[:300]
        assert calls["n"] == 1

        for _ in range(state.breaker.failure_threshold):
            state.breaker.record_failure()
        assert state.breaker.get_status()["state"] == "open"
        # 模拟恢复期已过（OPEN -> HALF_OPEN 在下次状态读取时发生）
        state.breaker._opened_at = time_module.monotonic() - 999

        first = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "ho-1"},
        )
        assert first.status_code == 200, first.text[:300]
        assert calls["n"] >= 2, "半开期首个对话必须真正调用模型"

        second = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "ho-2"},
        )
        assert second.status_code == 200, second.text[:300]
        assert state.breaker.get_status()["state"] == "closed", (
            "两次成功的半开试探后熔断器必须关闭"
        )
        print("✓ 半开熔断经对话端点可恢复（预检不占试探名额）")
    finally:
        _cleanup(user_id)


def test_trip_export_uses_strict_store(monkeypatch):
    """行程列表 / 导出 / 日历用 strict 打开：PG 故障期不能把回退 JSON 当成功。"""
    from webapp import server as server_module

    captured = []

    class _Store:
        def get_trip_history(self, limit=None):
            return []

        def close(self):
            pass

    def fake_open(user_id, strict=False, **kwargs):
        captured.append((user_id, strict))
        return _Store()

    monkeypatch.setattr(server_module, "_open_long_term", fake_open)
    client = _client()
    user_id = _unique_user("web_test_strict_export")
    assert (
        client.get("/api/trips/export", params={"user_id": user_id}).status_code
        == 200
    )
    # 列表读与状态 / 导出 / CLI 同一口径：故障期不回退 JSON
    assert (
        client.get("/api/trips", params={"user_id": user_id}).status_code == 200
    )
    assert (
        client.get(
            "/api/trips/trip_1/calendar", params={"user_id": user_id}
        ).status_code
        == 404
    )
    assert [strict for _, strict in captured] == [True, True, True], captured
    print("✓ 行程列表 / 导出 / 日历使用 strict 打开长期记忆")


def test_daemon_import_executor_is_not_joined_at_exit():
    """导入线程为 daemon 且不登记 atexit join：停服不会被分钟级导入卡住。"""
    from concurrent.futures.thread import _threads_queues
    from webapp.server import _DaemonImportExecutor

    executor = _DaemonImportExecutor(
        max_workers=1, thread_name_prefix="test-import"
    )
    try:
        assert executor.submit(lambda: 1).result(timeout=5) == 1
        threads = list(executor._threads)
        assert threads and all(thread.daemon for thread in threads)
        assert all(thread not in _threads_queues for thread in threads), (
            "登记到 _threads_queues 会让 atexit 逐个 join，导入中停服仍卡住"
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    print("✓ 导入执行器线程不参与 atexit join")


def test_preference_type_rejects_control_characters():
    """偏好类型会进 INFO 日志：%0A / %00 / %7F 必须被路由直接拒绝。"""
    client = _client()
    user_id = _unique_user("web_test_prefctl")
    try:
        for bad in ("x%0AFAKE", "x%00y", "x%7Fz", "x%E2%80%A8y"):
            response = client.put(
                f"/api/preferences/{bad}",
                params={"user_id": user_id},
                json={"user_id": user_id, "value": "v"},
            )
            assert response.status_code == 422, (bad, response.text[:200])
        print("✓ 偏好类型拒绝控制字符")
    finally:
        _cleanup(user_id)


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
        assert "aligo_sessions_max" in text
        assert "aligo_skills_ok 1" in text, "源码目录应有可加载技能"
        import re

        assert re.search(r"aligo_skills_count [1-9]\d*", text), text
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
    assert f"aligo_sessions_max {state.max_sessions}" in text
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
        lambda wait_for_create=True: {
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
    """TRUST_PROXY_HEADERS 开启后按 X-Forwarded-For 最后一跳地址限流。"""
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

        # 开启信任后：相同 XFF 共享 IP 桶，不同 XFF 互不影响。
        # 可信对端默认只有回环：TestClient 需以 127.0.0.1 作为对端地址，
        # 否则（"testclient" 非 IP）XFF 一律被忽略
        user_c = _unique_user("web_test_proxy_c")
        user_d = _unique_user("web_test_proxy_d")
        user_e = _unique_user("web_test_proxy_e")
        user_f = _unique_user("web_test_proxy_f")
        users += [user_c, user_d, user_e, user_f]
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_headers", True)
        client = TestClient(app, client=("127.0.0.1", 50000))
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
        # 客户端自带伪造前缀不能获得新桶：取最后一跳（代理追加的真实地址）
        sixth = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_f, "session_id": "proxy-f1"},
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.11, 10.0.0.1"},
        )
        assert sixth.status_code == 429, (
            "轮换 XFF 前缀不能绕过 IP 桶（实际 key 取最后一跳）"
        )

        # 代理以多条独立头行追加（RFC 7230：等价于按顺序逗号拼接）：
        # 只读第一行会把客户端自带的伪造值当成限流键
        user_g = _unique_user("web_test_proxy_g")
        user_h = _unique_user("web_test_proxy_h")
        users += [user_g, user_h]
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        seventh = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_g, "session_id": "proxy-g1"},
            headers=[
                ("X-Forwarded-For", "1.1.1.1"),
                ("X-Forwarded-For", "203.0.113.12"),
            ],
        )
        assert seventh.status_code == 200
        eighth = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_h, "session_id": "proxy-h1"},
            headers=[
                ("X-Forwarded-For", "2.2.2.2"),
                ("X-Forwarded-For", "203.0.113.12"),
            ],
        )
        assert eighth.status_code == 429, (
            "多条 XFF 头行须按逗号拼接取最后一跳，轮换第一行不能获得新桶"
        )
        print("✓ 可信代理开启后按 X-Forwarded-For 最后一跳地址限流")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_trusted_proxy_peer_and_hop_parsing(monkeypatch):
    """XFF 只信任可信对端；末跳支持 [IPv6]/IP:port，非法形式回退 TCP 对端。"""
    from config import RATE_LIMIT_CONFIG
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=100, window_sec=60)
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_headers", True)
    users = []
    try:
        def _chat(client, tag, xff):
            user = _unique_user(f"web_test_hop_{tag}")
            users.append(user)
            return client.post(
                "/api/chat",
                json={
                    "message": "你好",
                    "user_id": user,
                    "session_id": f"hop-{tag}",
                },
                headers={"X-Forwarded-For": xff},
            )

        # 非可信对端携带轮换 XFF：一律忽略，共用 TCP 对端桶
        untrusted = TestClient(app, client=("203.0.113.50", 60000))
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat(untrusted, "u1", "198.51.100.1").status_code == 200
        assert (
            _chat(untrusted, "u2", "198.51.100.2").status_code == 429
        ), "非可信对端的 XFF 不能被采信（否则任意客户端可自选限流身份）"

        # 显式把该对端加入可信网段后恢复按末跳分桶
        monkeypatch.setitem(
            RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "203.0.113.0/24"
        )
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat(untrusted, "u3", "198.51.100.7").status_code == 200
        assert (
            _chat(untrusted, "u4", "198.51.100.7").status_code == 429
        ), "可信对端的相同末跳共享桶"
        assert (
            _chat(untrusted, "u5", "198.51.100.8").status_code == 200
        ), "不同末跳互不影响"

        # 末跳带端口 / 方括号：端口剥离后与裸 IP 同桶
        trusted = TestClient(app, client=("127.0.0.1", 50000))
        monkeypatch.setitem(
            RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "127.0.0.1/32,::1/128"
        )
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat(trusted, "p1", "198.51.100.20:5000").status_code == 200
        assert (
            _chat(trusted, "p2", "198.51.100.20").status_code == 429
        ), "IP:port 应剥离端口后与裸 IP 共桶"

        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat(trusted, "p3", "[2001:db8::7]:443").status_code == 200
        assert (
            _chat(trusted, "p4", "2001:db8::7").status_code == 429
        ), "[IPv6]:port 应剥离端口后与裸 IPv6 共桶（/64 聚合）"

        # 非法末跳（不是裸 IP / [IPv6] / IP:port）：回退 TCP 对端桶，
        # 任意字符串不能各自成为限流桶
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat(trusted, "p5", "198.51.100.30:notaport").status_code == 200
        assert (
            _chat(trusted, "p6", "[zzz]:1").status_code == 429
        ), "两个非法末跳都应回退到同一 TCP 对端桶"
        # 合法末跳不会被拖进回退桶
        assert _chat(trusted, "p7", "198.51.100.31").status_code == 200
        print("✓ 可信代理对端与末跳解析（端口 / 非法回退）")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_ipv6_prefix_len_config_aggregates_delegated_blocks(monkeypatch):
    """RATE_IPV6_PREFIX_LEN 可按委派粒度收紧 IPv6 聚合前缀。"""
    from config import RATE_LIMIT_CONFIG
    from webapp import server as server_module

    monkeypatch.setitem(RATE_LIMIT_CONFIG, "ipv6_prefix_len", 56)
    first = server_module._rate_identity("2001:db8:aa:1::1")
    second = server_module._rate_identity("2001:db8:aa:2::1")
    other = server_module._rate_identity("2001:db8:bb:1::1")
    assert first == second, "同一 /56 的兄弟 /64 应归并为同一键"
    assert first != other
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "ipv6_prefix_len", 64)
    assert (
        server_module._rate_identity("2001:db8:aa:1::1")
        != server_module._rate_identity("2001:db8:aa:2::1")
    ), "默认仍是 /64"
    # 非法值回退 /64，不抛异常
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "ipv6_prefix_len", 12)
    assert server_module._rate_identity("2001:db8:aa:1::1") == "2001:db8:aa:1::/64"
    print("✓ IPv6 聚合前缀可配置（默认 /64 / 非法回退）")


def test_rate_limit_capacity_429_does_not_consume_ip_bucket():
    """用户桶键表满载：429 标注容量、发生在记录 IP 桶之前、信息量正确。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=5, window_sec=60, max_keys=2)
    state.ip_limiter = RateLimiter(limit=100, window_sec=60)
    client = TestClient(app, client=("127.0.0.1", 50000))
    users = []
    try:
        for index in range(2):
            user = _unique_user(f"web_test_capacity_{index}")
            users.append(user)
            ok = client.post(
                "/api/chat",
                json={
                    "message": "你好",
                    "user_id": user,
                    "session_id": f"capacity-{index}",
                },
            )
            assert ok.status_code == 200, ok.text
        before = state.ip_limiter.status("127.0.0.1")[0]
        third = _unique_user("web_test_capacity_x")
        users.append(third)
        blocked = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": third, "session_id": "capacity-x"},
        )
        assert blocked.status_code == 429, blocked.text
        detail = blocked.json()["detail"]
        assert "容量" in detail, detail
        assert "X-RateLimit-Remaining" in blocked.headers
        assert blocked.headers["X-RateLimit-Remaining"] == "0"
        after = state.ip_limiter.status("127.0.0.1")[0]
        assert after == before, "容量拒绝必须发生在记录 IP 桶之前（不连坐同 IP）"
        print("✓ 容量 429 不消耗 IP 桶且文案可区分")
    finally:
        for user_id in users:
            _cleanup(user_id)


def test_export_guard_prechecks_rate_limit_before_slots():
    """导出槽满 + 读额度已耗尽：预检 429 而非占槽后的 503。"""
    from utils.rate_limit import RateLimiter

    client = _client()
    state = client.app.state.aligo
    state.read_limiter = RateLimiter(limit=1, window_sec=60)
    state.kb_limiter = RateLimiter(limit=100, window_sec=60)
    user = _unique_user("web_test_export_precheck")
    try:
        assert state.read_limiter.check("testclient") is None
        held = [state.export_slots.acquire(blocking=False) for _ in range(2)]
        assert all(held)
        try:
            blocked = client.get(
                "/api/sessions/export", params={"user_id": user, "format": "md"}
            )
            assert blocked.status_code == 429, blocked.text[:200]
            assert "数据读取过于频繁" in blocked.json()["detail"]
        finally:
            for acquired in held:
                if acquired:
                    state.export_slots.release()
        print("✓ 导出槽满时已超限请求先吃 429（不占槽）")
    finally:
        _cleanup(user)


def test_kb_read_guard_prechecks_rate_limit_before_slots():
    """知识库读槽满 + KB 额度耗尽：预检 429 而非槽满 503。"""
    from utils.rate_limit import RateLimiter

    client = _client()
    state = client.app.state.aligo
    state.kb_limiter = RateLimiter(limit=1, window_sec=60)
    try:
        assert state.kb_limiter.check("testclient") is None
        held = [state.kb_read_slots.acquire(blocking=False) for _ in range(4)]
        assert all(held)
        try:
            blocked = client.get("/api/knowledge/documents")
            assert blocked.status_code == 429, blocked.text[:200]
            assert "知识库操作过于频繁" in blocked.json()["detail"]
        finally:
            for acquired in held:
                if acquired:
                    state.kb_read_slots.release()
        print("✓ 知识库读槽满时已超限请求先吃 429（不占槽）")
    finally:
        pass


def test_knowledge_eval_precheck_rate_limit_before_lock():
    """KB 额度耗尽时评测不触碰跨实例评测锁（429 为限流文案）。"""
    from utils.rate_limit import RateLimiter

    client = _client()
    state = client.app.state.aligo
    state.kb_limiter = RateLimiter(limit=1, window_sec=60)
    assert state.kb_limiter.check("testclient") is None
    token = state.eval_lock.acquire()
    assert token is not None, "测试应能占住评测锁"
    try:
        blocked = client.get("/api/knowledge/eval")
        assert blocked.status_code == 429, blocked.text[:200]
        detail = blocked.json()["detail"]
        assert "知识库操作过于频繁" in detail, detail
        assert "已有检索评测在运行" not in detail
        assert blocked.headers.get("X-RateLimit-Remaining") == "0"
    finally:
        state.eval_lock.release(token)
    print("✓ 评测超限请求不触达评测锁")


def test_export_with_knowledge_charges_kb_bucket(monkeypatch):
    """with_knowledge 导出同时占用 KB 桶：KB 额度耗尽时直接 429。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    state.kb_limiter = RateLimiter(limit=1, window_sec=60)
    state.read_limiter = RateLimiter(limit=1000, window_sec=60)
    user = _unique_user("web_export_kb_bucket")
    try:
        monkeypatch.setattr(
            server_module, "export_documents", lambda: []
        )
        first = client.get(
            "/api/export", params={"user_id": user, "with_knowledge": "1"}
        )
        assert first.status_code == 200, first.text[:200]
        second = client.get(
            "/api/export", params={"user_id": user, "with_knowledge": "1"}
        )
        assert second.status_code == 429, second.text[:200]
        assert "知识库导出操作过于频繁" in second.json()["detail"]
        # 不计 KB 的普通导出不受 KB 额度影响
        plain = client.get("/api/export", params={"user_id": user})
        assert plain.status_code == 200, plain.text[:200]
        print("✓ with_knowledge 导出计入知识库桶")
    finally:
        _cleanup(user)


def test_rate_limit_probe_budget_bounds_health_latency(monkeypatch):
    """健康探测受总预算约束：慢桶不会在单次请求内逐个叠加。"""
    from webapp import server as server_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(server_module.time, "monotonic", lambda: clock["now"])
    probes: list[int] = []

    class _SlowHealthy:
        degraded = False
        backend = "redis"
        limit = 10
        window_sec = 60.0

        def probe(self):
            probes.append(1)
            clock["now"] += 0.9
            return True

    class _State:
        rate_limiter = _SlowHealthy()
        ip_limiter = _SlowHealthy()
        kb_limiter = _SlowHealthy()
        weather_limiter = _SlowHealthy()
        import_limiter = _SlowHealthy()
        read_limiter = _SlowHealthy()
        write_limiter = _SlowHealthy()

    info = server_module._rate_limit_info(_State())
    assert info["degraded"] is False
    assert len(probes) == 1, "预算内只做一次真实探测（其余沿用当前状态）"
    assert clock["now"] - 1000.0 <= server_module.RATE_LIMIT_PROBE_BUDGET_SEC
    print("✓ 健康探测受总预算约束")


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


def test_breaker_open_does_not_consume_rate_limit():
    """熔断打开的 503 不消耗用户 / IP 限流额度（恢复后重试不被 429 锁住）。"""
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    for _ in range(state.breaker.failure_threshold):
        state.breaker.record_failure()
    state.rate_limiter = RateLimiter(limit=5, window_sec=60)
    state.ip_limiter = RateLimiter(limit=5, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_breaker_quota")
    try:
        for path in ("/api/chat", "/api/chat/stream"):
            response = client.post(
                path,
                json={
                    "message": "你好",
                    "user_id": user_id,
                    "session_id": "bq-1",
                },
            )
            assert response.status_code == 503, (path, response.text[:200])
            # 熔断拒绝带退避提示：与用户锁 / 预算耗尽等瞬时 503 同口径
            assert response.headers.get("Retry-After") == "5", (
                path,
                dict(response.headers),
            )
        remaining, _ = state.rate_limiter.status(f"testclient:{user_id}")
        assert remaining == 5, f"熔断拒绝消耗了用户桶额度（剩 {remaining}）"
        ip_remaining, _ = state.ip_limiter.status("testclient")
        assert ip_remaining == 5, f"熔断拒绝消耗了 IP 桶额度（剩 {ip_remaining}）"
        print("✓ 熔断打开的拒绝不消耗限流额度")
    finally:
        _cleanup(user_id)


def test_half_open_busy_probe_denies_before_rate_limit(monkeypatch):
    """半开唯一试探名额被长回合占用时，并发对话在扣限流额度前就被 503。"""
    import asyncio

    import httpx

    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    started = threading.Event()
    release = threading.Event()

    class _GateModel:
        async def __call__(self, messages, **kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(_INTENT, ensure_ascii=False),
                    }
                ],
                "usage": {"input_tokens": 6, "output_tokens": 4, "time": 0.05},
            }

    app = create_app(model=UsageTrackingModel(_GateModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=5, window_sec=60)
    state.ip_limiter = RateLimiter(limit=5, window_sec=60)
    for _ in range(state.breaker.failure_threshold):
        state.breaker.record_failure()
    state.breaker._opened_at = time.monotonic() - 999  # 进入半开

    rate_calls = {"n": 0}
    real = server_module._enforce_rate_limit

    def counting(*args, **kwargs):
        rate_calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(server_module, "_enforce_rate_limit", counting)
    user_a = _unique_user("web_test_halfopen_busy")
    user_b = _unique_user("web_test_halfopen_wait")

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:

            async def first():
                return await client.post(
                    "/api/chat",
                    json={
                        "message": "你好",
                        "user_id": user_a,
                        "session_id": "hb-1",
                    },
                )

            first_task = asyncio.create_task(first())
            assert await asyncio.to_thread(started.wait, 10), "首个对话未进入模型调用"
            try:
                before = rate_calls["n"]
                second = await client.post(
                    "/api/chat",
                    json={
                        "message": "你好",
                        "user_id": user_b,
                        "session_id": "hb-2",
                    },
                )
                assert second.status_code == 503, second.text[:300]
                assert rate_calls["n"] == before, "半开拒绝不应扣限流额度"
            finally:
                release.set()
                first_response = await asyncio.wait_for(first_task, 20)
                assert first_response.status_code == 200, first_response.text[:300]

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _cleanup(user_a)
        _cleanup(user_b)
    print("✓ 半开名额被占时预检在限流之前拒绝")


def _emulate_epoch_bump(fake, script, args):
    """bump-epoch Lua 的测试桩：脚本含 INCR 时按 INCR + EXPIRE 模拟。

    返回 None 表示不是纪元脚本，调用方的 eval 继续按各自逻辑处理。
    """
    if "INCR" not in script:
        return None
    key = args[0]
    value = int(fake.data.get(key, "0")) + 1
    fake.data[key] = str(value)
    ttls = getattr(fake, "ttls", None)
    if ttls is not None:
        ttls[key] = int(args[1])
    return value


def test_pending_tombstone_replay_after_redis_recovers():
    """Redis 故障期删除：恢复后补写共享墓碑与会话纪元；回滚则撤销待补写。"""

    class _FlakyRedis:
        def __init__(self):
            self.up = False
            self.data = {}
            self.ttls = {}

        def _check(self):
            if not self.up:
                raise ConnectionError("redis down")

        def set(self, key, value, nx=False, ex=None):
            self._check()
            if nx and key in self.data:
                return None
            self.data[key] = value
            self.ttls[key] = ex
            return True

        def incr(self, key):
            self._check()
            value = int(self.data.get(key, "0")) + 1
            self.data[key] = str(value)
            return value

        def expire(self, key, ttl):
            self.ttls[key] = ttl
            return 1

        def exists(self, key):
            self._check()
            return int(key in self.data)

        def delete(self, key):
            self._check()
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            self._check()
            bumped = _emulate_epoch_bump(self, script, args)
            return 0 if bumped is None else bumped

    shared = _FlakyRedis()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state._redis_client = shared
    state._tombstones_shared = False

    token = state.mark_deleted("u-replay", "s-replay")
    assert "aligo:deleted:u-replay:s-replay" not in shared.data
    assert ("u-replay", "s-replay") in state._pending_shared_tombstones

    shared.up = True
    assert state.replay_pending_tombstones() >= 1
    assert shared.data["aligo:deleted:u-replay:s-replay"] == token
    assert "aligo:session-epoch:u-replay:s-replay" in shared.data, (
        "补写时必须同时补会话纪元（落库守卫依赖它）"
    )
    assert ("u-replay", "s-replay") not in state._pending_shared_tombstones

    # 删除被回滚：待补写条目一并撤销，恢复后不能补出「已撤销」的墓碑
    shared.up = False
    token2 = state.mark_deleted("u-replay2", "s-replay2")
    state.unmark_deleted("u-replay2", "s-replay2", token2)
    assert ("u-replay2", "s-replay2") not in state._pending_shared_tombstones
    shared.up = True
    state.replay_pending_tombstones()
    assert "aligo:deleted:u-replay2:s-replay2" not in shared.data
    print("✓ 故障期删除的共享墓碑 / 纪元在恢复后补写（回滚则撤销）")


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

        def incr(self, key):
            value = int(self.data.get(key, "0")) + 1
            self.data[key] = str(value)
            return value

        def expire(self, key, ttl):
            self.ttls[key] = ttl
            return 1

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            bumped = _emulate_epoch_bump(self, script, args)
            if bumped is not None:
                return bumped
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
    # 删除同时递增会话纪元（单调）：撤销墓碑后，删除前启动的在途回合
    # 仍会因纪元变化在落库守卫处被否决
    assert shared.data["aligo:session-epoch:u-shared:s-shared"] == "1"
    assert state_b.is_recently_deleted("u-shared", "s-shared") is True
    assert state_b.is_recently_deleted("u-shared", "other") is False

    state_a.unmark_deleted("u-shared", "s-shared")
    assert state_b.is_recently_deleted("u-shared", "s-shared") is False
    assert (
        shared.data["aligo:session-epoch:u-shared:s-shared"] == "1"
    ), "撤销墓碑不能回退会话纪元"
    print("✓ 删除墓碑通过 Redis 跨实例共享（含 TTL 与撤销）")


def test_mark_deleted_force_during_tombstone_cooldown():
    """墓碑失败冷却期内执行删除仍强制写共享墓碑与会话纪元（不漏保护）。"""
    import time as time_module

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

        def incr(self, key):
            value = int(self.data.get(key, "0")) + 1
            self.data[key] = str(value)
            return value

        def expire(self, key, ttl):
            self.ttls[key] = ttl
            return 1

        def get(self, key):
            return self.data.get(key)

        def eval(self, script, numkeys, *args):
            return _emulate_epoch_bump(self, script, args)

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    shared = _FakeRedis()
    state._redis_client = shared
    state._tombstones_shared = False
    # 模拟一次墓碑读取失败后的 60 秒重试冷却
    state._tombstone_retry_at = time_module.monotonic() + 60
    state.mark_deleted("u-force", "s-force")
    assert "aligo:deleted:u-force:s-force" in shared.data, (
        "关键路径必须绕过冷却真实写入共享墓碑"
    )
    assert shared.data["aligo:session-epoch:u-force:s-force"] == "1"
    print("✓ 删除会话在墓碑冷却期内仍强制写共享墓碑与纪元")


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


def test_successful_delete_reasserts_shared_tombstone():
    """并发删除：成功删除方覆盖墓碑为自身 token，原所有者回滚不误删。"""

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

        def get(self, key):
            return self.data.get(key)

        def exists(self, key):
            return int(key in self.data)

        def delete(self, key):
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            bumped = _emulate_epoch_bump(self, script, args)
            if bumped is not None:
                return bumped
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

    key = "aligo:deleted:u-re:s-re"
    token_a = state_a.mark_deleted("u-re", "s-re")
    token_b = state_b.mark_deleted("u-re", "s-re")
    assert shared.data[key] == token_a, "先到者是墓碑所有者"

    # B 的删除实际成功（A 的删除检查到「已不存在」并回滚）：成功方在删除后
    # 重新主张墓碑，覆盖为自己的 token
    state_b.reassert_deleted("u-re", "s-re", token_b)
    assert shared.data[key] == token_b

    # A 的删除失败回滚按旧 token 匹配：不能删掉 B 的墓碑
    state_a.unmark_deleted("u-re", "s-re", token_a)
    assert shared.data.get(key) == token_b, "已删除会话必须保留共享删除保护"
    assert state_b.is_recently_deleted("u-re", "s-re") is True
    print("✓ 并发删除的成功方重新主张共享墓碑")


def test_pending_replay_skips_rolled_back_delete():
    """补写与撤销交错：已回滚的删除不能被恢复后的补写固化成真实墓碑。"""

    class _HookRedis:
        def __init__(self):
            self.data = {}
            self.up = False
            self.on_set = None

        def _check(self):
            if not self.up:
                raise ConnectionError("redis down")

        def set(self, key, value, nx=False, ex=None):
            self._check()
            hook, self.on_set = self.on_set, None
            if hook is not None:
                hook()
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def exists(self, key):
            self._check()
            return int(key in self.data)

        def delete(self, key):
            self._check()
            self.data.pop(key, None)
            return 1

        def eval(self, script, numkeys, *args):
            self._check()
            bumped = _emulate_epoch_bump(self, script, args)
            if bumped is not None:
                return bumped
            key, expected = args[0], args[1]
            if self.data.get(key) == expected:
                self.data.pop(key, None)
                return 1
            return 0

    shared = _HookRedis()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state._redis_client = shared
    state._tombstones_shared = False

    token = state.mark_deleted("u-race", "s-race")
    assert ("u-race", "s-race") in state._pending_shared_tombstones

    shared.up = True
    # 补写的 SET 执行期间删除被回滚（unmark 撤销待补写条目并删共享墓碑）
    shared.on_set = lambda: state.unmark_deleted("u-race", "s-race", token)
    assert state.replay_pending_tombstones() == 0
    assert "aligo:deleted:u-race:s-race" not in shared.data, (
        "已回滚的删除不能被补写固化成真实墓碑"
    )
    assert state.is_recently_deleted("u-race", "s-race") is False
    print("✓ 补写前复查 + 写后补偿：回滚的删除不被固化")


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
    monkeypatch.setattr(server_module, "get_shared_redis_client", lambda force=False: client)

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


def test_tombstone_eviction_is_per_user_fair():
    """容量淘汰按用户公平：一个用户的删除量不能挤掉另一用户仍有效的墓碑。

    墓碑被挤出后，迟到落库 / 导入恢复可能把已删除的会话写回。
    """
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_tombstones = 3
    try:
        state.mark_deleted("bob", "b1")
        state.mark_deleted("alice", "a1")
        state.mark_deleted("alice", "a2")
        state.mark_deleted("alice", "a3")  # alice 3 条超限：淘汰 alice 最旧
        assert state.is_recently_deleted("bob", "b1") is True
        assert state.is_recently_deleted("alice", "a2") is True
        assert state.is_recently_deleted("alice", "a3") is True
        assert len(state._deleted_sessions) <= state.max_tombstones
    finally:
        state.close_all()
    print("✓ 墓碑容量按用户公平淘汰")


def test_pending_shared_tombstone_eviction_is_fair():
    """故障期待补写表同样按用户公平淘汰。"""
    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_tombstones = 3
    try:
        with state._tombstone_lock:
            state._pending_shared_tombstones.clear()
        state._remember_pending_tombstone("bob", "b1", "t-b")
        state._remember_pending_tombstone("alice", "a1", "t-a1")
        state._remember_pending_tombstone("alice", "a2", "t-a2")
        state._remember_pending_tombstone("alice", "a3", "t-a3")
        pending = state._pending_shared_tombstones
        assert ("bob", "b1") in pending
        assert ("alice", "a1") not in pending
        assert ("alice", "a3") in pending
        assert len(pending) <= state.max_tombstones
    finally:
        state.close_all()
    print("✓ 待补写墓碑按用户公平淘汰")


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
        server_module, "_open_long_term", lambda user_id, strict=False, **kwargs: _BrokenStore()
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
    held = []  # 弱引用锁表：测试必须自己持有强引用模拟「在用」
    try:
        for index in range(2):
            lock = asyncio.Lock()
            # 模拟锁正在被持有：同步测试里没有可直接 acquire 的事件循环
            setattr(lock, "_locked", True)
            held.append(lock)
            state.locks[f"held-{index}"] = lock
        assert state.lock_for("brand-new-user") is state.overflow_lock
        assert len(state.locks) == 2, "锁表占满时不能再为每个新用户建锁"
        assert state.lock_for("yet-another-user") is state.overflow_lock
        # 有锁释放（且不再被任何请求引用）后可以重新入表
        setattr(held[0], "_locked", False)
        held.pop(0)
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


def test_lock_for_borrowed_lock_not_evicted():
    """已借出但尚未 acquire 的锁不能被表满淘汰：否则同一用户会分裂出两把锁。"""
    import asyncio

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    original = state.max_locks
    state.max_locks = 1
    try:
        borrowed = state.lock_for("borrower")
        filler = asyncio.Lock()
        setattr(filler, "_locked", True)
        state.locks["filler"] = filler
        state.lock_for("newcomer")  # 表满：不能让这里淘汰已借出的 borrowed
        assert state.lock_for("borrower") is borrowed, (
            "已借出的锁被淘汰后同一用户会拿到另一把锁并行执行"
        )
        assert "borrower" in state.locks
    finally:
        state.max_locks = original
        state.locks.clear()
    print("✓ 已借出的用户锁不被表满淘汰")


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
        del blocker  # 弱引用锁表：不再被请求引用后自动回收
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


def test_cancelled_session_open_releases_late_turn():
    """等待会话构造期间被取消：迟到完成的线程必须补 end_turn。

    asyncio.to_thread 不受协程取消影响；不补释放的话会话占用会钉住到
    1h 租约兜底，累计到并发上限后新会话全部 503。
    """
    import asyncio
    import threading

    from webapp.server import _get_session_marked

    class _Session:
        def __init__(self):
            self.ended = 0

        def end_turn(self):
            self.ended += 1

    class _State:
        def __init__(self):
            self.gate = threading.Event()
            self.session = _Session()
            self.calls = 0

        def get_session(self, user_id, session_id, mark_in_use=False):
            self.calls += 1
            assert mark_in_use is True
            self.gate.wait(5)
            return self.session

    async def scenario():
        state = _State()
        task = asyncio.create_task(_get_session_marked(state, "u1", "s1"))
        while state.calls == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state.gate.set()
        for _ in range(200):
            if state.session.ended == 1:
                return state
            await asyncio.sleep(0.01)
        raise AssertionError("迟到完成的会话占用未释放（会钉住到 1h 租约）")

    state = asyncio.run(scenario())
    assert state.session.ended == 1
    print("✓ 取消会话构造后迟到占用被释放")


def test_session_open_prefers_dedicated_build_executor():
    """会话构造走独立有界线程池：构造会整份解析快照 / 建 PG-Redis 连接，
    突发新会话不能占满 asyncio 默认池、把在途回合的记忆 to_thread 拖成排队。"""
    import asyncio
    import threading

    from utils.daemon_executor import DaemonThreadPoolExecutor
    from webapp.server import _get_session_marked

    class _Session:
        def end_turn(self):
            pass

    class _State:
        def __init__(self):
            self.session = _Session()
            self.threads = []
            self.session_build_executor = DaemonThreadPoolExecutor(
                max_workers=1, thread_name_prefix="aligo-session-build"
            )

        def get_session(self, user_id, session_id, mark_in_use=False):
            self.threads.append(threading.current_thread().name)
            return self.session

    async def scenario():
        state = _State()
        try:
            session = await _get_session_marked(state, "u1", "s1")
        finally:
            state.session_build_executor.shutdown(wait=True)
        assert session is state.session
        assert state.threads, "会话构造没有被执行"
        assert state.threads[0].startswith("aligo-session-build"), state.threads

    asyncio.run(scenario())
    print("✓ 会话构造走专用线程池")


def test_session_lru_skips_in_use_sessions():
    """LRU 淘汰不能关闭正在对话的会话；全部在用时不新建（容量拒绝）。"""
    from webapp.server import SessionCapacityError

    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_sessions = 2
    closed = []

    busy = state.get_session("u-lru", "busy", True)
    busy.close = lambda: closed.append(busy)  # 实例属性覆盖 close

    idle = state.get_session("u-lru", "idle")
    assert len(state.sessions) == 2
    assert idle not in closed and busy not in closed, "本次返回的会话也不能被淘汰"

    # 空闲会话可被淘汰给新会话腾位
    third = state.get_session("u-lru", "third", True)
    assert state._session_key("u-lru", "idle") not in state.sessions, (
        "最久未用的空闲会话应被淘汰"
    )
    assert len(state.sessions) == 2
    assert busy not in closed, "在用会话不能被淘汰"

    # 全部会话都在用：不再无限新建（每个会话常驻一条数据库连接），
    # 本次请求收到可重试的容量错误
    with pytest.raises(SessionCapacityError):
        state.get_session("u-lru", "fourth", True)
    assert len(state.sessions) == 2

    # 占用标记泄漏（如 SSE 响应未启动就断开）：超过上限时长后允许回收
    busy._turn_started_at -= state.session_turn_leak_sec + 1
    state.get_session("u-lru", "fourth", True)
    assert state._session_key("u-lru", "busy") not in state.sessions
    assert busy in closed, "泄漏占用的会话被回收时同样要 close"
    third.end_turn()
    print("✓ 会话 LRU 跳过在用会话 / 全部在用容量拒绝 / 泄漏回收")


def test_session_capacity_postcheck_rejects_after_construction(monkeypatch):
    """预检放行后构造期间槽位被并发占满：复检拒绝、缓存不污染并回收候选。"""
    from webapp import server as server_module
    from webapp.server import SessionCapacityError

    state = create_app(model=UsageTrackingModel(_FakeModel())).state.aligo
    state.max_sessions = 2
    busy = state.get_session("u-post", "busy", True)
    idle = state.get_session("u-post", "idle")
    closed = []

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            # 模拟构造期间最后一个空闲槽被其它并发请求占用
            idle.begin_turn()

        def close(self):
            closed.append(self)

    with monkeypatch.context() as patch:
        patch.setattr(server_module, "_WebSession", _FakeSession)
        with pytest.raises(SessionCapacityError):
            state.get_session("u-post", "s-new", True)
    assert idle.in_use() and busy.in_use()
    assert state._session_key("u-post", "s-new") not in state.sessions
    assert len(state.sessions) == 2, "拒绝时缓存不能被候选污染"
    assert len(closed) == 1, "构造后的候选会话必须被关闭回收"

    idle.end_turn()
    busy.end_turn()
    # 释放占用后正常新建（构建槽没有泄漏）
    fresh = state.get_session("u-post", "s-new", True)
    assert fresh is not None
    fresh.end_turn()
    print("✓ 会话容量复检拒绝并回收候选")


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
    assert response.headers.get("connection") == "close", (
        "413 未读请求体：必须关闭连接，避免 keep-alive 把后续请求吞进 body"
    )
    print("✓ 超大请求体返回 413（带安全头 / 请求 ID / 关闭连接）")


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
    assert response.headers.get("connection") == "close", (
        "分块中止后必须关闭连接，避免剩余 body 被当成下一个请求"
    )
    print("✓ 分块请求体超过 2 MB 同样返回 413（并关闭连接）")


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


def test_chunked_import_body_too_large(monkeypatch):
    """分块传输同样受 /api/import 的 64MB 上限约束。

    /api/import 不套中间件的读体超时（端点自带 300 秒预算），但字节计数
    必须照常执行：此前超时与计数被写成同一分支，分块上传可绕过上限把
    任意大小的请求体读入内存。这里把上限缩到 1KB 验证。
    """
    from webapp import server as server_module

    monkeypatch.setitem(
        server_module._BODY_LIMIT_OVERRIDES, "/api/import", 1024
    )
    client = _client()
    response = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_import_chunked")},
        content=[b"x" * 2048],
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in response.request.headers, "用例应走分块传输"
    assert response.status_code == 413, response.text[:300]
    assert "请求体过大" in response.json()["detail"]
    assert response.headers.get("connection") == "close"
    print("✓ /api/import 的分块请求体同样按实际读取量截断")


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
    previous = os.environ.get("ALIGO_ENABLE_DOCS")
    os.environ["ALIGO_ENABLE_DOCS"] = "1"
    try:
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
    finally:
        if previous is None:
            os.environ.pop("ALIGO_ENABLE_DOCS", None)
        else:
            os.environ["ALIGO_ENABLE_DOCS"] = previous
    print("✓ 文档页面放宽 CSP，普通页面保持严格策略")


def test_docs_csp_only_on_exact_document_routes(monkeypatch):
    """文档开启时仅真实文档路由放宽 CSP：未命中的 /docs/* 404 保持严格。"""
    previous = os.environ.get("ALIGO_ENABLE_DOCS")
    os.environ["ALIGO_ENABLE_DOCS"] = "1"
    try:
        client = _client()
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            assert "cdn.jsdelivr.net" in response.headers.get(
                "content-security-policy", ""
            ), path
        for path in ("/docs/x", "/redoc/x"):
            response = client.get(path)
            assert response.status_code == 404, (path, response.status_code)
            csp = response.headers.get("content-security-policy", "")
            assert "cdn.jsdelivr.net" not in csp, path
            assert "script-src 'self'" in csp, path
    finally:
        if previous is None:
            os.environ.pop("ALIGO_ENABLE_DOCS", None)
        else:
            os.environ["ALIGO_ENABLE_DOCS"] = previous
    print("✓ 仅真实文档路由放宽 CSP（/docs/* 404 严格）")


def test_docs_env_parsing_matches_bool_switch(monkeypatch):
    """ALIGO_ENABLE_DOCS 与其它布尔开关同口径：引号 / BOM / true / 空格。"""
    bom_one = chr(0xFEFF) + "1"
    for value in ('"1"', bom_one, "true", " 1 "):
        monkeypatch.setenv("ALIGO_ENABLE_DOCS", value)
        assert _client().get("/docs").status_code == 200, value
    monkeypatch.setenv("ALIGO_ENABLE_DOCS", "enabled")
    assert _client().get("/docs").status_code == 404
    print("✓ ALIGO_ENABLE_DOCS 解析与布尔开关同口径")


def test_security_headers_deduplicate_inner_values():
    """内层已设置受管安全头时，出口只保留中间件的值（不出现重复冲突头）。"""
    from webapp.server import RequestContextMiddleware

    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"x-request-id", b"inner-id"),
                    (b"content-type", b"text/plain"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestContextMiddleware(inner)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.sleep(0)
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"127.0.0.1")],
    }
    asyncio.run(middleware(scope, receive, send))
    headers: dict[bytes, list[bytes]] = {}
    for key, value in sent[0]["headers"]:
        headers.setdefault(key.lower(), []).append(value)
    assert headers[b"x-frame-options"] == [b"DENY"]
    assert len(headers[b"x-request-id"]) == 1
    assert headers[b"x-request-id"][0] != b"inner-id"
    assert len(headers[b"content-security-policy"]) == 1
    print("✓ 受管安全头去重（内层同名头被中间件覆盖）")


def test_docs_disabled_by_default():
    """/docs 与 /openapi.json 默认关闭：页面与 schema 都是信息泄露面。"""
    previous = os.environ.pop("ALIGO_ENABLE_DOCS", None)
    try:
        client = _client()
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        # 首页与 API 不受影响
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
    finally:
        if previous is not None:
            os.environ["ALIGO_ENABLE_DOCS"] = previous
    print("✓ API 文档默认关闭")


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

        # 预览：计数与真实导入一致，且不写库
        preview = client.post(
            "/api/import",
            params={"user_id": target, "dry_run": 1},
            json={"backup": backup},
        ).json()
        assert preview["dry_run"] is True
        assert preview["messages_added"] == 2 and preview["trips_created"] == 1
        assert client.get("/api/sessions", params={"user_id": target}).json()["sessions"] == []

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

    def fake_update(doc_id, content, metadata, **kwargs):
        updated["call"] = (doc_id, content, metadata)
        return {"status": "success", "id": doc_id}

    monkeypatch.setattr(server_module, "update_document", fake_update)

    def fake_get_document(doc_id):
        if doc_id != 7:
            return None
        return {
            "id": doc_id,
            "content": "旧内容",
            "metadata": {"category": "交通", "title": "旧标题", "part": "1"},
        }

    monkeypatch.setattr(server_module, "get_document", fake_get_document)
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
    # 省略 metadata：沿用库中现有元数据（与 CLI kb-update 一致），不能清空
    assert updated["call"][2] == {
        "category": "交通",
        "title": "旧标题",
        "part": "1",
    }, updated["call"][2]
    # 显式提交空字典仍按提交值覆盖（清空用户元数据）
    cleared = client.put(
        "/api/knowledge/documents/7", json={"content": "x", "metadata": {}}
    )
    assert cleared.status_code == 200
    assert updated["call"][2] == {}
    # 片段不存在：省略 metadata 时的读取阶段直接 404
    assert (
        client.put("/api/knowledge/documents/8", json={"content": "x"}).status_code
        == 404
    )
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
    # 11 个用户键在 PUT schema 层不再 422：历史遗留的 >10 键记录（#553
    # 之前旧 CLI 可写出）必须能原样保存，键数上限由 knowledge_admin 按
    # 「库内已有键集」判断（新增键超限仍拒绝，见知识管理测试）
    too_many_keys = {f"k{index}": "v" for index in range(11)}
    eleven_keys = client.put(
        "/api/knowledge/documents/7",
        json={"content": "x", "metadata": too_many_keys},
    )
    assert eleven_keys.status_code == 200
    # 新增接口保持严格的 10 键上限
    assert (
        client.post(
            "/api/knowledge/documents",
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

        # 复用同一额度桶的端点：429 文案必须如实说明被限操作
        doctor = client.get("/api/doctor")
        assert doctor.status_code == 429, doctor.text
        assert "自检操作过于频繁" in doctor.json()["detail"], doctor.text
        restore = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {}}},
        )
        assert restore.status_code == 429, restore.text
        restore_detail = restore.json()["detail"]
        assert "备份恢复操作过于频繁" in restore_detail, restore_detail
        assert "知识库操作" not in restore_detail, restore_detail

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
    from utils.rag_eval import EVAL_CASES
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

    def fake_evaluate(search, **kwargs):
        search("测试", 5)
        return {
            "total": len(EVAL_CASES),
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
        lambda **_: {
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
    assert data["total"] == len(EVAL_CASES)
    assert data["hit5"] == 1.0
    assert "x-ratelimit-limit" in response.headers
    assert runs == [True], "评测必须用 strict=True 区分空结果与检索失败"

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda **_: {"available": False, "reason": "知识库未初始化", "path": "p"},
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
        lambda **_: {
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

    # 收尾只读复核发现锁已失效（心跳间隔内被主从切换丢失）：结果同样打标
    class _StaleEvalLock:
        lost = False
        degraded = False

        def acquire(self):
            return "token"

        def release(self, token):
            pass

        def verify_owned(self, token):
            self.lost = True
            return False

    state.eval_lock = _StaleEvalLock()
    monkeypatch.setitem(server_module.STORAGE_CONFIG, "redis_url", "")
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    stale = client.get("/api/knowledge/eval")
    assert stale.status_code == 200
    assert "共享锁不可用" in stale.json().get("warning", "")

    # 零命中诊断与共享锁告警必须同时保留（此前后写者覆盖先写者）
    def zero_hit_evaluate(*args, **kwargs):
        return {
            "total": 1,
            "hit1": 0.0,
            "hit3": 0.0,
            "hit5": 0.0,
            "mrr": 0.0,
            "rows": [],
            "warning": "没有命中任何评测题：请先确认知识库已初始化且片段数量正常",
        }

    monkeypatch.setattr("utils.rag_eval.evaluate_retrieval", zero_hit_evaluate)
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    merged = client.get("/api/knowledge/eval")
    assert merged.status_code == 200
    merged_warning = merged.json().get("warning", "")
    assert "没有命中任何评测题" in merged_warning
    assert "共享锁不可用" in merged_warning
    # 恢复命中结果的假实现，后续「未配置 Redis 不应误报」用例仍按原口径
    monkeypatch.setattr("utils.rag_eval.evaluate_retrieval", fake_evaluate)

    # 未配置 Redis 时进程内锁是正常模式，不应误报
    monkeypatch.setitem(server_module.STORAGE_CONFIG, "redis_url", "")
    state.eval_lock = _LostEvalLock()
    state.eval_lock.lost = False
    state.kb_limiter = RateLimiter(limit=3, window_sec=60)
    single = client.get("/api/knowledge/eval")
    assert single.status_code == 200 and "warning" not in single.json()
    print("✓ /api/knowledge/eval 检索评测（结果 / 503 / 额度头 / 并发 429 不耗额度）")


def test_knowledge_list_sanitizes_legacy_surrogates(monkeypatch):
    """历史脏数据里的孤立代理项在列表 / 分类响应里清洗，不再整条 500。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    bad = "坏" + chr(0xD800) + "片段"
    monkeypatch.setattr(
        server_module,
        "list_documents",
        lambda **kwargs: {
            "total": 1,
            "items": [{"id": 1, "content": bad, "metadata": {"title": bad}}],
        },
    )
    monkeypatch.setattr(
        server_module, "list_categories", lambda: [{"category": bad, "count": 1}]
    )
    client = _client()
    client.app.state.aligo.kb_limiter = RateLimiter(limit=10, window_sec=60)
    listed = client.get("/api/knowledge/documents")
    assert listed.status_code == 200, listed.text[:300]
    payload = listed.json()
    assert "\\ud800" in payload["items"][0]["content"], payload
    listed.text.encode("utf-8")
    cats = client.get("/api/knowledge/categories")
    assert cats.status_code == 200, cats.text[:300]
    print("✓ 知识库列表 / 分类清洗孤立代理项")


def test_knowledge_duplicates_limit_zero_caps_on_web(monkeypatch):
    """Web limit=0 最多返回 2000 组并标记 truncated（全量审计走 CLI）。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    groups = [(f"内容{i}", [i * 2 + 1, i * 2 + 2]) for i in range(2001)]
    monkeypatch.setattr(server_module, "find_duplicate_contents", lambda: groups)
    client = _client()
    client.app.state.aligo.kb_limiter = RateLimiter(limit=10, window_sec=60)
    response = client.get("/api/knowledge/duplicates", params={"limit": 0})
    assert response.status_code == 200, response.text[:200]
    payload = response.json()
    assert payload["total_groups"] == 2001, payload["total_groups"]
    assert payload["shown"] == 2000
    assert payload["truncated"] is True
    print("✓ Web 重复组 limit=0 截断到 2000 并标记 truncated")


def test_knowledge_cleanup_plan_truncated(monkeypatch):
    """清理接口的 plan 与读接口同一上限：超过 2000 组时截断并标记。

    ``plan_total`` / ``truncated`` 恒出现（未截断时为组数与 false）；
    截断只影响视图——``dry_run=false`` 的删除仍按全量计划执行（fake 记录
    传入的 dry_run 并确认响应不含截断后的执行语义变化）。
    """
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    calls: list[bool] = []
    plan = [{"keep": i, "remove": [i + 10000]} for i in range(2001)]

    def _cleanup(dry_run):
        calls.append(dry_run)
        return {
            "dry_run": dry_run,
            "removed": 0 if dry_run else 2001,
            "skipped": [],
            "plan": list(plan),
        }

    monkeypatch.setattr(server_module, "cleanup_duplicate_contents", _cleanup)
    client = _client()
    client.app.state.aligo.kb_limiter = RateLimiter(limit=10, window_sec=60)
    response = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": True}
    )
    assert response.status_code == 200, response.text[:300]
    payload = response.json()
    assert payload["truncated"] is True, payload.get("truncated")
    assert payload["plan_total"] == 2001
    assert len(payload["plan"]) == 2000

    # dry_run=false：全量执行（fake 收到 False、计数来自全量计划），
    # 视图截断与执行语义互不影响
    executed = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": False}
    )
    assert executed.status_code == 200, executed.text[:300]
    executed_payload = executed.json()
    assert calls == [True, False]
    assert executed_payload["removed"] == 2001
    assert executed_payload["plan_total"] == 2001
    assert executed_payload["truncated"] is True
    assert len(executed_payload["plan"]) == 2000

    # 未截断：字段恒定出现，消费者无需区分「未截断」与旧服务端
    monkeypatch.setattr(
        server_module,
        "cleanup_duplicate_contents",
        lambda dry_run: {
            "dry_run": dry_run,
            "removed": 0,
            "skipped": [],
            "plan": [{"keep": 1, "remove": [2]}],
        },
    )
    small = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": True}
    )
    assert small.status_code == 200, small.text[:300]
    small_payload = small.json()
    assert small_payload["plan_total"] == 1
    assert small_payload["truncated"] is False
    assert len(small_payload["plan"]) == 1
    print("✓ 清理接口 plan 截断到 2000 组并恒定标记 plan_total / truncated")


def test_knowledge_cleanup_redacts_failed_errors(monkeypatch):
    """清理接口的 failed[].error 不得回显本机绝对路径。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "cleanup_duplicate_contents",
        lambda dry_run: {
            "dry_run": dry_run,
            "removed": 0,
            "skipped": [],
            "failed": [
                {
                    "id": 1,
                    "error": r"IOError: D:\secret\dir\memory.json is locked",
                }
            ],
        },
    )
    client = _client()
    client.app.state.aligo.kb_limiter = RateLimiter(limit=10, window_sec=60)
    response = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": False}
    )
    assert response.status_code == 200, response.text[:300]
    error = response.json()["failed"][0]["error"]
    assert r"D:\secret" not in error, error
    assert "IOError" in error, error
    print("✓ 清理接口脱敏 failed 错误")


def test_knowledge_duplicates_api(monkeypatch):
    """重复知识片段接口：与 CLI 同口径分组，limit=0 全量，存储失败 503。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    groups = [
        ("完全相同的内容", [3, 7]),
        ("另一组重复", [9, 10, 11]),
    ]
    monkeypatch.setattr(
        server_module, "find_duplicate_contents", lambda: list(groups)
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.kb_limiter = RateLimiter(limit=5, window_sec=60)
    client = TestClient(app)
    assert 'id="kbDuplicates"' in client.get("/").text, "面板必须提供「查重复」入口"

    response = client.get("/api/knowledge/duplicates", params={"limit": 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_groups"] == 2
    assert data["total_fragments"] == 5, "总数按全量组统计，不受 limit 影响"
    assert data["shown"] == 1
    assert data["groups"][0]["ids"] == [3, 7]
    assert data["groups"][0]["content"] == "完全相同的内容"
    assert data["groups"][0]["preview"] == "完全相同的内容"
    assert "x-ratelimit-limit" in response.headers

    all_groups = client.get(
        "/api/knowledge/duplicates", params={"limit": 0}
    ).json()
    assert all_groups["shown"] == 2
    assert all_groups["groups"][1]["ids"] == [9, 10, 11]

    monkeypatch.setattr(server_module, "find_duplicate_contents", lambda: [])
    empty = client.get("/api/knowledge/duplicates")
    assert empty.status_code == 200
    assert empty.json() == {
        "total_groups": 0,
        "total_fragments": 0,
        "shown": 0,
        "truncated": False,
        "groups": [],
    }

    def boom():
        raise RuntimeError("milvus down")

    monkeypatch.setattr(server_module, "find_duplicate_contents", boom)
    failed = client.get("/api/knowledge/duplicates")
    assert failed.status_code == 503
    assert "读取失败" in failed.json()["detail"]

    def missing():
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(server_module, "find_duplicate_contents", missing)
    missing_response = client.get("/api/knowledge/duplicates")
    assert missing_response.status_code == 503
    assert "先运行初始化脚本" in missing_response.json()["detail"]

    invalid = client.get("/api/knowledge/duplicates", params={"limit": 999})
    assert invalid.status_code == 422

    # 额度 5 已在上面用尽（422 不消耗）：再请求直接 429
    rejected = client.get("/api/knowledge/duplicates")
    assert rejected.status_code == 429
    assert rejected.headers.get("retry-after")
    assert rejected.headers.get("x-ratelimit-remaining") == "0"
    print("✓ /api/knowledge/duplicates 重复检查（分组 / limit / 503 / 422 / 429）")


def test_knowledge_duplicates_cleanup_api(monkeypatch):
    """清理重复：JSON body（默认 dry-run）；无 body 422（跨站简单 POST 打不到删除）。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    calls = []
    payloads = {
        True: {
            "dry_run": True,
            "groups": 2,
            "planned": 3,
            "removed": 0,
            "skipped": [],
            "failed": [],
            "plan": [
                {
                    "preview": "重复内容",
                    "content": "重复内容",
                    "keep": 3,
                    "remove": [7],
                },
                {
                    "preview": "另一组",
                    "content": "另一组",
                    "keep": 9,
                    "remove": [10, 11],
                },
            ],
        },
        False: {
            "dry_run": False,
            "groups": 2,
            "planned": 3,
            "removed": 1,
            "skipped": [{"id": 10, "reason": "片段不存在或已被删除"}],
            "failed": [{"id": 11, "error": "milvus write failed"}],
            "plan": [],
        },
    }

    def fake_cleanup(dry_run=True):
        calls.append(dry_run)
        return payloads[dry_run]

    monkeypatch.setattr(
        server_module, "cleanup_duplicate_contents", fake_cleanup
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.kb_limiter = RateLimiter(limit=10, window_sec=60)
    client = TestClient(app)
    assert 'id="kbDedupe"' in client.get("/").text, "面板必须提供「清理重复」入口"

    # 无 JSON body：422，且不进入清理逻辑（防跨站无预检简单 POST）
    blocked = client.post("/api/knowledge/duplicates/cleanup")
    assert blocked.status_code == 422
    assert calls == []

    # 跨站表单（application/x-www-form-urlencoded）同样 422：
    # 浏览器表单发不出 application/json，破坏性清理不能被第三方页面触发
    form_blocked = client.post(
        "/api/knowledge/duplicates/cleanup", data={"dry_run": "false"}
    )
    assert form_blocked.status_code == 422
    assert calls == [], "表单 POST 不能进入清理逻辑"

    preview = client.post("/api/knowledge/duplicates/cleanup", json={})
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["dry_run"] is True
    assert data["planned"] == 3
    assert data["plan"][0]["keep"] == 3, "每组保留最小 id"
    assert data["plan"][0]["remove"] == [7]
    assert "x-ratelimit-limit" in preview.headers
    assert calls == [True]

    applied = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": False}
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["removed"] == 1
    assert result["skipped"][0]["id"] == 10
    assert result["failed"][0]["id"] == 11
    assert calls == [True, False], "dry_run 必须透传到服务层"

    def missing_boom(dry_run=True):
        raise FileNotFoundError("知识库未初始化")

    monkeypatch.setattr(
        server_module, "cleanup_duplicate_contents", missing_boom
    )
    missing = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": True}
    )
    assert missing.status_code == 503
    assert "先运行初始化脚本" in missing.json()["detail"]

    def fail_boom(dry_run=True):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(server_module, "cleanup_duplicate_contents", fail_boom)
    failed = client.post(
        "/api/knowledge/duplicates/cleanup", json={"dry_run": False}
    )
    assert failed.status_code == 503
    assert "清理失败" in failed.json()["detail"]
    print("✓ /api/knowledge/duplicates/cleanup 需 JSON body / dry-run / 503")

def test_knowledge_eval_import_failure(monkeypatch):
    """技能文件缺失时评测依赖 import 失败，应返回 503 而不是 500。"""
    import sys
    import types

    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda **_: {"available": True, "collection": "kb", "documents": 3, "path": "p"},
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


def test_knowledge_update_drops_internal_metadata_keys(monkeypatch):
    """get_document 回传的内部键不占用户 10 键额度（面板保存不再 422）。"""
    from webapp import server as server_module

    captured = {}

    def fake_update(doc_id, content, metadata=None, **kwargs):
        captured["metadata"] = metadata
        return {"status": "success", "id": doc_id, "parts": 1}

    monkeypatch.setattr(server_module, "update_document", fake_update)
    user_keys = {f"k{index}": "v" for index in range(10)}
    payload_meta = {
        **user_keys,
        "user_edited": "1",
        "part": "1",
        "parts": "2",
        "group": "g",
    }
    response = _client().put(
        "/api/knowledge/documents/1",
        json={"content": "新内容", "metadata": payload_meta},
    )
    assert response.status_code == 200, response.text
    assert captured["metadata"] == user_keys, captured
    print("✓ 面板保存时内部元数据键被丢弃（10 用户键不触发 422）")


def test_knowledge_status_degrades_on_dimension_mismatch(monkeypatch):
    """维度与嵌入模型不一致时状态芯片不得显示健康（检索全部 503）。"""
    from webapp import server as server_module

    captured = {}

    def fake_status(**kwargs):
        captured.update(kwargs)
        return {
            "available": True,
            "documents": 3,
            "dimension_mismatch": True,
        }

    monkeypatch.setattr(server_module, "knowledge_status", fake_status)
    response = _client().get("/api/knowledge")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "维度" in body["reason"] and "重建" in body["reason"], body
    assert captured.get("check_configured_dim") is True, (
        "冷进程也要按模型目录配置发现维度不一致"
    )
    print("✓ 维度不一致时 /api/knowledge 降级为不可用并给出重建指引")


def test_knowledge_eval_rejects_dimension_mismatch(monkeypatch):
    """冷进程维度不一致：评测入口直接 503，不构造 Agent 抛底层异常。"""
    from webapp import server as server_module

    captured = {}

    def fake_status(**kwargs):
        captured.update(kwargs)
        return {
            "available": True,
            "collection": "kb",
            "documents": 3,
            "path": "p",
            "dimension": 512,
            "dimension_mismatch": True,
        }

    monkeypatch.setattr(server_module, "knowledge_status", fake_status)
    response = _client().get("/api/knowledge/eval")
    assert response.status_code == 503
    assert "维度" in response.json()["detail"]
    assert captured.get("check_configured_dim") is True
    print("✓ 冷进程维度不一致时评测入口 503 并给出重建指引")


def test_knowledge_dimension_hint_follows_install_mode(monkeypatch):
    """维度不一致的重建指引随安装形态切换：wheel 下必须是可执行的 aligo 命令。"""
    from webapp import server as server_module
    import utils.paths as paths_module

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda **kwargs: {
            "available": True,
            "documents": 3,
            "dimension": 512,
            "dimension_mismatch": True,
        },
    )
    monkeypatch.setattr(
        paths_module, "is_source_checkout", lambda *args, **kwargs: False
    )
    body = _client().get("/api/knowledge").json()
    assert "aligo kb-init --documents" in body["reason"], body
    assert ".claude/skills" not in body["reason"], body
    detail = _client().get("/api/knowledge/eval").json()["detail"]
    assert "aligo kb-init --documents" in detail, detail
    assert ".claude/skills" not in detail, detail

    monkeypatch.setattr(
        paths_module, "is_source_checkout", lambda *args, **kwargs: True
    )
    body = _client().get("/api/knowledge").json()
    assert "init_knowledge_base.py" in body["reason"], body
    print("✓ 维度重建指引随安装形态切换（wheel / 源码）")


def test_knowledge_export_strips_nul(monkeypatch):
    """专用导出剥离 NUL：否则导入端把该条目标为无效、导出→导入丢片段。"""
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [
            {
                "content": "前" + chr(0) + "后",
                "metadata": {"title": "标" + chr(0) + "题"},
            }
        ],
    )
    response = _client().get("/api/knowledge/export")
    assert response.status_code == 200
    assert chr(0) not in response.text
    documents = response.json()["documents"]
    assert documents == [{"content": "前后", "metadata": {"title": "标题"}}], documents
    print("✓ 知识专用导出剥离 NUL（导出→导入不丢片段）")


def test_knowledge_export_guard_counts_utf8_bytes(monkeypatch):
    """导出 413 守卫按 UTF-8 字节而非字符数：中文库不能放行面板拒收的文件。"""
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [{"id": 1, "content": "住" * 100, "metadata": {}}],
    )
    # 中文文档紧凑序列化约 140 字符、UTF-8 约 340 字节：字符口径会放行
    monkeypatch.setattr("utils.user_import.MAX_BACKUP_BYTES", 200)
    response = _client().get("/api/knowledge/export")
    assert response.status_code == 413, response.text[:200]
    assert "面板导入上限" in response.json()["detail"]

    # ASCII 小文档仍正常导出（字节口径不误伤）
    monkeypatch.setattr(
        server_module,
        "export_documents",
        lambda: [{"id": 2, "content": "ok", "metadata": {}}],
    )
    ok = _client().get("/api/knowledge/export")
    assert ok.status_code == 200 and ok.json()["documents"][0]["id"] == 2

    # 估算助手：同一文档 UTF-8 字节严格大于字符数
    estimated = server_module._estimated_export_bytes(
        [{"content": "住" * 10}], indent=2
    )
    assert estimated > len('{"content": "' + "住" * 10 + '"}')
    print("✓ 知识导出守卫按 UTF-8 字节计算（中文库不放行超限文件）")


def test_knowledge_empty_collection_degrades_status_and_eval(monkeypatch):
    """空集合（0 片段）按不可用展示；评测直接 503，不白跑 30 题 0%。"""
    from webapp import server as server_module

    monkeypatch.setattr(
        server_module,
        "knowledge_status",
        lambda **_: {
            "available": True,
            "collection": "kb",
            "documents": 0,
            "path": "p",
        },
    )
    body = _client().get("/api/knowledge").json()
    assert body["available"] is False and body["empty"] is True, body
    assert "集合为空" in body["reason"], body
    response = _client().get("/api/knowledge/eval")
    assert response.status_code == 503
    assert "集合为空" in response.json()["detail"]
    print("✓ 空集合时状态降级、评测 503（与 doctor / CLI 同口径）")


def test_kb_group_lookup_uses_read_slots(monkeypatch):
    """整组回填也是全库扫描：槽满时 503，不触发底层扫描。"""
    from webapp import server as server_module

    calls = []

    def fake_group(doc_id):
        calls.append(doc_id)
        return {"id": doc_id, "content": "x", "parts": 1}

    monkeypatch.setattr(server_module, "get_document_group", fake_group)
    client = _client()
    state = client.app.state.aligo
    held = [state.kb_read_slots.acquire(blocking=False) for _ in range(4)]
    try:
        assert all(held)
        response = client.get("/api/knowledge/documents/1/group")
        assert response.status_code == 503
        assert "知识库读取任务过多" in response.json()["detail"]
        assert calls == [], "槽满时不得触发底层全库扫描"
    finally:
        for _ in held:
            state.kb_read_slots.release()
    ok = client.get("/api/knowledge/documents/1/group")
    assert ok.status_code == 200 and calls == [1]
    print("✓ 整组回填受知识库读并发槽约束")


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
        "skipped_conflict": 0,
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


def test_kb_import_partial_and_capacity_mapping(monkeypatch):
    """知识导入：部分提交 503 带已提交条数；容量上限 409（不是 400）。"""
    from utils.errors import AccountCapacityError, PartialImportError
    from webapp import server as server_module

    client = _client()

    def partial(documents, dry_run=False):
        raise PartialImportError(
            "第 2 批导入失败（已导入 500 条）：milvus down",
            created=500,
            updated=0,
        )

    monkeypatch.setattr(server_module, "import_documents", partial)
    response = client.post(
        "/api/knowledge/import", json={"documents": [{"content": "x"}]}
    )
    assert response.status_code == 503, response.text[:300]
    detail = response.json()["detail"]
    assert "已提交 500 条" in detail and "跳过已导入" in detail, detail
    assert "milvus down" not in detail, detail

    def capacity(documents, dry_run=False):
        raise AccountCapacityError("知识库片段总数已达上限（200000），请先删除旧片段")

    monkeypatch.setattr(server_module, "import_documents", capacity)
    response = client.post(
        "/api/knowledge/import", json={"documents": [{"content": "x"}]}
    )
    assert response.status_code == 409, response.text[:300]
    assert "上限" in response.json()["detail"]

    def add_capacity(content, metadata=None):
        raise AccountCapacityError("知识库片段总数已达上限（200000），请先删除旧片段")

    monkeypatch.setattr(server_module, "add_document", add_capacity)
    response = client.post(
        "/api/knowledge/documents",
        json={"content": "x", "metadata": {"category": "c"}},
    )
    assert response.status_code == 409, response.text[:300]
    print("✓ 知识导入部分提交 503 / 容量上限 409")


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


def test_usage_summary_tz_parameter_buckets_browser_day():
    """tz 参数按浏览器时区分桶与取 7 天窗口（容器 UTC + 东八区浏览器）。

    浏览器本地 0–8 点的调用时间戳落在 UTC 前一天：不传 tz 时进 UTC 桶、
    最新柱不是浏览器「今天」，前端按浏览器日期加的今天高亮整张图无命中。
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from webapp.server import _usage_summary

    zone = ZoneInfo("Asia/Shanghai")
    browser_today = datetime.now(zone).date()
    # 浏览器本地「今天」04:00 = UTC 前一天 20:00
    stamp = datetime.combine(
        browser_today, datetime.min.time(), tzinfo=zone
    ) + timedelta(hours=4)
    meta = {
        "usage": {"calls": 1, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "elapsed": 1.0,
        "agents": [],
    }
    history = [
        {
            "role": "assistant",
            "content": json.dumps({"results": [], "_meta": meta}, ensure_ascii=False),
            "timestamp": stamp.astimezone(timezone.utc).isoformat(),
            "session_id": "s",
        }
    ]
    usage = _usage_summary(history, "Asia/Shanghai")
    buckets = {day["date"]: day["tokens"] for day in usage["by_day"]}
    today_text = browser_today.isoformat()
    assert buckets.get(today_text) == 5, f"应记到浏览器日期 {today_text}: {buckets}"
    assert usage["by_day"][-1]["date"] == today_text, usage["by_day"]
    # 非法 / 未知时区退回服务器本地，不能 500
    fallback = _usage_summary(history, "Not/AZone")
    assert len(fallback["by_day"]) == 7
    print("✓ 用量 by_day 支持浏览器时区参数")


def test_usage_summary_naive_timestamp_uses_browser_zone():
    """JSON 后端的 naive 服务器墙钟也要按浏览器时区分桶（tz 不能只对 PG 生效）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from utils.timestamps import to_aware
    from webapp.server import _usage_summary

    naive = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    aware = to_aware(naive.isoformat())
    target = ZoneInfo("Pacific/Midway")  # UTC-11：与东八区 / UTC 都跨日
    expected = aware.astimezone(target).date().isoformat()
    server_day = naive.date().isoformat()
    if expected == server_day:
        pytest.skip("本机时区与 UTC-11 同一天，无法构造跨日用例")

    meta = {
        "usage": {"calls": 1, "input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        "elapsed": 1.0,
        "agents": [],
    }
    history = [
        {
            "role": "assistant",
            "content": json.dumps({"results": [], "_meta": meta}, ensure_ascii=False),
            "timestamp": naive.isoformat(),
            "session_id": "s",
        }
    ]
    usage = _usage_summary(history, "Pacific/Midway")
    buckets = {day["date"]: day["tokens"] for day in usage["by_day"]}
    assert buckets.get(expected) == 5, (buckets, expected)
    assert buckets.get(server_day, 0) == 0, "naive 时间戳不能绕过 tz 参数"

    # 缺省（CLI / 导出）仍是服务器本地日期口径
    local_buckets = {
        day["date"]: day["tokens"] for day in _usage_summary(history)["by_day"]
    }
    assert local_buckets.get(server_day) == 5, local_buckets
    print("✓ 用量 by_day 对 naive 时间戳同样按浏览器时区分桶")


def test_status_today_defaults_to_tz_when_only_tz_given():
    """只传文档化的 tz 时，行程状态与用量窗口共用同一个「今天」。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Pacific/Midway")
    zone_today = datetime.now(zone).date()
    if zone_today == datetime.now().date():
        pytest.skip("本机时区与 UTC-11 同一天，无法构造跨日用例")
    client = _client()
    user = _unique_user("web_status_tz_today")
    day = zone_today.isoformat()
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user,
                "origin": "北京",
                "destination": "杭州",
                "start_date": day,
                "end_date": day,
            },
        )
        assert created.status_code == 201, created.text
        data = client.get(
            "/api/status", params={"user_id": user, "tz": "Pacific/Midway"}
        ).json()
        assert data["travel_summary"]["active"] == 1, data["travel_summary"]
        assert (data["next_trip"] or {}).get("destination") == "杭州", data["next_trip"]
        # 用量窗口同口径：最后一格就是该时区的今天
        assert data["usage"]["by_day"][-1]["date"] == day, data["usage"]["by_day"]
    finally:
        _cleanup(user)
    print("✓ /api/status 只传 tz 时 today 按同时区推断")


def test_cached_chronological_key_invalidates_on_tz_change():
    """chronological_key 缓存必须带时区世代：进程内切 TZ 后不得命中旧结果。"""
    import time as time_module

    if not hasattr(time_module, "tzset"):
        pytest.skip("当前平台不支持动态时区（time.tzset）")
    from webapp import server as server_module

    server_module._cached_chronological_key.cache_clear()
    timestamp = "2026-03-04T00:30:00"
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time_module.tzset()
        utc_key = server_module._cached_chronological_key(
            server_module._tz_epoch(), timestamp
        )
        os.environ["TZ"] = "Asia/Shanghai"
        time_module.tzset()
        shanghai_key = server_module._cached_chronological_key(
            server_module._tz_epoch(), timestamp
        )
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time_module.tzset()
    assert utc_key == "2026-03-04T00:30:00+00:00", utc_key
    assert shanghai_key == "2026-03-03T16:30:00+00:00", shanghai_key
    print("✓ 时间戳排序键缓存随时区切换失效")


def test_group_sessions_chronological_key_cache_serves_repeat_requests(monkeypatch):
    """同一历史重复请求：排序键全部命中缓存，输出与首次逐字段一致。"""
    from webapp import server as server_module

    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"m{index}",
            "timestamp": f"2026-03-{1 + index % 5:02d}T10:{index:02d}:00",
            "session_id": f"s{index % 4}",
        }
        for index in range(40)
    ]
    first = server_module._group_sessions(history, 1_000_000)

    def _boom(value):
        raise AssertionError("热请求不应重新计算排序键")

    monkeypatch.setattr(server_module, "chronological_key", _boom)
    second = server_module._group_sessions(history, 1_000_000)
    assert second == first, "记忆化后的会话列表必须与首次逐字段一致"
    print("✓ 会话列表排序键命中缓存且输出不变")


def test_archived_count_shared_live_ids_matches_scan():
    """列表聚合收集的 live ids 与 _archived_count 全量扫描口径逐项一致。"""
    from webapp import server as server_module

    history = [
        {"session_id": "s1", "content": "a", "timestamp": "2026-01-01T00:00:00"},
        {"session_id": "s1", "content": "b", "timestamp": "2026-01-01T00:01:00"},
        {"session_id": "export", "content": "c", "timestamp": "2026-01-01T00:02:00"},
        {"session_id": " ", "content": "d", "timestamp": "2026-01-01T00:03:00"},
        {"session_id": 0, "content": "e", "timestamp": "2026-01-01T00:04:00"},
        {"session_id": None, "content": "f", "timestamp": "2026-01-01T00:05:00"},
    ]
    data = {
        "chat_history": history,
        # 集合语义：重复条目只计一次；保留字 'export' 与纯空白串不算可见
        # 会话；整数 0 是合法会话（与 PG 的 TEXT 列 / 归档视图一致）；
        # ghost 没有消息 —— 计数 = {"s1", "0"} 与可见集合的交集
        "archived_sessions": ["s1", "s1", "export", " ", "0", "", "ghost"],
    }
    live: set[str] = set()
    server_module._group_sessions(history, 1_000_000, live_session_ids=live)
    shared = server_module._archived_count(data, live)
    assert shared == server_module._archived_count(data) == 2
    # 归档计数必须与归档视图的会话数逐项一致（重复 / 保留字 / 整数 0）
    archived_view = server_module._group_sessions(
        history, 1_000_000, archived=data["archived_sessions"], archived_mode="only"
    )
    assert len(archived_view) == shared
    assert {item["session_id"] for item in archived_view} == {"s1", "0"}
    assert (
        server_module._archived_count(
            {"chat_history": history, "archived_sessions": []}, live
        )
        == 0
    ), "无归档账号直接归零"
    print("✓ 归档计数复用聚合集合（口径与归档视图一致）")


def test_export_reuses_serialized_payload_for_limit_check(monkeypatch):
    """导出只序列化一次：上限自检走 precomputed_size，响应体复用同一串字节。"""
    from utils import user_import

    seen: list[object] = []
    original = user_import._backup_payload_size

    def _spy(backup, *, crlf=None):
        seen.append(crlf)
        return original(backup, crlf=crlf)

    monkeypatch.setattr(user_import, "_backup_payload_size", _spy)
    user_id = _unique_user("web_test_export_precomputed")
    try:
        response = _client().get("/api/export", params={"user_id": user_id})
        assert response.status_code == 200, response.text
        assert seen == [], "已清洗备份的字节数应复用，不再重复清洗 + 序列化"
        body = json.loads(response.content)
        assert body["user_id"] == user_id
        # 响应体 == 自检口径的同一份字节（indent=2、LF、UTF-8）
        assert (
            user_import._backup_payload_size(body, crlf=False)
            == len(response.content)
        ), "响应体必须与体积自检使用的字节完全一致"
        print("✓ 导出复用同一份序列化字节")
    finally:
        _cleanup(user_id)


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


def test_usage_summary_total_consistent_with_by_day_for_partial_usage():
    """备份导入允许只有 total_tokens 的部分 usage：总量与按天必须同口径。"""
    from datetime import datetime

    from webapp.server import _usage_summary

    today = datetime.now().date().isoformat()

    def _assistant(meta: dict) -> dict:
        return {
            "role": "assistant",
            "content": json.dumps(
                {"results": [], "_meta": meta}, ensure_ascii=False
            ),
            "timestamp": f"{today}T12:00:00",
            "session_id": "s",
        }

    history = [
        # 只有 total_tokens（导入的历史备份形态）
        _assistant({"usage": {"calls": 1, "total_tokens": 100}}),
        # 正常形态：input+output 与 total_tokens 一致
        _assistant(
            {
                "usage": {
                    "calls": 1,
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                }
            }
        ),
    ]
    usage = _usage_summary(history)
    assert usage["total_tokens"] == 105, usage
    bucket = next(d for d in usage["by_day"] if d["date"] == today)
    assert bucket["tokens"] == usage["total_tokens"], (bucket, usage)
    assert usage["calls"] == 2
    print("✓ 部分 usage 的总 tokens 与按天统计同口径")


def test_usage_summary_ignores_negative_usage_and_elapsed():
    """备份恢复可带入负数 usage / elapsed：不能扣减总量或拉低平均耗时。"""
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

    history = [
        _assistant(
            {
                "usage": {
                    "calls": 1,
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
                "elapsed": 2.0,
            }
        ),
        # 备份文件里的坏数据：负数 usage 与负数耗时
        _assistant(
            {
                "usage": {
                    "calls": -3,
                    "input_tokens": -100,
                    "output_tokens": -1,
                    "total_tokens": -1000,
                },
                "elapsed": -50.0,
            }
        ),
    ]
    usage = _usage_summary(history)
    assert usage["calls"] == 1, usage
    assert usage["total_tokens"] == 5, usage
    assert usage["input_tokens"] == 2 and usage["output_tokens"] == 3, usage
    assert usage["avg_elapsed"] == 2.0, usage
    assert all(day["tokens"] >= 0 for day in usage["by_day"]), usage["by_day"]
    print("✓ 负数 usage / elapsed 不计入状态汇总")


def test_usage_summary_cached_per_user(monkeypatch):
    """/api/status 的用量汇总按用户缓存：历史不变时不再全量重放。"""
    from webapp import server as server_module

    server_module._USAGE_SUMMARY_CACHE.clear()
    calls = {"n": 0}
    real = server_module._usage_summary

    def counting(history, tz=None):
        calls["n"] += 1
        return real(history, tz)

    monkeypatch.setattr(server_module, "_usage_summary", counting)
    history = [
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "results": [],
                    "_meta": {
                        "usage": {
                            "calls": 1,
                            "input_tokens": 2,
                            "output_tokens": 3,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            "timestamp": "2026-11-11T12:00:00",
            "session_id": "s",
        }
    ]
    try:
        first = server_module._usage_summary_for_user("u-cache", history)
        second = server_module._usage_summary_for_user("u-cache", history)
        assert calls["n"] == 1, "历史未变时必须命中缓存"
        assert first["total_tokens"] == second["total_tokens"] == 5
        grown = history + [
            {"role": "user", "content": "追加消息", "timestamp": "2026-11-11T12:01:00"}
        ]
        server_module._usage_summary_for_user("u-cache", grown)
        assert calls["n"] == 2, "历史变化后必须重新汇总"
        # 时区进入指纹：同一历史按不同时区取窗口不能命中旧缓存
        server_module._usage_summary_for_user("u-cache", grown, "Asia/Shanghai")
        assert calls["n"] == 3, "换时区（窗口口径变化）必须重新汇总"
        # 多时区客户端交替刷新：每个时区各自命中槽位，不能互相顶掉
        server_module._usage_summary_for_user("u-cache", grown)
        server_module._usage_summary_for_user("u-cache", grown, "Asia/Shanghai")
        assert calls["n"] == 3, "时区交替请求必须命中各自缓存槽位"
        # 缓存封顶：不同用户不会无限写入
        for index in range(server_module._USAGE_SUMMARY_CACHE_MAX + 5):
            server_module._usage_summary_for_user(f"u-{index}", [])
        assert (
            len(server_module._USAGE_SUMMARY_CACHE)
            <= server_module._USAGE_SUMMARY_CACHE_MAX
        )
    finally:
        server_module._USAGE_SUMMARY_CACHE.clear()
    print("✓ /api/status 用量汇总按用户缓存且封顶")



def test_usage_cache_fingerprint_rolls_with_day(monkeypatch):
    """跨过午夜后指纹必须变化：近 7 天窗口要向前滚动。"""
    from webapp import server as server_module

    real_datetime = server_module.datetime

    class _FixedDatetime(real_datetime):
        fixed = real_datetime(2026, 11, 10, 23, 59)

        @classmethod
        def now(cls, tz=None):
            return cls.fixed

    monkeypatch.setattr(server_module, "datetime", _FixedDatetime)
    history = [
        {"role": "assistant", "content": "{}", "timestamp": "t", "session_id": "s"}
    ]
    first = server_module._usage_history_fingerprint(history)
    _FixedDatetime.fixed = real_datetime(2026, 11, 11, 0, 1)
    second = server_module._usage_history_fingerprint(history)
    assert first[0] == "2026-11-10", first
    assert second[0] == "2026-11-11", second
    assert first != second
    print("✓ 用量缓存指纹包含当天日期（跨午夜自动失效）")

def test_knowledge_eval_bounded_lock_and_masked_status(monkeypatch):
    """评测端点查状态必须带写锁超时、屏蔽路径与原始异常。"""
    from webapp import server as server_module

    seen: dict = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return {
            "available": False,
            "reason": "知识库正在写入（导入进行中），请稍后刷新",
        }

    monkeypatch.setattr(server_module, "knowledge_status", fake_status)
    response = _client().get("/api/knowledge/eval")
    assert response.status_code == 503
    assert seen == {
        "include_path": False,
        "detail_errors": False,
        "lock_timeout": 0.5,
        # 冷进程也要按模型目录配置发现维度不一致（重启后首次评测即 503）
        "check_configured_dim": True,
    }, seen
    detail = response.json()["detail"]
    assert "正在写入" in detail
    assert "milvus_lite.db" not in detail and "C:\\" not in detail
    print("✓ 评测端点有界等待写锁且不泄露路径 / 原始异常")


def test_invalid_user_id_rejected():
    response = _client().post("/api/chat", json={"message": "你好", "user_id": "../etc"})
    assert response.status_code == 422
    print("✓ 非法用户 ID 被拒绝（防止路径穿越）")


def test_export_attachment():
    user_id = _unique_user("web_test_export")
    try:
        response = _client().get("/api/export", params={"user_id": user_id})
        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")
        payload = json.loads(response.text)
        assert payload["user_id"] == user_id
        print("✓ /api/export 返回可下载 JSON")
    finally:
        _cleanup(user_id)


def test_export_reports_skipped_knowledge_header(monkeypatch):
    """导出体积通过但知识行恢复会跳过时，响应头带跳过计数。"""
    from utils import user_import

    def _fake(backup, warning_out=None, reason_out=None, precomputed_size=None):
        if warning_out is not None:
            warning_out["knowledge_skipped_count"] = 3
        return None

    monkeypatch.setattr(
        user_import, "export_backup_limit_problem", _fake
    )
    user_id = _unique_user("web_test_export_skipped")
    try:
        response = _client().get(
            "/api/export", params={"user_id": user_id}
        )
        assert response.status_code == 200, response.text
        assert response.headers.get("x-knowledge-skipped") == "3"
        print("✓ /api/export 提示恢复会跳过的知识行")
    finally:
        _cleanup(user_id)


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


def test_route_path_normalization():
    from webapp.server import _route_path

    assert _route_path({"path": "/aligo/api/import", "root_path": "/aligo"}) == "/api/import"
    assert _route_path({"path": "/api/import/"}) == "/api/import"
    assert _route_path({"path": "/aligo/", "root_path": "/aligo"}) == "/"
    assert _route_path({"path": "/"}) == "/"
    # root_path="/" 等价无前缀：剥离后必须保留前导斜杠
    assert _route_path({"path": "/api/health", "root_path": "/"}) == "/api/health"
    print("✓ 路由视角路径归一（root_path / 尾斜杠）")


def test_validation_error_truncates_large_input():
    """422 回显的 input 截断在 200 字内：大请求体不被完整 repr 物化。"""
    client = _client()
    big = "x" * 500_000
    response = client.post(
        "/api/knowledge/import", json={"documents": big}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    text = str(detail)
    assert len(text) < 5000, "422 响应不应回显整份请求体"
    print("✓ 422 大输入截断")


def test_chat_maps_user_facing_error_to_502(monkeypatch):
    """UserFacingError 的消息可原样返回（默认 502），不能吞成通用 500。

    该类异常全部来自服务端 / 上游（解析失败、预算耗尽），不是客户端请求
    非法；返回 4xx 会让客户端重试中间件放弃重试。
    """
    from utils.errors import UserFacingError
    from webapp import server as server_module

    async def broken_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise UserFacingError("意图识别结果解析失败，请重试")

    monkeypatch.setattr(
        server_module.ConversationRuntime, "ask", broken_ask
    )
    client = _client()
    user_id = _unique_user("web_test_userfacing")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "s-uf"},
        )
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert "意图识别结果解析失败，请重试" in detail
        assert "请求 ID" in detail
        print("✓ UserFacingError 映射 502 并原样透出文案")
    finally:
        _cleanup(user_id)


def test_chat_maps_budget_exhausted_to_503_with_retry_after(monkeypatch):
    """整轮预算耗尽是瞬时状态：503 + Retry-After，不是客户端错误 400。"""
    from utils.errors import UserFacingError
    from webapp import server as server_module

    async def broken_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise UserFacingError("本轮处理时间超出预算，请重试", status_code=503)

    monkeypatch.setattr(
        server_module.ConversationRuntime, "ask", broken_ask
    )
    client = _client()
    user_id = _unique_user("web_test_budget")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user_id, "session_id": "s-budget"},
        )
        assert response.status_code == 503, response.text
        assert response.headers.get("Retry-After") == "5"
        print("✓ 预算耗尽映射 503 + Retry-After")
    finally:
        _cleanup(user_id)


def test_session_cache_evicts_on_hit_path(monkeypatch):
    """命中已有会话时同样执行容量回收：并发首请求不能把缓存推过上限。"""
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    created = []

    class _Runtime:
        def __init__(self, user_id, session_id, model=None, circuit_breaker=None, **kwargs):
            self.user_id = user_id
            self.session_id = session_id
            self.closed = 0
            created.append(self)

        def close(self):
            self.closed += 1

    monkeypatch.setattr(server_module, "ConversationRuntime", _Runtime)
    state.max_sessions = 1
    first = state.get_session("u-lru", "s-a")
    second = state.get_session("u-lru", "s-b")
    assert len(state.sessions) == 1

    # 人为制造「命中时已超限」：缓存里多出一个空闲旧会话，命中 s-b 应淘汰它
    closed = []

    class _Victim:
        def in_use(self):
            return False

        def long_in_use(self, limit_sec):
            return False

        def close(self):
            closed.append(1)

    with state._sessions_lock:
        state.sessions["u-lru" + chr(10) + "s-victim"] = _Victim()
    third = state.get_session("u-lru", "s-b")
    assert third is second
    assert len(state.sessions) == 1, "命中路径必须回收超限会话"
    assert closed == [1], "被淘汰会话应被关闭"
    assert first.runtime.closed >= 1
    print("✓ 会话缓存命中路径执行 LRU 回收")


def test_rate_identity_ipv6_aggregation():
    from webapp.server import _rate_identity

    assert _rate_identity("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert _rate_identity("2001:db8:1:2::9") == _rate_identity("2001:db8:1:2::1")
    assert _rate_identity("192.0.2.1") == "192.0.2.1"
    assert _rate_identity("unknown") == "unknown"
    # IPv4 映射的 IPv6 地址按真实 IPv4 聚合，不能都落进 ::/64 同一个桶
    assert _rate_identity("::ffff:203.0.113.5") == "203.0.113.5"
    assert _rate_identity("::ffff:203.0.113.5") != _rate_identity("::ffff:203.0.113.9")
    print("✓ IPv6 限流身份按 /64 聚合")


def test_import_with_root_path_uses_extended_limit(monkeypatch):
    """子路径部署：/aligo/api/import 仍拿 64MB 上限，no-store 与文档 CSP 正常。"""
    target = _unique_user("web_test_rootpath")
    monkeypatch.setenv("ALIGO_ENABLE_DOCS", "1")
    try:
        app = create_app(model=UsageTrackingModel(_FakeModel()))
        client = TestClient(app, root_path="/aligo")
        backup = {
            "backup": {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "x" * 2_500_000,
                        "session_id": "sess-root",
                    }
                ]
            }
        }
        response = client.post(
            "/aligo/api/import", params={"user_id": target}, json=backup
        )
        assert response.status_code == 200, response.text[:200]
        status = client.get("/aligo/api/status", params={"user_id": target})
        assert status.headers.get("cache-control") == "no-store"
        docs = client.get("/aligo/docs")
        assert docs.status_code == 200
        assert "cdn.jsdelivr.net" in docs.headers.get(
            "content-security-policy", ""
        )
        print("✓ root_path 下 /api/import 上限、no-store、文档 CSP")
    finally:
        _cleanup(target)


def test_gzip_middleware_excludes_sse_under_root_path():
    """root_path 子路径部署下 SSE 仍按归一化路径排除 gzip。"""
    import asyncio as _asyncio

    from webapp.server import SelectiveGZipMiddleware

    calls = []

    async def inner(scope, receive, send):
        calls.append("inner")

    async def compressed(scope, receive, send):
        calls.append("gzip")

    middleware = SelectiveGZipMiddleware(inner, minimum_size=1024)
    middleware.compressed_app = compressed  # type: ignore[assignment]
    scope = {"type": "http", "path": "/aligo/api/chat/stream", "root_path": "/aligo"}
    _asyncio.run(middleware(scope, None, None))
    assert calls == ["inner"], "SSE 在子路径部署下也必须跳过 gzip"

    calls.clear()
    scope = {"type": "http", "path": "/aligo/api/status", "root_path": "/aligo"}
    _asyncio.run(middleware(scope, None, None))
    assert calls == ["gzip"]
    print("✓ root_path 下 SSE 排除 gzip")


def test_root_path_chunked_413_metric_label(monkeypatch):
    """子路径部署下 chunked 超限仍按路由模板记 413 指标（不带前缀）。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app, root_path="/aligo")

    def body_chunks():
        chunk = b"x" * 65536
        for _ in range(40):  # 2.5MB，超过 2MB 通用上限
            yield chunk

    response = client.post(
        "/aligo/api/chat",
        content=body_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413, response.text[:200]
    by_path = REQUEST_METRICS.snapshot()["by_path"]
    assert by_path.get("/api/chat", 0) >= 1, by_path
    print("✓ root_path chunked 413 指标标签归一")


def test_import_trailing_slash_redirects(monkeypatch):
    """/api/import/ 应先 307 归一，而不是被 2MB 预检直接 413。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    client = TestClient(app, follow_redirects=False)
    small = {
        "backup": {
            "chat_history": [
                {"role": "user", "content": "hi", "session_id": "sess-slash"}
            ]
        }
    }
    response = client.post(
        "/api/import/",
        params={"user_id": _unique_user("web_test_slash")},
        json=small,
    )
    assert response.status_code == 307, response.text[:200]
    print("✓ 尾斜杠 /api/import/ 正常 307 重定向")


def test_early_413_metrics_labeled_as_import(monkeypatch):
    """路由前 413 与 chunked 超限的指标口径一致（记录为 /api/import）。"""
    from utils.metrics import REQUEST_METRICS
    from webapp import server as server_module

    REQUEST_METRICS.reset()
    monkeypatch.setitem(server_module._BODY_LIMIT_OVERRIDES, "/api/import", 1024)
    client = _client()
    try:
        response = client.post(
            "/api/import",
            params={"user_id": _unique_user("web_test_413")},
            content=b"x" * 2048,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        by_path = REQUEST_METRICS.snapshot()["by_path"]
        assert "/api/import" in by_path, by_path
        assert "__unmatched__" not in by_path
        print("✓ 路由前 413 的指标标签与路由模板一致")
    finally:
        REQUEST_METRICS.reset()


def test_pre_route_metric_labels_cover_read_routes():
    """读接口的尾斜杠重定向也按接口名归一，不混入 __unmatched__。"""
    from webapp import server as server_module

    assert server_module._metric_path_before_route("/api/status/") == "/api/status"
    assert (
        server_module._metric_path_before_route("/api/sessions/abc/export/")
        == "/api/sessions/{session_id}/export"
    )
    assert (
        server_module._metric_path_before_route("/api/trips/trip_1/calendar/")
        == "/api/trips/{trip_id}/calendar"
    )
    assert (
        server_module._metric_path_before_route(
            "/api/knowledge/documents/12/group/"
        )
        == "/api/knowledge/documents/{doc_id}/group"
    )
    assert (
        server_module._metric_path_before_route("/api/no-such-route/")
        == "__unmatched__"
    )
    print("✓ 路由前指标标签覆盖读接口与尾斜杠")


def test_knowledge_unavailable_maps_chinese_collection_marker():
    """半初始化时 RAG 智能体抛中文「缺少集合」：映射为可操作的 503。"""
    from webapp import server as server_module

    exc = server_module._knowledge_unavailable(
        "检索",
        RuntimeError("知识库缺少集合（milvus_lite.db 存在但尚未初始化知识）"),
    )
    assert exc.status_code == 503
    assert "初始化脚本" in exc.detail
    print("✓ 中文「缺少集合」映射为初始化提示")


def test_doctor_rate_limited(monkeypatch):
    """未鉴权的自检接口接入知识库桶限流。"""
    from webapp import server as server_module

    async def fake_run_doctor(include_network=False):
        return []

    monkeypatch.setattr(server_module, "run_doctor", fake_run_doctor)
    client = _client()
    first = client.get("/api/doctor")
    assert first.status_code == 200
    limit = first.headers.get("x-ratelimit-limit")
    remaining = first.headers.get("x-ratelimit-remaining")
    assert limit is not None and remaining is not None
    second = client.get("/api/doctor")
    assert int(second.headers["x-ratelimit-remaining"]) < int(remaining)
    print("✓ /api/doctor 接入限流桶")


def test_session_build_slots_bound_concurrent_connections(monkeypatch):
    """并发新会话的「连接构造」数量必须受会话上限约束。"""
    import threading as _threading
    import time as _time

    from webapp import server as server_module

    class _SlowSession:
        lock = _threading.Lock()
        active = 0
        peak = 0

        def __init__(self, user_id, session_id, model, breaker, persist_guard=None):
            with _SlowSession.lock:
                _SlowSession.active += 1
                _SlowSession.peak = max(_SlowSession.peak, _SlowSession.active)
            _time.sleep(0.01)
            with _SlowSession.lock:
                _SlowSession.active -= 1
            self.turns = 0

        def begin_turn(self):
            self.turns += 1

        def end_turn(self):
            self.turns = max(0, self.turns - 1)

        def in_use(self):
            return self.turns > 0

        def long_in_use(self, seconds):
            return False

        def close(self):
            pass

    monkeypatch.setattr(server_module, "_WebSession", _SlowSession)
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 5
    state._session_build_slots = _threading.BoundedSemaphore(5)
    errors = []

    def worker(index):
        try:
            state.get_session("u-slots", f"s{index}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [_threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert _SlowSession.peak <= 5, _SlowSession.peak
    state.close_all()
    print(f"✓ 并发会话构造峰值 {_SlowSession.peak} ≤ 上限 5")


def test_lock_for_overflow_waiters(monkeypatch):
    """兜底锁有等待者时，同一用户也必须继续共用兜底锁。"""
    import types as _types

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    fake_overflow = _types.SimpleNamespace(
        locked=lambda: False, _waiters=[object()]
    )
    monkeypatch.setattr(state, "overflow_lock", fake_overflow)
    assert state.lock_for("u-waiters") is fake_overflow
    print("✓ 兜底锁等待者期间保持按用户串行")


def test_metrics_streaming_and_sse_errors():
    """SSE 长连接不计入延迟分位；error 收尾的流有独立计数。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    try:
        REQUEST_METRICS.record(
            "POST", "/api/chat/stream", 200, 30000.0, streaming=True
        )
        data = REQUEST_METRICS.snapshot()
        assert data["requests_total"] == 1
        assert data["duration_samples"] == 0, "流式请求不应进入延迟样本"
        assert data["avg_duration_ms"] == 0.0 and data["max_duration_ms"] == 0.0

        REQUEST_METRICS.record_sse_error()
        assert REQUEST_METRICS.snapshot()["sse_errors"] == 1
        assert "aligo_sse_errors_total 1" in REQUEST_METRICS.render_prometheus()
        print("✓ SSE 不计延迟分位 / error 收尾独立计数")
    finally:
        REQUEST_METRICS.reset()


def test_health_reports_degraded_when_breaker_open(monkeypatch):
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    try:
        for _ in range(state.breaker.failure_threshold):
            state.breaker.record_failure()
        client = TestClient(app)
        data = client.get("/api/health").json()
        assert data["status"] == "degraded", data
        assert data["circuit_breaker"] == "open"
    finally:
        state.close_all()
    print("✓ 熔断打开时健康检查报 degraded")


def test_sse_error_increments_metrics(monkeypatch):
    """流式回合以 error 事件收尾时 sse_errors 计数增加。"""
    from utils.metrics import REQUEST_METRICS
    from agents.orchestration_agent import OrchestrationAgent

    async def boom(self, intention, on_event=None):
        raise RuntimeError("编排失败")

    monkeypatch.setattr(OrchestrationAgent, "reply", boom)
    REQUEST_METRICS.reset()
    client = _client()
    user_id = _unique_user("web_test_sse_err")
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "你好", "user_id": user_id, "session_id": "sse-err"},
        ) as response:
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert events[-1]["type"] == "error"
        assert REQUEST_METRICS.snapshot()["sse_errors"] == 1, [e["type"] for e in events]
        # 路由前 413 之外，SSE 请求本身仍计入请求数
        assert (
            REQUEST_METRICS.snapshot()["by_path"].get("/api/chat/stream", 0) == 1
        )
    finally:
        _cleanup(user_id)
        REQUEST_METRICS.reset()
    print("✓ SSE error 收尾计入独立指标")


def test_circuit_breaker_probe_owner_is_async_task(monkeypatch):
    """同一线程的不同 asyncio task 不能互相消费半开试探名额。"""
    import asyncio

    from utils.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_sec=0.01, half_open_successes=1
    )
    breaker.record_failure()  # CLOSED -> OPEN
    time_module = __import__("time")
    breaker._opened_at = time_module.monotonic() - 1.0  # noqa: SLF001

    async def scenario():
        assert breaker.allow_call() is True  # 当前 task 持有试探

        async def other_task():
            breaker.record_success()  # 其它 task：不得消费名额

        await asyncio.create_task(other_task())
        assert breaker.state.value == "half_open", "其它任务的成功不能关闭熔断"
        breaker.record_success()  # 持有者：计数并关闭
        assert breaker.state.value == "closed"

    asyncio.run(scenario())
    print("✓ 半开试探按 asyncio task 认属主")


def test_half_open_failure_freezes_failure_count(monkeypatch):
    """半开失败回到 OPEN 时失败数冻结在阈值（/metrics 与 HELP 文案一致）。"""
    from utils.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(
        failure_threshold=2, recovery_timeout_sec=0.0, half_open_successes=1
    )
    breaker.record_failure()
    breaker.record_failure()
    breaker._maybe_half_open()  # noqa: SLF001
    assert breaker.allow_call() is True
    breaker.record_failure()
    assert breaker.get_status()["failure_count"] == 2
    print("✓ 半开失败后失败数冻结在阈值")


def test_import_uses_dedicated_executor(monkeypatch):
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    try:
        executor = getattr(state, "import_executor", None)
        assert executor is not None
        assert executor._max_workers == 1
    finally:
        state.close_all()
    print("✓ 备份恢复使用专用执行器")


def test_knowledge_import_uses_dedicated_limiter(monkeypatch):
    """备份导入使用更宽松的独立限流桶（面板分批循环不再必然 429）。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.import_limiter = RateLimiter(limit=1, window_sec=60)
    monkeypatch.setattr(
        server_module,
        "import_documents",
        lambda documents, dry_run=False: {
            "imported": 0,
            "skipped": 0,
            "skipped_invalid": 0,
            "skipped_duplicate": 0,
            **({"would_import": 0} if dry_run else {}),
        },
    )
    client = TestClient(app)
    payload = {"documents": [{"content": "x"}]}
    first = client.post("/api/knowledge/import/preview", json=payload)
    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "1"
    second = client.post("/api/knowledge/import/preview", json=payload)
    assert second.status_code == 429
    assert "导入过于频繁" in second.json()["detail"]
    print("✓ 备份导入走独立限流桶")


def test_user_data_import_api_with_knowledge(monkeypatch):
    """一体化备份：/api/import 同时恢复知识片段；知识库失败不影响用户数据。"""
    from utils import knowledge_admin as admin_module

    target = _unique_user("web_test_import_kb")
    calls = []

    def fake_import(documents, dry_run=False, *, return_pieces=False):
        calls.append(documents)
        return {
            "imported": len(documents),
            "skipped": 0,
            "skipped_invalid": 0,
            "skipped_duplicate": 0,
        }

    monkeypatch.setattr(admin_module, "import_documents", fake_import)
    try:
        client = _client()
        backup = {
            "backup": {
                "user_id": "src",
                "knowledge": [{"content": "知识一", "metadata": {}}],
            }
        }
        response = client.post(
            "/api/import", params={"user_id": target}, json=backup
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["knowledge_imported"] == 1
        assert payload["ok"] is True
        assert calls and calls[0][0]["content"] == "知识一"

        def missing(documents, dry_run=False, *, return_pieces=False):
            raise FileNotFoundError("知识库未初始化")

        monkeypatch.setattr(admin_module, "import_documents", missing)
        response = client.post(
            "/api/import", params={"user_id": target}, json=backup
        )
        assert response.status_code == 200
        assert "knowledge_error" in response.json()
        assert response.json()["ok"] is False, "知识恢复失败不能报整体成功"
        print("✓ /api/import 一体化备份（知识片段恢复 / 失败上报）")
    finally:
        _cleanup(target)

def test_snippet_stripping_is_linear_and_link_equivalent():
    """片段清洗：链接剥离与正则等价，未闭合的 [ 不再二次方回溯。"""
    from webapp import server as server_module

    def reference(text: str) -> str:
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)

    cases = [
        "见 [差旅标准](https://example.com/a) 与 ![图](img.png)",
        "空链接 []() 与空图 ![](x)",
        "[嵌套 [括号](u)](v) 收尾",
        "[未闭合]( 说明与 [另一个",
        "](先闭合) [再开",
        "![说明](url) 普通文本 !x](y)",
    ]
    for text in cases:
        assert server_module._strip_markdown_links(text) == reference(text), text

    # 未闭合的 '[' 曾让正则在每个起点回溯到串尾（O(n²)）：20 万个也必须秒回
    adversarial = "[" * 200_000 + "needle"
    started = time.perf_counter()
    snippet = server_module._match_snippet(adversarial, "needle")
    elapsed = time.perf_counter() - started
    assert "needle" in snippet
    assert elapsed < 2.0, f"片段清洗耗时 {elapsed:.2f}s，疑似仍为二次方复杂度"

    started = time.perf_counter()
    server_module._strip_markdown_for_snippet("[" * 200_000 + "]" * 200_000)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"整段清洗耗时 {elapsed:.2f}s，疑似仍为二次方复杂度"
    print("✓ 搜索片段清洗与正则等价且线性耗时")


def test_read_rate_limit_headers_and_rejection():
    """面板数据读接口接入独立限流：额度头存在，超限 429 带 Retry-After。"""
    from utils.rate_limit import create_rate_limiter

    client = _client()
    state = client.app.state.aligo
    state.read_limiter = create_rate_limiter("read", 3, 60, None)
    user = _unique_user("web_read")
    try:
        first = client.get("/api/status", params={"user_id": user})
        assert first.status_code == 200
        assert first.headers.get("x-ratelimit-limit") == "3"
        assert int(first.headers.get("x-ratelimit-remaining", 3)) < 3
        # 导出接口同样计入读额度，返回 Response 的路径也要带回额度头
        exported = client.get(
            "/api/export", params={"user_id": user, "format": "json"}
        )
        assert exported.status_code == 200
        assert exported.headers.get("x-ratelimit-limit") == "3"
        assert (
            client.get("/api/status", params={"user_id": user}).status_code == 200
        )
        rejected = client.get("/api/status", params={"user_id": user})
        assert rejected.status_code == 429
        assert rejected.headers.get("retry-after")
    finally:
        _cleanup(user)
    print("✓ 面板读接口独立限流与额度头")


def test_read_rate_limit_covers_list_and_export_endpoints():
    """列表 / 导出接口共享同一读额度桶：任一漏掉限流第 5 次请求就会 200。"""
    from context.storage import create_long_term_memory
    from utils.rate_limit import create_rate_limiter

    client = _client()
    state = client.app.state.aligo
    state.read_limiter = create_rate_limiter("read_cover", 4, 60, None)
    user = _unique_user("web_read_cover")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        store.add_chat_message("user", "查天气", "s-read")
        trip_id = store.save_trip_history(
            {"origin": "北京", "destination": "上海", "start_date": "2026-12-01"}
        )
        assert trip_id
        close = getattr(store, "close", None)
        if callable(close):
            close()

        remaining = []
        for path, params in (
            ("/api/sessions", {"user_id": user}),
            ("/api/trips", {"user_id": user}),
            ("/api/sessions/export", {"user_id": user}),
            (f"/api/trips/{trip_id}/calendar", {"user_id": user}),
        ):
            response = client.get(path, params=params)
            assert response.status_code == 200, (path, response.text)
            assert response.headers.get("x-ratelimit-limit") == "4", path
            remaining.append(response.headers.get("x-ratelimit-remaining"))
        assert remaining == ["3", "2", "1", "0"], remaining

        rejected = client.get("/api/sessions", params={"user_id": user})
        assert rejected.status_code == 429
        assert rejected.headers.get("retry-after")
        assert rejected.headers.get("x-ratelimit-remaining") == "0"
    finally:
        _cleanup(user)
    print("✓ 列表 / 导出接口共享读限流额度")


def test_kb_import_runs_in_dedicated_executor(monkeypatch):
    """知识库导入在专用执行器里跑，不占用面板接口共享的线程池。"""
    from webapp import server as server_module

    names: list[str] = []

    def fake_import(documents, dry_run=False):
        names.append(threading.current_thread().name)
        return {"imported": len(documents), "skipped": 0, "would_import": len(documents)}

    monkeypatch.setattr(server_module, "import_documents", fake_import)
    client = _client()
    response = client.post(
        "/api/knowledge/import/preview", json={"documents": [{"content": "知识"}]}
    )
    assert response.status_code == 200
    assert names and names[0].startswith("aligo-kb-import"), names
    print("✓ 知识库导入在专用执行器执行")


def test_kb_import_write_runs_in_dedicated_executor(monkeypatch):
    """非预览的知识库写库端点同样在 kb-import 专用执行器里跑。"""
    from webapp import server as server_module

    names: list[str] = []

    def fake_import(documents, dry_run=False):
        names.append(threading.current_thread().name)
        return {"imported": len(documents), "skipped": 0, "would_import": len(documents)}

    monkeypatch.setattr(server_module, "import_documents", fake_import)
    client = _client()
    response = client.post(
        "/api/knowledge/import", json={"documents": [{"content": "知识"}]}
    )
    assert response.status_code == 200, response.text
    assert names and names[0].startswith("aligo-kb-import"), names
    print("✓ 知识库写库导入在专用执行器执行")


def test_import_body_parsed_off_event_loop(monkeypatch):
    """/api/import 的 64MB 请求体解析与校验不能在事件循环线程同步执行。"""
    from utils import user_import as user_import_module
    from webapp import server as server_module

    threads: list[str] = []

    class _StubPayload:
        backup: dict = {}

    class _StubImportPayload:
        @staticmethod
        def model_validate_json(raw):
            threads.append(threading.current_thread().name)
            return _StubPayload()

    monkeypatch.setattr(server_module, "UserImportPayload", _StubImportPayload)
    monkeypatch.setattr(
        user_import_module,
        "parse_user_export_data",
        lambda backup, stats_out=None: (
            {"messages": [], "titles": {}, "pinned": [], "archived": [], "trips": []},
            [],
        ),
    )
    monkeypatch.setattr(
        user_import_module,
        "import_user_data",
        lambda store, sections, **kwargs: {"imported": 0, "skipped": 0},
    )
    client = _client()
    user = _unique_user("web_import_thread")
    try:
        response = client.post(
            "/api/import", params={"user_id": user}, json={"backup": {}}
        )
        assert response.status_code == 200
        assert threads and threads[0].startswith("aligo-import"), threads
    finally:
        _cleanup(user)
    print("✓ /api/import 请求体解析在导入执行器线程完成")


def test_import_rate_limited_before_body_parsing(monkeypatch):
    """/api/import 先限流再读 / 解析请求体：超限风暴不付出备份解析代价。"""
    from utils.rate_limit import create_rate_limiter
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    state.kb_limiter = create_rate_limiter("kb_import_order_test", 1, 60, None)
    user = _unique_user("web_import_rate")
    backup = {
        "backup": {
            "chat_history": [
                {"role": "user", "content": "hi", "session_id": "s-rate"}
            ]
        }
    }
    try:
        allowed = client.post(
            "/api/import", params={"user_id": user, "dry_run": "1"}, json=backup
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.headers.get("x-ratelimit-remaining") == "0"

        class _NeverParse:
            @staticmethod
            def model_validate_json(raw):  # noqa: ANN001 - 测试桩
                raise AssertionError("超限请求不应读取 / 解析请求体")

        monkeypatch.setattr(server_module, "UserImportPayload", _NeverParse)
        rejected = client.post(
            "/api/import",
            params={"user_id": user},
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert rejected.status_code == 429, rejected.text
        assert rejected.headers.get("retry-after")
        assert rejected.headers.get("x-ratelimit-remaining") == "0"
    finally:
        _cleanup(user)
    print("✓ /api/import 限流先于请求体解析")


def test_import_rejects_malformed_body_with_422():
    """手工解析请求体保持 422 语义：坏 JSON / 缺 backup 字段都不 500。"""
    client = _client()
    user = _unique_user("web_import_bad")
    try:
        bad = client.post(
            "/api/import",
            params={"user_id": user},
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert bad.status_code == 422
        detail = bad.json()["detail"]
        assert isinstance(detail, list) and detail
        missing = client.post(
            "/api/import", params={"user_id": user}, json={"other": 1}
        )
        assert missing.status_code == 422
    finally:
        _cleanup(user)
    print("✓ /api/import 坏请求体返回 422")


def test_weather_city_rejects_control_characters(monkeypatch):
    """/api/weather 的 city 拒绝控制字符：%0A 不再能伪造日志行。"""
    from webapp import server as server_module

    client = _client()
    rejected = client.get("/api/weather", params={"city": "北京\n伪造日志行"})
    assert rejected.status_code == 422
    called = {}

    def fake_fetch(name):
        called["name"] = name
        return {"city": name}

    monkeypatch.setattr(server_module, "fetch_weather", fake_fetch)
    ok = client.get("/api/weather", params={"city": "北京"})
    assert ok.status_code == 200
    assert called["name"] == "北京"
    print("✓ /api/weather 城市名拒绝控制字符")

def test_web_session_persist_guard_tracks_deletions():
    """Web 会话的落库守卫反映墓碑状态（写入锁内阻止写回已删除会话）。"""
    client = _client()
    state = client.app.state.aligo
    user = _unique_user("web_guard")
    session = state.get_session(user, "s-guard")
    guard = session.runtime._persist_guard
    assert callable(guard), "Web 会话必须注入落库守卫"
    assert guard() is True, "未删除的会话应允许落库"
    token = state.mark_deleted(user, "s-guard")
    assert guard() is False, "墓碑生效后守卫必须否决落库"
    state.unmark_deleted(user, "s-guard", token)
    assert guard() is True
    state.drop_session(user, "s-guard")
    _cleanup(user)
    print("✓ Web 会话落库守卫跟随墓碑状态")


def test_session_capacity_rejects_when_all_in_use():
    """全部会话都在用时不再新建会话（每个会话常驻一条数据库连接）。"""
    from webapp.server import SessionCapacityError

    client = _client()
    state = client.app.state.aligo
    state.max_sessions = 2
    user = _unique_user("web_capacity")
    first = state.get_session(user, "s1", True)
    state.get_session(user, "s2", True)
    try:
        with pytest.raises(SessionCapacityError):
            state.get_session(user, "s3", True)
        # 有会话结束（空闲可淘汰）后可以继续新建
        first.end_turn()
        third = state.get_session(user, "s3", True)
        assert third is not None and third.session_id == "s3"
    finally:
        state.close_all()
        _cleanup(user)
    print("✓ 会话容量上限：全部在用拒绝、有空闲即恢复")


def test_chat_maps_session_capacity_to_503(monkeypatch):
    """/api/chat 与 SSE 把会话容量拒绝映射为可重试的 503。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user = _unique_user("web_capacity_api")

    def capacity(*args, **kwargs):
        raise server_module.SessionCapacityError("并发会话过多（上限 2）")

    monkeypatch.setattr(state, "get_session", capacity)
    response = client.post(
        "/api/chat",
        json={"message": "你好", "user_id": user, "session_id": "s1"},
    )
    assert response.status_code == 503
    assert "并发会话过多" in response.json()["detail"]
    stream = client.post(
        "/api/chat/stream",
        json={"message": "你好", "user_id": user, "session_id": "s1"},
    )
    assert stream.status_code == 503
    assert "并发会话过多" in stream.json()["detail"]
    _cleanup(user)
    print("✓ 会话容量拒绝映射 503（普通与流式）")


def test_write_endpoints_reject_unwritable_text():
    """实时写接口拒绝 NUL / 孤立代理项（PG text/JSONB 与 UTF-8 写盘都会失败）。"""
    client = _client()
    user = _unique_user("web_text_guard")
    try:
        response = client.post(
            "/api/chat",
            json={"message": "a\u0000b", "user_id": user, "session_id": "s1"},
        )
        assert response.status_code == 422
        response = client.put(
            "/api/preferences/home_location",
            json={"user_id": user, "value": "a\u0000b"},
        )
        assert response.status_code == 422
        response = client.put(
            "/api/trips/trip_1",
            json={"user_id": user, "origin": "a\u0000b"},
        )
        assert response.status_code == 422
        response = client.put(
            "/api/sessions/s1/title",
            json={"user_id": user, "title": "a\u0000b"},
        )
        assert response.status_code == 422
        # 孤立代理项（JSON 里以 \ud800 转义提交）同样拒绝
        response = client.post(
            "/api/chat",
            content=b'{"message": "a\\ud800b", "user_id": "u1", "session_id": "s1"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
    finally:
        _cleanup(user)
    print("✓ 实时写接口拒绝 NUL / 孤立代理项")
def test_read_rate_limit_covers_export_and_detail_endpoints():
    """行程导出 / 日历 / 会话详情同样计入读接口限流桶。"""
    from utils.rate_limit import create_rate_limiter

    client = _client()
    state = client.app.state.aligo
    state.read_limiter = create_rate_limiter("read", 3, 60, None)
    user = _unique_user("web_read_more")
    try:
        first = client.get("/api/trips/export", params={"user_id": user})
        assert first.status_code == 200
        assert first.headers.get("x-ratelimit-limit") == "3"
        second = client.get("/api/trips/calendar", params={"user_id": user})
        assert second.status_code == 200
        assert second.headers.get("x-ratelimit-limit") == "3"
        third = client.get("/api/sessions/sess-read", params={"user_id": user})
        assert third.status_code == 200
        rejected = client.get(
            "/api/sessions/sess-read/export", params={"user_id": user}
        )
        assert rejected.status_code == 429
        assert rejected.headers.get("retry-after")
    finally:
        _cleanup(user)
    print("✓ 读接口限流覆盖行程导出 / 日历 / 会话详情")


def test_import_validation_error_is_trimmed():
    """手工解析的 422 与全局处理器同口径：input 截断，NaN 不变成 500。"""
    client = _client()
    user = _unique_user("web_import_trim")
    try:
        big = {"data": "x" * 2_000_000}  # 无 backup 键：校验失败
        response = client.post("/api/import", params={"user_id": user}, json=big)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list) and detail
        assert len(response.content) < 5000, len(response.content)
        raw = detail[0].get("input", "")
        assert len(raw) <= 201, len(raw)
        nan = client.post(
            "/api/import",
            params={"user_id": user},
            content=b'{"backup": NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert nan.status_code == 422, nan.text
    finally:
        _cleanup(user)
    print("✓ /api/import 校验错误截断且 NaN 不 500")


def test_import_backpressure_rejects_when_busy(monkeypatch):
    """导入排队已满时返回 503 背压，不再读入整份请求体。"""
    import threading as _threading

    client = _client()
    state = client.app.state.aligo
    state.import_queue_slots = _threading.BoundedSemaphore(1)
    assert state.import_queue_slots.acquire(blocking=False)
    try:
        response = client.post(
            "/api/import",
            params={"user_id": _unique_user("web_import_bp")},
            json={"backup": {}},
        )
        assert response.status_code == 503
        assert "已有导入任务" in response.json()["detail"]
    finally:
        state.import_queue_slots.release()
    print("✓ 导入排队已满返回 503 背压")


def test_import_rechecks_tombstone_before_write(monkeypatch):
    """排队期间的删除在写库前复查：已删除会话不会被恢复写回。"""
    client = _client()
    state = client.app.state.aligo
    user = _unique_user("web_import_recheck")
    calls = {"n": 0}

    def fake_deleted(uid, sid):
        if sid != "s-late":
            return False
        calls["n"] += 1
        return calls["n"] >= 2  # 快照放行，写库前的复查拒绝

    monkeypatch.setattr(state, "is_recently_deleted", fake_deleted)
    backup = {
        "chat_history": [
            {
                "role": "user",
                "content": "迟到删除",
                "timestamp": "2026-11-01T09:00:00",
                "session_id": "s-late",
            },
            {
                "role": "user",
                "content": "正常",
                "timestamp": "2026-11-01T09:01:00",
                "session_id": "s-ok",
            },
        ]
    }
    try:
        response = client.post(
            "/api/import", params={"user_id": user}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert any("排队期间被删除" in item for item in body.get("skipped") or []), body
        sessions = client.get("/api/sessions", params={"user_id": user}).json()[
            "sessions"
        ]
        ids = [item["session_id"] for item in sessions]
        assert "s-ok" in ids and "s-late" not in ids, ids
    finally:
        _cleanup(user)
    print("✓ 导入写库前复查墓碑")


def test_session_capacity_precheck_skips_construction(monkeypatch):
    """容量拒绝在构造会话之前判定（不再为注定 503 的请求建数据库连接）。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    state.max_sessions = 1

    class _Busy:
        def in_use(self):
            return True

        def long_in_use(self, seconds):
            return False

    state.sessions[state._session_key("u-pre", "s1")] = _Busy()  # type: ignore[assignment]

    def explode(*args, **kwargs):
        raise AssertionError("容量拒绝前不应构造会话")

    monkeypatch.setattr(server_module, "_WebSession", explode)
    with pytest.raises(server_module.SessionCapacityError):
        state.get_session("u-pre", "s2", True)
    print("✓ 容量拒绝在构造会话之前判定")


def test_session_detail_sanitizes_legacy_nan_meta():
    """旧数据 _meta 里的 NaN 不再让会话详情 500（归一为 null）。"""
    from context.storage import create_long_term_memory

    client = _client()
    user = _unique_user("web_meta_nan")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "hi", "s-nan")
            # 直接落库一条含 NaN usage.time 的助手消息（绕过导入校验，
            # 模拟旧版本 / 手写记忆文件）
            store.add_chat_message(
                "assistant",
                '{"results": [], "_meta": {"usage": {"time": NaN}}}',
                "s-nan",
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.get("/api/sessions/s-nan", params={"user_id": user})
        assert response.status_code == 200, response.text
        meta = response.json()["messages"][-1]["meta"]
        assert meta is None or meta.get("usage", {}).get("time") is None
    finally:
        _cleanup(user)
    print("✓ 会话详情对旧数据 NaN _meta 归一")


def test_session_export_html_format():
    """单会话导出支持 format=html（与 CLI session-export --html 同口径）。"""
    from context.storage import create_long_term_memory

    client = _client()
    user = _unique_user("web_session_html")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "去北京出差", "s-html")
            store.add_chat_message("assistant", "好的，已安排", "s-html")
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        renamed = client.put(
            "/api/sessions/s-html/title",
            json={"user_id": user, "title": "<b>标题</b>"},
        )
        assert renamed.status_code == 200, renamed.text
        response = client.get(
            "/api/sessions/s-html/export",
            params={"user_id": user, "format": "html"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html")
        assert ".html" in response.headers["content-disposition"]
        # 标题在出口单次转义：不能原样落 <b>，也不能双重转义成 &amp;lt;
        assert "&lt;b&gt;标题&lt;/b&gt;" in response.text
        assert "<b>标题</b>" not in response.text
        assert "去北京出差" in response.text
        invalid = client.get(
            "/api/sessions/s-html/export",
            params={"user_id": user, "format": "pdf"},
        )
        assert invalid.status_code == 422
        # 默认仍是 Markdown
        markdown = client.get(
            "/api/sessions/s-html/export", params={"user_id": user}
        )
        assert markdown.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
    finally:
        _cleanup(user)
    print("✓ 单会话导出支持 HTML（标题单次转义 / 格式校验）")


def test_export_with_knowledge(monkeypatch):
    """with_knowledge=1 把知识片段写入备份（与 CLI --with-knowledge 同字段）。"""
    from webapp import server as server_module

    client = _client()
    user = _unique_user("web_export_kb")
    try:
        monkeypatch.setattr(
            server_module,
            "export_documents",
            lambda: [{"content": "差旅标准", "metadata": {"category": "制度"}}],
        )
        response = client.get(
            "/api/export", params={"user_id": user, "with_knowledge": "1"}
        )
        assert response.status_code == 200
        # 未降级时不得出现省略提示头（absence 锚点，防止降级判断误伤正常导出）
        assert response.headers.get("x-knowledge-omitted") is None
        backup = response.json()
        assert backup["knowledge"] == [
            {"content": "差旅标准", "metadata": {"category": "制度"}}
        ]
        # 默认不带知识段（不触发全库扫描）
        plain = client.get("/api/export", params={"user_id": user}).json()
        assert "knowledge" not in plain

        def boom():
            raise FileNotFoundError("知识库未初始化")

        monkeypatch.setattr(server_module, "export_documents", boom)
        failed = client.get(
            "/api/export",
            params={"user_id": user, "with_knowledge": "1"},
        )
        assert failed.status_code == 503
        assert "知识库导出失败" in failed.json()["detail"]
    finally:
        _cleanup(user)
    print("✓ 备份导出可携带知识库片段")


def test_export_with_knowledge_failure_modes(monkeypatch):
    """知识库导出异常映射 503（无初始化提示）；format=md 提前返回不扫描知识库。"""
    from webapp import server as server_module

    client = _client()
    user = _unique_user("web_export_kb_fail")
    try:
        def boom():
            raise RuntimeError("milvus down")

        monkeypatch.setattr(server_module, "export_documents", boom)
        failed = client.get(
            "/api/export", params={"user_id": user, "with_knowledge": "1"}
        )
        assert failed.status_code == 503, failed.text
        detail = failed.json()["detail"]
        assert "知识库导出失败" in detail
        assert "先运行初始化脚本" not in detail, "非未初始化异常不应提示初始化脚本"

        calls = []

        def counting_boom():
            calls.append(1)
            raise RuntimeError("不该被调用")

        monkeypatch.setattr(server_module, "export_documents", counting_boom)
        markdown = client.get(
            "/api/export",
            params={"user_id": user, "format": "md", "with_knowledge": "1"},
        )
        assert markdown.status_code == 200, markdown.text
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert ".md" in markdown.headers["content-disposition"]
        assert calls == [], "format=md 必须在知识库扫描之前返回"
    finally:
        _cleanup(user)
    print("✓ 知识库导出失败映射与非 JSON 格式提前返回")


def test_export_with_knowledge_round_trips(monkeypatch):
    """导出的 knowledge 段可直接被 /api/import 一体化恢复。"""
    from utils import knowledge_admin as admin_module
    from webapp import server as server_module

    client = _client()
    user = _unique_user("web_export_kb_rt")
    try:
        docs = [{"content": "差旅标准", "metadata": {"category": "制度"}}]
        monkeypatch.setattr(server_module, "export_documents", lambda: docs)
        backup = client.get(
            "/api/export", params={"user_id": user, "with_knowledge": "1"}
        ).json()
        imported = []
        def fake_import(documents, dry_run=False, *, return_pieces=False):
            result = {"imported": len(documents), "skipped": 0}
            if dry_run:
                result["would_import"] = len(documents)
                if return_pieces:
                    result["pieces"] = [dict(item) for item in documents]
                return result
            imported.extend(documents)
            return result

        monkeypatch.setattr(admin_module, "import_documents", fake_import)
        response = client.post(
            "/api/import", params={"user_id": user}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        assert imported == docs, imported
    finally:
        _cleanup(user)
    print("✓ 备份导出的知识段可一体化恢复")


def test_match_snippet_handles_casefold_length_changes():
    """İ 小写后长度 +1：命中下标必须仍按原文定位（窗口不错位成空片段）。"""
    from webapp import server as server_module

    text = "İ" * 500 + "needle"
    snippet = server_module._match_snippet(text, "needle")
    assert "needle" in snippet
    # 常规大小写不敏感匹配不回归（片段保留原文大小写）
    assert "needle" in server_module._match_snippet("Some NEEDLE here", "needle").lower()


def test_ci_find_caseless_fast_path_skips_regex(monkeypatch):
    """无大小写区分的关键字（中文 / 数字 / 符号）直接精确查找，不走正则回退。

    会话搜索逐条扫描时，每条未命中消息的 re.escape+re.search 是主要开销
    （2 万条无命中搜索实测约 100ms）：无大小写关键字的忽略大小写语义与
    精确查找等价，必须走 C 层 str.find；有大小写差异的关键字保持原路径。
    """
    from webapp import server as server_module

    assert server_module._ci_find("这里有问题在这里", "问题") == 3
    assert server_module._ci_find("完全没有匹配", "英文") == -1

    def _boom(*args, **kwargs):
        raise AssertionError("无大小写关键字不应走 re.search")

    monkeypatch.setattr(server_module.re, "search", _boom)
    assert server_module._ci_find("大段中文没有关键字", "关键词") == -1
    assert server_module._ci_find("abc123", "--") == -1
    monkeypatch.undo()

    # 大小写不敏感路径不回归（英文 / 土耳其语 İ 长度变化仍按原文定位）
    assert server_module._ci_find("Some NEEDLE here", "needle") == 5
    assert server_module._ci_find("İstanbul", "istanbul") == 0
    assert server_module._ci_find("Hello", "") == -1


def test_session_detail_sanitizes_surrogate_meta():
    """备份中以转义夹带的孤立代理项不再让会话详情 500。"""
    from context.storage import create_long_term_memory

    client = _client()
    user = _unique_user("web_meta_sur")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "hi", "s-sur")
            # 落库内容里的 \ud800 是 JSON 转义文本：读取侧 json.loads 会把它
            # 解码成真正的孤立代理项，响应序列化必须转义而不是 500
            store.add_chat_message(
                "assistant",
                '{"results": [], "_meta": {"note": "x\\ud800y"}}',
                "s-sur",
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.get("/api/sessions/s-sur", params={"user_id": user})
        assert response.status_code == 200, response.text
        note = response.json()["messages"][-1]["meta"]["note"]
        assert "ud800" in note.lower(), note
    finally:
        _cleanup(user)
    print("✓ 会话详情对孤立代理项 _meta 转义")


def test_kb_import_backpressure_rejects_when_busy():
    """知识库导入排队已满时返回 503（无界队列不再无限驻留载荷）。"""
    import threading as _threading

    client = _client()
    state = client.app.state.aligo
    state.kb_import_queue_slots = _threading.BoundedSemaphore(1)
    original_import_limit = state.import_limiter.limit
    state.import_limiter.limit = 1
    assert state.kb_import_queue_slots.acquire(blocking=False)
    try:
        response = client.post(
            "/api/knowledge/import", json={"documents": [{"content": "x"}]}
        )
        assert response.status_code == 503
        assert "已有知识库导入" in response.json()["detail"]
        assert state.import_limiter.status("testclient")[0] == 1, (
            "槽位繁忙的零成本 503 必须归还导入桶额度"
        )
        preview = client.post(
            "/api/knowledge/import/preview", json={"documents": [{"content": "x"}]}
        )
        assert preview.status_code == 503
        assert state.import_limiter.status("testclient")[0] == 1, (
            "预览同样归还导入桶额度"
        )
    finally:
        state.kb_import_queue_slots.release()
        state.import_limiter.limit = original_import_limit
    print("✓ 知识库导入排队已满返回 503 且归还导入额度")


def test_weather_cache_hit_skips_rate_limit(monkeypatch):
    """缓存命中不产生上游请求：不消耗天气 IP 桶额度；未命中仍受限流保护。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    cached_payload = {"city": "北京", "current": {"description": "晴"}, "daily": []}
    monkeypatch.setattr(
        server_module,
        "peek_cached_weather",
        lambda city: dict(cached_payload, city=city),
    )
    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda city: {"city": city, "current": {}, "daily": []},
    )
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    app.state.aligo.weather_limiter = RateLimiter(limit=1, window_sec=60)
    client = TestClient(app)

    # 两次都命中缓存：即便额度只有 1，也不应 429（缓存不耗额度）
    for _ in range(2):
        response = client.get("/api/weather", params={"city": "北京"})
        assert response.status_code == 200, response.text

    # 缓存未命中：恢复按 IP 限流，第二次 429
    monkeypatch.setattr(server_module, "peek_cached_weather", lambda city: None)
    assert client.get("/api/weather", params={"city": "上海"}).status_code == 200
    limited = client.get("/api/weather", params={"city": "广州"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    print("✓ 天气缓存命中不消耗 IP 桶额度")

def test_chat_lock_wait_is_bounded(monkeypatch):
    """SSE 排队等待用户锁有上限：期间探测断连，超时 503 + Retry-After。"""
    import asyncio

    from fastapi import HTTPException
    from webapp import server as server_module

    class _Request:
        def __init__(self, disconnected: bool = False) -> None:
            self._disconnected = disconnected

        async def is_disconnected(self) -> bool:
            return self._disconnected

    # 超时必须显式传参：_acquire_chat_lock 的默认值在模块导入时已绑定，
    # 给模块常量打补丁不会生效（此前该用例实际等满生产超时 ~50 秒）
    monkeypatch.setattr(server_module, "_CHAT_LOCK_POLL_SEC", 0.01)

    async def _timed_out():
        lock = asyncio.Lock()
        await lock.acquire()
        try:
            await server_module._acquire_chat_lock(
                _Request(), lock, "rid", timeout=0.05
            )
        finally:
            lock.release()

    with pytest.raises(HTTPException) as info:
        asyncio.run(_timed_out())
    assert info.value.status_code == 503
    assert (info.value.headers or {}).get("Retry-After") == "5"
    assert "已有对话" in info.value.detail

    async def _disconnected():
        lock = asyncio.Lock()
        await lock.acquire()
        try:
            await server_module._acquire_chat_lock(
                _Request(disconnected=True), lock, "rid", timeout=0.05
            )
        finally:
            lock.release()

    with pytest.raises(HTTPException) as info:
        asyncio.run(_disconnected())
    assert "已断开" in info.value.detail

    async def _free_lock():
        lock = asyncio.Lock()
        await server_module._acquire_chat_lock(_Request(), lock, "rid")
        assert lock.locked()
        lock.release()

    asyncio.run(_free_lock())
    print("✓ SSE 对话锁等待有界并探测断连")

def test_open_long_term_refuses_quarantined_memory(monkeypatch, tmp_path):
    """JSON 记忆被隔离后 Web 打开存储必须 503，不能当作空数据继续；
    PostgreSQL 后端不受同目录遗留的 .corrupt-* 文件影响。"""
    from config import STORAGE_CONFIG
    from fastapi import HTTPException
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)

    class _JsonStore:
        db_path = str(tmp_path / "u-quarantine.json")

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _JsonStore(),
    )
    # 主文件已被存储层移走，只剩隔离备份：/api/export 之前会返回 200 空备份
    (tmp_path / "u-quarantine.json.corrupt-20260101000000000000-1").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(HTTPException) as info:
        server_module._open_long_term("u-quarantine")
    assert info.value.status_code == 503
    assert "隔离" in info.value.detail

    # PostgreSQL 后端（无 db_path）不回退扫描 JSON 目录：遗留隔离文件
    # 与该账号无关，扫描会让其所有请求永久 503 且导入恢复永远清不掉
    class _PgStore:
        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _PgStore(),
    )
    assert server_module._open_long_term("u-quarantine") is not None

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _JsonStore(),
    )
    # 用户用备份恢复出主文件后不再拦截
    (tmp_path / "u-quarantine.json").write_text("{}", encoding="utf-8")
    assert server_module._open_long_term("u-quarantine") is not None
    print("✓ 记忆文件隔离后 Web 接口拒绝空数据（仅 JSON 后端）")


def test_persistence_failure_detail_keeps_quarantine_hint():
    """隔离类持久化失败保留恢复引导文案（与 _open_long_term 的 503 同口径）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    quarantined = server_module._persistence_failure_detail(
        MemoryPersistenceError("记忆文件损坏并已隔离备份"), "rid-1"
    )
    assert "隔离" in quarantined and "恢复" in quarantined, quarantined
    assert "rid-1" in quarantined
    generic = server_module._persistence_failure_detail(
        MemoryPersistenceError("长期记忆写入失败"), "rid-2"
    )
    assert "存储暂时不可用" in generic and "隔离" not in generic, generic
    reload_failed = server_module._persistence_failure_detail(
        MemoryPersistenceError("长期记忆重载失败（data/memory/u1.json）：bad"),
        "rid-3",
    )
    # 未隔离、没有备份可恢复：不能引导用户去找不存在的备份文件
    assert "损坏" in reload_failed and "备份文件恢复" not in reload_failed, (
        reload_failed
    )
    cannot_backup = server_module._persistence_failure_detail(
        MemoryPersistenceError("记忆文件损坏且无法备份，已拒绝覆盖写入"),
        "rid-4",
    )
    assert "损坏" in cannot_backup and "备份文件恢复" not in cannot_backup, (
        cannot_backup
    )
    # 隔离标记最终写不进去（损坏文件已还原原位）：同属「损坏且未能隔离
    # 备份」，不能降级成可重试的通用 503
    marker_failed = server_module._persistence_failure_detail(
        MemoryPersistenceError(
            "记忆文件损坏且无法记录隔离标记，已拒绝以空快照启动（u1.json）"
        ),
        "rid-5",
    )
    assert "损坏" in marker_failed, marker_failed
    assert "存储暂时不可用" not in marker_failed, marker_failed
    assert "备份文件恢复" not in marker_failed, marker_failed
    # 导入互斥锁冲突：可重试，但保留可行动的原因
    import_busy = server_module._persistence_failure_detail(
        MemoryPersistenceError("另一个导入正在进行，请稍后重试：锁被占用"),
        "rid-6",
    )
    assert "另一个导入正在进行" in import_busy, import_busy
    assert "存储暂时不可用" not in import_busy, import_busy


def test_persistence_detail_prefix_only():
    """消息中间含「隔离」的普通错误不误报为记忆损坏（前缀判定）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    detail = server_module._persistence_failure_detail(
        MemoryPersistenceError("写入隔离目录失败"), "rid-3"
    )
    assert "存储暂时不可用" in detail, detail


def test_pending_quarantine_message_has_no_backup_hint(monkeypatch, tmp_path):
    """pending（隔离改名未完成、无备份文件）的 503 文案不引导寻找备份。"""
    from config import STORAGE_CONFIG
    from fastapi import HTTPException
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)

    class _PendingStore:
        def get_fresh_quarantine_backup(self):
            return None

        def has_pending_corrupt_quarantine(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _PendingStore(),
    )
    with pytest.raises(HTTPException) as info:
        server_module._open_long_term("u-pending-msg")
    detail = info.value.detail
    assert info.value.status_code == 503
    assert "尚未完成" in detail and "备份文件恢复" not in detail, detail

    pending_detail = server_module._persistence_failure_detail(
        server_module.MemoryPersistenceError(detail), "rid-p"
    )
    assert "备份文件恢复" not in pending_detail, pending_detail


def test_quarantine_marker_fresh_and_pending_unconditional(monkeypatch):
    """本实例当场检测（fresh）/ 待复核（pending）在任何 DSN 配置下都拦截；
    历史遗留备份才受 PG 门禁。"""
    from config import STORAGE_CONFIG
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://x/y")

    class _FreshStore:
        def get_fresh_quarantine_backup(self):
            return "u.json.corrupt-1"

    assert server_module._quarantine_marker_for("u", _FreshStore())

    class _PendingStore:
        def get_fresh_quarantine_backup(self):
            return None

        def has_pending_corrupt_quarantine(self):
            return True

    assert server_module._quarantine_marker_for("u", _PendingStore())

    class _LeftoverStore:
        def get_recovered_corrupt_backup(self):
            return "u.json.corrupt-1"

    assert server_module._quarantine_marker_for("u", _LeftoverStore()) is None


def test_cached_session_quarantined_is_evicted_and_closed(monkeypatch, tmp_path):
    """缓存会话在隔离发生后被驱逐并立即关闭（构造守卫随后 503）。"""
    from config import STORAGE_CONFIG
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    import threading
    from collections import OrderedDict

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    decoupled_dir = tmp_path / "global-memory"
    decoupled_dir.mkdir()
    monkeypatch.setattr(server_module, "MEMORY_DIR", decoupled_dir)
    monkeypatch.setattr(server_module, "wrap_usage_tracking", lambda m: m)

    class _StubApp:
        _session_key = staticmethod(server_module.AligoWebApp._session_key)

        def __init__(self):
            self._sessions_lock = threading.Lock()
            self.sessions = OrderedDict()
            self.max_sessions = 1
            self.session_turn_leak_sec = 60.0
            self._session_build_slots = threading.Semaphore(1)
            self.model = None
            self.breaker = None
            self.is_recently_deleted = lambda user, session: False

    app_state = _StubApp()
    closed = []

    class _FakeLongTerm:
        db_path = str(tmp_path / "u-cache.json")

        def get_recovered_corrupt_backup(self):
            return None

    class _FakeMemory:
        long_term = _FakeLongTerm()

    class _FakeRuntime:
        memory = _FakeMemory()

    class _FakeSession:
        runtime = _FakeRuntime()

        def close(self):
            closed.append(True)

    class _BoomSession:
        def __init__(self, *args, **kwargs):
            raise MemoryPersistenceError("记忆文件损坏并已隔离备份")

    (tmp_path / "u-cache.json.corrupt-20260101000000000000-1").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(server_module, "_WebSession", _BoomSession)
    key = app_state._session_key("u-cache", "s1")
    app_state.sessions[key] = _FakeSession()
    try:
        with pytest.raises(MemoryPersistenceError):
            server_module.AligoWebApp.get_session(
                app_state, "u-cache", "s1"
            )
    finally:
        app_state.sessions.pop(key, None)
    assert closed == [True], "被驱逐的隔离会话必须立即关闭"


def test_quarantine_probe_runs_outside_sessions_lock(monkeypatch):
    """缓存命中的隔离探测（文件系统扫描）必须在 _sessions_lock 之外执行：
    锁内的慢 stat 会把所有用户的会话查找串行阻塞。"""
    import threading
    from collections import OrderedDict

    from webapp import server as server_module

    monkeypatch.setattr(server_module, "wrap_usage_tracking", lambda m: m)

    class _StubApp:
        _session_key = staticmethod(server_module.AligoWebApp._session_key)

        def __init__(self):
            self._sessions_lock = threading.Lock()
            self.sessions = OrderedDict()
            self.max_sessions = 4
            self.session_turn_leak_sec = 60.0
            self._session_build_slots = threading.Semaphore(1)
            self.model = None
            self.breaker = None
            self.is_recently_deleted = lambda user, session: False

        def _evict_over_limit_locked(self, key, evicted):
            pass

    app_state = _StubApp()

    class _FakeSession:
        def close(self):
            pass

        def begin_turn(self):
            pass

        def in_use(self):
            return False

    probed = []

    def probe(user_id, long_term):
        probed.append(app_state._sessions_lock.locked())
        return None

    monkeypatch.setattr(server_module, "_quarantine_marker_for", probe)
    monkeypatch.setattr(
        server_module, "_session_long_term", lambda session: None
    )
    key = app_state._session_key("u-lock", "s1")
    app_state.sessions[key] = _FakeSession()
    try:
        server_module.AligoWebApp.get_session(app_state, "u-lock", "s1")
    finally:
        app_state.sessions.pop(key, None)
    assert probed == [False], "隔离探测不能在全局会话锁内做文件系统 I/O"
    print("✓ 缓存命中的隔离探测在会话锁外执行")


def test_active_marker_detected_under_dsn(monkeypatch, tmp_path):
    """配置 PostgreSQL 时，落盘隔离标记（其它实例写入）仍要拦截缓存会话；
    无标记的遗留 .corrupt-* 继续按孤儿豁免。"""
    from config import STORAGE_CONFIG
    from context.long_term_memory import LongTermMemory
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    storage_dir = str(tmp_path)
    seed = LongTermMemory("u-marker-dsn", storage_dir)
    seed.save_preference("home_location", "北京")
    # 缓存实例在隔离发生前加载（fresh 为 None）
    cached = LongTermMemory("u-marker-dsn", storage_dir)
    assert cached.get_fresh_quarantine_backup() is None

    path = tmp_path / "u-marker-dsn.json"
    path.write_text("{bad json", encoding="utf-8")
    detecting = LongTermMemory("u-marker-dsn", storage_dir)
    backup = detecting.get_fresh_quarantine_backup()
    assert backup

    # 全局 MEMORY_DIR 与实例 db_path 目录解耦：扫描必须按后者命中
    global_dir = tmp_path / "global-memory"
    global_dir.mkdir()
    monkeypatch.setattr(server_module, "MEMORY_DIR", global_dir)
    monkeypatch.setitem(
        STORAGE_CONFIG, "postgres_dsn", "postgresql://u:p@127.0.0.1:1/db"
    )
    assert (
        server_module._quarantine_marker_for("u-marker-dsn", cached) == backup
    ), "DSN 配置下缓存会话必须从落盘标记发现隔离"

    # 无标记的遗留 .corrupt-* 在 DSN 配置下仍是孤儿（PG 恢复后豁免）；
    # 全局目录放一个诱饵备份，证明实例目录优先
    (global_dir / "u-legacy.json.corrupt-20260101000000000000-9").write_text(
        "{}", encoding="utf-8"
    )
    legacy_backup = (
        tmp_path / "u-legacy.json.corrupt-20260101000000000000-1"
    )
    legacy_backup.write_text("{}", encoding="utf-8")

    class _Legacy:
        db_path = str(tmp_path / "u-legacy.json")

        def get_recovered_corrupt_backup(self):
            return None

    assert server_module._quarantine_marker_for("u-legacy", _Legacy()) is None

    # 清除 DSN：扫描必须按实例 db_path 所在目录命中真实备份
    # （此前固定扫全局 MEMORY_DIR，自定义路径下断言恒真）
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    assert (
        server_module._quarantine_marker_for("u-legacy", _Legacy())
        == str(legacy_backup)
    )
    print("✓ DSN 配置下落盘标记仍拦截、无标记遗留备份豁免/按实例目录扫描")


def test_persistence_detail_maps_case_conflict():
    """大小写冲突是永久且可修正的问题，不能报成可重试的存储故障。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    detail = server_module._persistence_failure_detail(
        MemoryPersistenceError(
            "用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同（已有 alice）。当前文件系统"
            "大小写不敏感，请使用与已有账号完全一致的大小写"
        ),
        "rid-case",
    )
    assert "大小写" in detail and "存储暂时不可用" not in detail, detail
    assert "alice" not in detail, "不能回显已有账号 ID"


def test_open_long_term_maps_case_conflict(monkeypatch):
    """/api/import 等 _open_long_term 调用点同样给可执行的大小写提示，
    状态码为永久可修正的 409（不是可重试 503）。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    def boom(*args, **kwargs):
        raise MemoryPersistenceError(
            "用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同（已有 alice）"
        )

    monkeypatch.setattr(server_module, "create_long_term_memory", boom)
    with pytest.raises(server_module.HTTPException) as info:
        server_module._open_long_term("AliceXyz")
    assert info.value.status_code == 409
    assert "大小写" in info.value.detail, info.value.detail
    print("✓ 大小写冲突映射为 409 + 可执行提示")


def test_chat_maps_case_conflict_to_409(monkeypatch):
    """对话（含流式）端点同样把永久、可修正的大小写冲突映射为 409。"""
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    async def boom(*args, **kwargs):
        raise MemoryPersistenceError(
            "用户 ID 与已有记忆文件仅大小写或 Unicode 规范化形式不同（已有 alice）"
        )

    monkeypatch.setattr(server_module, "_get_session_marked", boom)
    client = _client()
    response = client.post(
        "/api/chat", json={"message": "你好", "user_id": "AliceXyz"}
    )
    assert response.status_code == 409, response.text
    assert "大小写" in response.json()["detail"], response.text
    streamed = client.post(
        "/api/chat/stream", json={"message": "你好", "user_id": "AliceXyz"}
    )
    assert streamed.status_code == 409, streamed.text
    print("✓ 对话 / 流式端点把大小写冲突映射为 409")


def test_web_session_quarantine_gate_requires_json_backend(monkeypatch, tmp_path):
    """_WebSession 构造守卫只在 JSON 后端（db_path）扫描隔离备份：
    PG 形存储不受遗留 .corrupt-* 影响。"""
    from config import STORAGE_CONFIG
    from utils.errors import MemoryPersistenceError
    from webapp import server as server_module

    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)
    (tmp_path / "u-gate.json.corrupt-20260101000000000000-1").write_text(
        "{}", encoding="utf-8"
    )

    class _FakeLongTerm:
        def __init__(self, db_path=None):
            if db_path is not None:
                self.db_path = db_path

    class _FakeMemory:
        def __init__(self, long_term):
            self.long_term = long_term

    class _FakeRuntime:
        def __init__(self, long_term):
            self.memory = _FakeMemory(long_term)

        def close(self):
            pass

    def make_session(long_term):
        monkeypatch.setattr(
            server_module,
            "ConversationRuntime",
            lambda **kwargs: _FakeRuntime(long_term),
        )
        return server_module._WebSession("u-gate", "s1", None, None)

    # PG 形（无 db_path）：不拦截
    session = make_session(_FakeLongTerm())
    session.close()

    # JSON 形（有 db_path）：拦截
    with pytest.raises(MemoryPersistenceError) as info:
        make_session(_FakeLongTerm(db_path=str(tmp_path / "u-gate.json")))
    assert "隔离" in str(info.value)
    print("✓ _WebSession 隔离门禁按后端类型生效")


def test_quarantined_backup_scan_ignores_deleted_accounts(monkeypatch, tmp_path):
    """账号数据已删除（.deleted 墓碑）时遗留隔离备份是孤儿：不再 503。"""
    from webapp import server as server_module

    import os as os_module

    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)
    backup = tmp_path / "u-gone.json.corrupt-20260101000000000000-1"
    backup.write_text("{}", encoding="utf-8")
    tombstone = tmp_path / "u-gone.json.deleted"
    tombstone.write_text("", encoding="utf-8")
    # mtime 固化：备份严格早于墓碑才是孤儿（相等按隔离 fail-closed）
    tomb_mtime = tombstone.stat().st_mtime
    os_module.utime(backup, (tomb_mtime - 10.0, tomb_mtime - 10.0))
    assert server_module._quarantined_memory_backup("u-gone") is None


def test_open_long_term_forwards_allow_quarantined(monkeypatch, tmp_path):
    """Web 恢复入口（/api/import）把 allow_quarantined=True 传给存储工厂，
    普通打开默认 False——隔离状态下只有恢复能重建主文件。"""
    from webapp import server as server_module

    monkeypatch.setattr(server_module, "MEMORY_DIR", tmp_path)
    recorded = []

    class _Store:
        db_path = str(tmp_path / "u1.json")

        def close(self):
            pass

    def fake_factory(user_id, path, strict=False, allow_quarantined=False):
        recorded.append(allow_quarantined)
        return _Store()

    monkeypatch.setattr(server_module, "create_long_term_memory", fake_factory)
    (tmp_path / "u1.json").write_text("{}", encoding="utf-8")
    server_module._open_long_term("u1")
    assert recorded[-1] is False
    server_module._open_long_term("u1", allow_quarantined=True)
    assert recorded[-1] is True
    print("✓ Web 存储打开按入口转发 allow_quarantined")

def test_trips_and_status_sanitize_lone_surrogates(monkeypatch):
    """历史数据里的孤立代理项不能让 /api/trips、/api/status、去重接口 500。"""
    from webapp import server as server_module

    surrogate = "\ud800"
    trips = [
        {
            "trip_id": "t1",
            "origin": "北京",
            "destination": surrogate,
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
        },
        {
            "trip_id": "t2",
            "origin": "北京",
            "destination": surrogate,
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
        },
    ]

    class _Store:
        def export_snapshot(self):
            return {
                "preferences": {},
                "trip_history": list(trips),
                "chat_history": [],
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {"total_trips": 2, "total_messages": 0},
            }

        def get_trip_history(self, limit=None):
            return list(trips)

        def close(self):
            pass

    monkeypatch.setattr(
        server_module,
        "create_long_term_memory",
        lambda *args, **kwargs: _Store(),
    )
    client = _client()
    listed = client.get("/api/trips", params={"user_id": "u-surrogate"})
    assert listed.status_code == 200, listed.text
    assert surrogate not in listed.text, listed.text
    assert "ud800" in listed.text, "孤立代理项应转义为可见文本"

    status = client.get("/api/status", params={"user_id": "u-surrogate"})
    assert status.status_code == 200, status.text
    assert surrogate not in status.text

    dedupe = client.post(
        "/api/trips/dedupe",
        json={"user_id": "u-surrogate", "dry_run": True},
    )
    assert dedupe.status_code == 200, dedupe.text
    print("✓ 行程 / 状态 / 去重接口清洗孤立代理项")


def test_preference_reserved_type_rejected():
    """保留类型 preferences：写入仍 422；删除放行（历史脏数据只能经它清理）。"""
    import json as json_module

    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_reserved_pref")
    memory_dir = Path(server_module.MEMORY_DIR)
    memory_dir.mkdir(parents=True, exist_ok=True)
    try:
        put = client.put(
            "/api/preferences/preferences",
            json={"user_id": user_id, "value": ["汉庭"]},
        )
        assert put.status_code == 422, put.text
        assert "保留" in put.json()["detail"]

        # 历史脏数据：旧版嵌套偏好容器行加载后不会被展开 / 丢弃
        (memory_dir / f"{user_id}.json").write_text(
            json_module.dumps(
                {
                    "preferences": [
                        {"type": "preferences", "value": {"nested": "旧格式"}}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        deleted = client.delete(
            "/api/preferences/preferences", params={"user_id": user_id}
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"type": "preferences", "deleted": True}
        # 再删一次：行已不存在，如实返回未删除
        again = client.delete(
            "/api/preferences/preferences", params={"user_id": user_id}
        )
        assert again.status_code == 200 and again.json()["deleted"] is False
    finally:
        _cleanup(user_id)
    print("✓ 保留偏好类型 preferences：写入拒绝、删除可清历史脏数据")

def test_import_rejects_merged_preference_limit(monkeypatch):
    """备份自身未超限但合并存量后超过偏好类型上限：预览与执行都 409。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_import_pref_limit")
    monkeypatch.setattr(server_module, "MAX_PREFERENCE_ENTRIES", 2)
    store = create_long_term_memory(user_id, str(server_module.MEMORY_DIR))
    try:
        store.save_preference("home_location", "北京")
        store.save_preference("hotel_brands", ["汉庭"])
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    backup = {"preferences": {"airlines": ["国航"]}}
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 409, response.text
        assert "偏好" in response.json()["detail"]
        preview = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": 1},
            json={"backup": backup},
        )
        assert preview.status_code == 409, preview.text
        print("✓ 合并后偏好超限的导入被拒绝（预览一致）")
    finally:
        _cleanup(user_id)


def test_import_over_limit_preference_account_allowed(monkeypatch):
    """存量偏好类型数已超上限的账号：恢复只更新既有类型的备份不再 409
    （与 CLI / 合并复核的基线豁免同口径），预览与执行同结论。"""
    from context.storage import create_long_term_memory
    from webapp import server as server_module

    client = _client()
    user_id = _unique_user("web_test_import_pref_over")
    monkeypatch.setattr(server_module, "MAX_PREFERENCE_ENTRIES", 2)
    store = create_long_term_memory(user_id, str(server_module.MEMORY_DIR))
    try:
        for index, pref_type in enumerate(
            ("home_location", "hotel_brands", "airlines")
        ):
            store.save_preference(pref_type, f"v{index}")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    # 同步收紧导入层上限：只有存量 > 上限时 _enforce_import_limits 的
    # 基线豁免分支才会真正执行（两个 200 常量不收紧则用例恒绿）
    from utils import user_import as user_import_module

    monkeypatch.setattr(user_import_module, "_MAX_PREFERENCES", 2)
    backup = {"preferences": {"home_location": "北京"}}
    try:
        response = client.post(
            "/api/import", params={"user_id": user_id}, json={"backup": backup}
        )
        assert response.status_code == 200, response.text
        assert response.json()["preferences"] == 1
        preview = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": 1},
            json={"backup": backup},
        )
        assert preview.status_code == 200, preview.text
        # 存量恰好在上限时，新增类型仍拒绝（只豁免已越限存量）
        monkeypatch.setattr(server_module, "MAX_PREFERENCE_ENTRIES", 3)
        blocked = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={"backup": {"preferences": {"seat": "靠窗"}}},
        )
        assert blocked.status_code == 409, blocked.text
    finally:
        _cleanup(user_id)
    print("✓ 存量超限偏好账号的恢复按基线放行")



def test_cached_session_protected_during_quarantine_probe(monkeypatch):
    """缓存命中的锁外隔离探测期间会话被占住：探测窗口内的容量回收不能
    驱逐刚命中的条目（此前已命中请求会落入饱和预检 503）。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_probe_protect")
    chat = {"message": "你好", "user_id": user_id, "session_id": "s1"}
    try:
        first = client.post("/api/chat", json=chat)
        assert first.status_code == 200, first.text
        key = state._session_key(user_id, "s1")
        observed = {}
        original = server_module._quarantine_marker_for

        def probing(user, long_term):
            with state._sessions_lock:
                item = state.sessions.get(key)
                observed["in_use"] = None if item is None else item.in_use()
            return original(user, long_term)

        monkeypatch.setattr(server_module, "_quarantine_marker_for", probing)
        second = client.post("/api/chat", json=chat)
        assert second.status_code == 200, second.text
        assert observed.get("in_use") is True, observed
        with state._sessions_lock:
            assert not state.sessions[key].in_use()
    finally:
        _cleanup(user_id)
    print("✓ 探测窗口内缓存命中被占住（不被容量回收驱逐）")


def test_head_supported_on_get_routes():
    """RFC 9110 §9.3.2：GET 资源必须支持 HEAD（SSE 端点除外）。

    健康检查 / HAProxy / uptime 监控常用 HEAD 探测，此前全部 405 并带
    Allow: GET；HEAD 响应必须与 GET 同状态同头、无 body。"""
    client = _client()
    for path in ("/", "/api/health", "/api/metrics", "/metrics"):
        head = client.head(path)
        assert head.status_code == 200, (path, head.status_code)
        assert head.content == b"", path
        get = client.get(path)
        assert get.status_code == 200, path
        assert (
            head.headers.get("content-type") == get.headers.get("content-type")
        ), (path, head.headers.get("content-type"), get.headers.get("content-type"))
        assert head.headers.get("x-content-type-options") == "nosniff", path
    # SSE 生成代价高且需要逐事件送达：HEAD 不适用（保持 405 而不是触发一次生成）
    assert client.head("/api/chat/stream").status_code == 405
    print("✓ HEAD 与 GET 同状态同头、无 body（SSE 除外）")


def test_gzip_accept_encoding_rfc_semantics():
    """Accept-Encoding 按 RFC 9110 解析：q=0 拒绝、大小写不敏感、* 通配。

    Starlette GZipMiddleware 只做大小写敏感子串判断（gzip;q=0 被压缩、
    GZIP 不压缩），应用层改为按语法解析后再交给中间件。"""
    client = _client()
    cases = [
        ("gzip", True),
        ("GZIP", True),
        ("GZip;q=1", True),
        ("gzip;q=0.5", True),
        ("gzip;q=0", False),
        ("gzip;q=0.0", False),
        ("identity, gzip;q=0", False),
        ("*;q=1", True),
        ("*;q=0", False),
        ("br", False),
        ("", False),
    ]
    for header, expect in cases:
        response = client.get("/", headers={"Accept-Encoding": header})
        assert response.status_code == 200, header
        got = response.headers.get("content-encoding") == "gzip"
        assert got is expect, (header, response.headers.get("content-encoding"))
    print("✓ Accept-Encoding 按 q 值 / 大小写 / 通配语义压缩")


def test_recently_deleted_many_batches_redis_exists():
    """批量墓碑查询：本地墓碑自愈补写、共享 EXISTS 按 ≤500 一批流水线。"""

    class _Pipeline:
        def __init__(self, store, log):
            self.store = store
            self.log = log
            self.keys = []

        def exists(self, key):
            self.keys.append(key)

        def execute(self):
            self.log.append(list(self.keys))
            return [int(key in self.store.data) for key in self.keys]

    class _FakeRedis:
        def __init__(self):
            self.data = {"aligo:deleted:u-batch:s-dead": "tok"}
            self.batches = []

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.data:
                return None
            self.data[key] = value
            return True

        def pipeline(self, transaction=False):
            return _Pipeline(self, self.batches)

    client = _client()
    state = client.app.state.aligo
    shared = _FakeRedis()
    state._redis_client = shared
    state._tombstones_shared = True
    # 本地墓碑（共享写入曾失败）：命中后应返回 True 并补写共享墓碑
    key = state._session_key("u-batch", "s-local")
    state._deleted_sessions[key] = (time.monotonic(), "tok-local")
    ids = ["s-local"] + [f"s{i}" for i in range(1200)] + ["s-dead"]
    try:
        deleted = state.recently_deleted_many("u-batch", ids)
        assert deleted == {"s-local", "s-dead"}, deleted
        assert shared.data.get("aligo:deleted:u-batch:s-local") == "tok-local"
        assert shared.batches, "共享查询应走流水线而不是逐键 EXISTS"
        assert all(len(batch) <= 500 for batch in shared.batches), shared.batches
        # 1201 个待查（s-dead + 1200 个未见本地墓碑）
        assert sum(len(batch) for batch in shared.batches) == 1201
        # 空输入不产生 Redis 往返
        assert state.recently_deleted_many("u-batch", []) == set()
    finally:
        state._deleted_sessions.pop(key, None)
        state._redis_client = None
        state._tombstones_shared = False
    print("✓ 批量墓碑查询走流水线（本地自愈 + 500/批）")


def test_dedupe_destructive_consumes_write_bucket(monkeypatch):
    """真实去重（dry_run=False）是破坏性批量写：必须同时占用写桶。"""
    from webapp import server as server_module

    seen = []
    monkeypatch.setattr(
        server_module,
        "_enforce_write_rate_limit",
        lambda state, request: seen.append("write"),
    )
    client = _client()
    user_id = _unique_user("web_dedupe_write")
    try:
        dry = client.post(
            "/api/trips/dedupe", json={"user_id": user_id, "dry_run": True}
        )
        assert dry.status_code == 200, dry.text[:300]
        assert seen == [], "dry_run 只做全量扫描，不应消耗写桶"
        real = client.post(
            "/api/trips/dedupe", json={"user_id": user_id, "dry_run": False}
        )
        assert real.status_code == 200, real.text[:300]
        assert seen == ["write"]
    finally:
        _cleanup(user_id)
    print("✓ 真实去重占用写桶，dry_run 只占读桶")


def test_import_same_source_concurrent_guard():
    """同一来源 IP 的并发导入上限为 1：慢速上传不能独占全部全局槽位。"""
    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_import_inflight")
    # TestClient 的 TCP 对端固定为 "testclient"
    assert state.try_begin_import("testclient") is True
    original_kb_limit = state.kb_limiter.limit
    state.kb_limiter.limit = 1
    try:
        try:
            response = client.post(
                "/api/import",
                params={"user_id": user_id},
                json={
                    "backup": {
                        "chat_history": [
                            {
                                "role": "user",
                                "content": "导入守卫测试",
                                "timestamp": "2026-11-01T09:00:00",
                                "session_id": "s-inflight",
                            }
                        ]
                    }
                },
            )
            refund_remaining = state.kb_limiter.status("testclient")[0]
        finally:
            state.kb_limiter.limit = original_kb_limit
        assert refund_remaining == 1, (
            "零成本 503 拒绝必须归还已扣的知识库额度"
        )
        assert response.status_code == 503, response.text[:300]
        assert "该来源已有导入任务在处理" in response.json()["detail"]
        assert response.headers.get("retry-after") == "5", (
            "并发拒绝应给出可重试节奏"
        )
        trips = client.post(
            "/api/trips/import",
            params={"user_id": user_id},
            json={"content": "a"},
        )
        assert trips.status_code == 503, trips.text[:300]
        assert "该来源已有导入任务在处理" in trips.json()["detail"]
        assert state.import_limiter.status("testclient")[0] == (
            state.import_limiter.limit
        ), "行程导入的同来源并发 503 也必须归还导入额度"
        assert state.import_queue_slots.acquire(blocking=False) is True, (
            "被来源守卫拒绝的请求不能泄漏全局槽位"
        )
        state.import_queue_slots.release()
    finally:
        state.end_import("testclient")
    try:
        ok = client.post(
            "/api/import",
            params={"user_id": user_id},
            json={
                "backup": {
                    "chat_history": [
                        {
                            "role": "user",
                            "content": "导入守卫测试",
                            "timestamp": "2026-11-01T09:00:00",
                            "session_id": "s-inflight",
                        }
                    ]
                }
            },
        )
        assert ok.status_code == 200, ok.text[:300]
        # 正常结束后登记必须被释放
        assert state.try_begin_import("testclient") is True
        state.end_import("testclient")
    finally:
        _cleanup(user_id)
    print("✓ 同来源并发导入被 503 拒绝且不泄漏槽位")


def test_import_lock_timeout_refunds_kb_rate_limit(monkeypatch):
    """等待用户锁超时（导入尚未做任何工作）：必须归还已扣的知识库额度。"""
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    user_id = _unique_user("web_import_lock_refund")

    async def lock_timeout(request, lock, request_id, timeout=10.0, user_id=None):
        raise server_module.HTTPException(
            status_code=503, detail="同一用户已有操作正在进行，请稍后再试"
        )

    monkeypatch.setattr(server_module, "_acquire_chat_lock", lock_timeout)
    original_kb_limit = state.kb_limiter.limit
    state.kb_limiter.limit = 1
    try:
        try:
            response = client.post(
                "/api/import",
                params={"user_id": user_id},
                json={
                    "backup": {
                        "chat_history": [
                            {
                                "role": "user",
                                "content": "取锁失败测试",
                                "timestamp": "2026-11-01T09:00:00",
                                "session_id": "s-lock",
                            }
                        ]
                    }
                },
            )
            remaining = state.kb_limiter.status("testclient")[0]
        finally:
            state.kb_limiter.limit = original_kb_limit
        assert response.status_code == 503, response.text[:300]
        assert remaining == 1, "取锁失败必须归还已扣的知识库额度"
    finally:
        _cleanup(user_id)
    print("✓ /api/import 取锁失败归还知识库限流额度")


def test_trust_proxy_hops_multi_layer(monkeypatch):
    """多层可信代理：TRUST_PROXY_HOPS 取倒数第 N 跳。

    恒取末跳时 CDN 出口地址成为全站共享限流身份（一个客户端即可打满
    所有人的额度）；按层数取真实客户端地址后互不影响，链长不足回退
    TCP 对端而不是采信客户端可伪造的前缀。"""
    from config import RATE_LIMIT_CONFIG
    from utils.rate_limit import RateLimiter

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_headers", True)
    monkeypatch.setitem(
        RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "127.0.0.1/32"
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    users = []

    def _chat(tag, xff):
        user = _unique_user(f"web_test_hops_{tag}")
        users.append(user)
        return client.post(
            "/api/chat",
            json={"message": "你好", "user_id": user, "session_id": f"h-{tag}"},
            headers={"X-Forwarded-For": xff},
        )

    try:
        # 默认层数 1：末跳（CDN 出口）是身份——不同真实客户端互相打满
        monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_hops", 1)
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat("h1", "203.0.113.7, 198.51.100.9").status_code == 200
        assert _chat("h2", "203.0.113.8, 198.51.100.9").status_code == 429
        # 层数 2：倒数第二跳是真实客户端，出口不再决定身份
        monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_hops", 2)
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat("h3", "203.0.113.7, 198.51.100.9").status_code == 200
        assert _chat("h4", "203.0.113.8, 198.51.100.9").status_code == 200
        assert _chat("h5", "203.0.113.7, 198.51.100.9").status_code == 429
        # 链长不足（配置 3 跳但只有 2 项）：回退 TCP 对端桶
        monkeypatch.setitem(RATE_LIMIT_CONFIG, "trust_proxy_hops", 3)
        state.ip_limiter = RateLimiter(limit=1, window_sec=60)
        assert _chat("h6", "203.0.113.7, 198.51.100.9").status_code == 200
        assert _chat("h7", "203.0.113.99, 198.51.100.9").status_code == 429
    finally:
        for user in users:
            _cleanup(user)
    print("✓ TRUST_PROXY_HOPS 多跳取址（默认末跳、链长不足回退）")


def test_trusted_proxy_cidr_accepts_mapped_ipv4_form(monkeypatch):
    """TRUSTED_PROXY_CIDRS 写成 IPv4 映射 IPv6 形式也必须命中。

    双栈监听时对端可能是 ``::ffff:172.18.0.1``，运维照日志把这个形式填进
    配置：原始地址与映射后的 IPv4 都要参与匹配，否则这类 CIDR 永不生效、
    XFF 被整体忽略、所有客户端共用代理 IP 的限流桶。
    """
    from config import RATE_LIMIT_CONFIG
    from webapp.server import _is_trusted_proxy_peer

    monkeypatch.setitem(
        RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "::ffff:203.0.113.0/120"
    )
    assert _is_trusted_proxy_peer("::ffff:203.0.113.50") is True
    assert _is_trusted_proxy_peer("::ffff:203.0.114.50") is False

    monkeypatch.setitem(
        RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "::ffff:203.0.113.5"
    )
    assert _is_trusted_proxy_peer("::ffff:203.0.113.5") is True
    assert _is_trusted_proxy_peer("::ffff:203.0.113.6") is False

    # 常规形式与默认配置口径不变
    monkeypatch.setitem(
        RATE_LIMIT_CONFIG, "trusted_proxy_cidrs", "127.0.0.1/32,::1/128"
    )
    assert _is_trusted_proxy_peer("127.0.0.1") is True
    assert _is_trusted_proxy_peer("::ffff:172.18.0.1") is False
    print("✓ TRUSTED_PROXY_CIDRS 支持 IPv4 映射形式")


def test_hsts_header_opt_in(monkeypatch):
    """HSTS 默认关闭；ALIGO_HSTS_MAX_AGE 显式开启后全部响应（含 404）附加。

    应用自身不终止 TLS，无条件 HSTS 会锁死同主机名其它明文服务，因此
    只做显式开关；开启时错误页同样下发（不能只保护 200）。"""
    from webapp import server as server_module

    client = _client()
    plain = client.get("/api/health")
    assert plain.headers.get("strict-transport-security") is None
    monkeypatch.setattr(server_module, "_HSTS_MAX_AGE", 31536000)
    enabled = client.get("/api/health")
    assert (
        enabled.headers.get("strict-transport-security")
        == "max-age=31536000"
    ), enabled.headers
    missing = client.get("/definitely-not-a-page")
    assert missing.status_code == 404
    assert (
        missing.headers.get("strict-transport-security")
        == "max-age=31536000"
    ), missing.headers
    print("✓ HSTS 显式开启后覆盖全部响应（含 404）")


def test_hsts_env_parsing_strips_injected_noise():
    """ALIGO_HSTS_MAX_AGE 与数值型配置同口径：去 BOM / 成对引号，非法值告警归零。"""
    import subprocess

    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "from webapp import server;"
        "print(server._HSTS_MAX_AGE)"
    )

    def _run(value):
        env = dict(os.environ)
        env["ALIGO_HSTS_MAX_AGE"] = value
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    quoted = _run('"31536000"')
    assert quoted.returncode == 0, quoted.stderr
    assert quoted.stdout.strip() == "31536000", quoted.stdout
    bom = _run(chr(0xFEFF) + "31536000")
    assert bom.returncode == 0, bom.stderr
    assert bom.stdout.strip() == "31536000", bom.stdout
    invalid = _run("abc")
    assert invalid.returncode == 0, invalid.stderr
    assert invalid.stdout.strip() == "0", invalid.stdout
    assert "不是整数" in invalid.stderr, invalid.stderr
    print("✓ ALIGO_HSTS_MAX_AGE 去 BOM / 引号，非法值告警归零")


def test_configure_app_logging_hides_httpx_request_lines():
    """根日志 INFO 下不再打印 httpx 请求行（URL 可能内嵌网关凭据）。"""
    import logging as logging_module

    import httpx

    from webapp import server as server_module

    server_module._configure_app_logging()
    httpx_logger = logging_module.getLogger("httpx")
    assert httpx_logger.getEffectiveLevel() >= logging_module.WARNING

    records = []

    class _Capture(logging_module.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    root = logging_module.getLogger()
    old_level = root.level
    handler = _Capture()
    root.addHandler(handler)
    root.setLevel(logging_module.INFO)
    try:
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        with httpx.Client(transport=transport) as client:
            client.post("https://alice:S3cretPw@gw.example.com/v1/chat")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    assert not any("S3cretPw" in message for message in records), records
    print("✓ 应用日志不记录 httpx 请求行（含 base_url 凭据）")


def test_chat_circuit_open_in_ask_refunds_rate_limit(monkeypatch):
    """锁内预检之后 ask 首行才被熔断拒绝：零 LLM 拒绝必须归还限流额度。"""
    from utils.circuit_breaker import CircuitOpenError
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=5, window_sec=60)
    state.ip_limiter = RateLimiter(limit=5, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_refund_in_ask")
    refunded: list[str] = []

    async def open_ask(self, message, on_event=None, turn_id="", **_kwargs):
        raise CircuitOpenError()

    monkeypatch.setattr(server_module.ConversationRuntime, "ask", open_ask)
    monkeypatch.setattr(
        server_module,
        "_refund_rate_limit",
        lambda st, uid, request: refunded.append(uid),
    )
    try:
        response = client.post(
            "/api/chat", json={"message": "你好", "user_id": user_id}
        )
        assert response.status_code == 503, response.text[:300]
        assert refunded == [user_id], "ask 层的熔断拒绝必须归还额度"

        refunded.clear()
        stream = client.post(
            "/api/chat/stream", json={"message": "你好", "user_id": user_id}
        )
        assert "服务暂时不可用" in stream.text, stream.text[:300]
        assert refunded == [user_id], "流式路径同样归还额度"
    finally:
        _cleanup(user_id)
    print("✓ ask 层熔断拒绝归还限流额度（非流式 / 流式）")


def test_chat_lock_timeout_refunds_rate_limit(monkeypatch):
    """等待用户锁超时 / 断连发生在任何 LLM 调用之前：必须归还额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=5, window_sec=60)
    state.ip_limiter = RateLimiter(limit=5, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_refund_lock")
    user_key = f"testclient:{user_id}"

    async def lock_timeout(request, lock, request_id, timeout=50.0, user_id=None):
        raise server_module.HTTPException(
            status_code=503, detail="同一用户已有对话正在进行，请稍后再试"
        )

    monkeypatch.setattr(server_module, "_acquire_chat_lock", lock_timeout)
    try:
        response = client.post(
            "/api/chat", json={"message": "你好", "user_id": user_id}
        )
        assert response.status_code == 503, response.text[:300]
        assert state.rate_limiter.status(user_key)[0] == 5
        assert state.ip_limiter.status("testclient")[0] == 5

        stream = client.post(
            "/api/chat/stream", json={"message": "你好", "user_id": user_id}
        )
        assert stream.status_code == 503, stream.text[:300]
        assert state.rate_limiter.status(user_key)[0] == 5, (
            "流式取锁失败同样归还额度"
        )
        assert state.ip_limiter.status("testclient")[0] == 5
    finally:
        _cleanup(user_id)
    print("✓ 取锁失败归还对话限流额度（非流式 / 流式）")


def test_chat_session_open_failures_refund_rate_limit(monkeypatch):
    """会话容量 / 打开会话失败（无 LLM 调用）同样归还额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = RateLimiter(limit=5, window_sec=60)
    state.ip_limiter = RateLimiter(limit=5, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_refund_open")
    user_key = f"testclient:{user_id}"

    async def no_capacity(*args, **kwargs):
        raise server_module.SessionCapacityError("并发会话过多（上限 1）")

    monkeypatch.setattr(server_module, "_get_session_marked", no_capacity)
    try:
        response = client.post(
            "/api/chat", json={"message": "你好", "user_id": user_id}
        )
        assert response.status_code == 503, response.text[:300]
        assert state.rate_limiter.status(user_key)[0] == 5
        assert state.ip_limiter.status("testclient")[0] == 5

        stream = client.post(
            "/api/chat/stream", json={"message": "你好", "user_id": user_id}
        )
        assert stream.status_code == 503, stream.text[:300]
        assert state.rate_limiter.status(user_key)[0] == 5
    finally:
        _cleanup(user_id)
    print("✓ 会话容量拒绝归还对话限流额度")


def test_delete_endpoints_lock_timeout_refund_write_bucket(monkeypatch):
    """删除会话 / 删除最后一轮取锁超时（409）：归还写桶额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.write_limiter = RateLimiter(limit=3, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_delete_refund")
    session_id = uuid.uuid4().hex[:12]
    monkeypatch.setattr(server_module, "_DELETE_LOCK_TIMEOUT_SEC", 0.01)

    class _HeldLock:
        def release(self):
            pass

        async def acquire(self):
            await asyncio.sleep(30)
            return True

    monkeypatch.setattr(state, "lock_for", lambda uid: _HeldLock())
    response = client.delete(
        f"/api/sessions/{session_id}", params={"user_id": user_id}
    )
    assert response.status_code == 409, response.text[:300]
    assert state.write_limiter.status("testclient")[0] == 3, (
        "删除会话取锁超时必须归还写桶额度"
    )
    response = client.post(
        f"/api/sessions/{session_id}/last-turn",
        json={"user_id": user_id, "expect": "hi"},
    )
    assert response.status_code == 409, response.text[:300]
    assert state.write_limiter.status("testclient")[0] == 3, (
        "删除最后一轮取锁超时必须归还写桶额度"
    )
    print("✓ 删除类接口取锁超时归还写桶额度")


def test_trips_dedupe_lock_timeout_refunds_quota(monkeypatch):
    """去重取锁失败（503）：归还写桶（真实删除）与读桶额度。"""
    from utils.rate_limit import RateLimiter
    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.write_limiter = RateLimiter(limit=3, window_sec=60)
    state.read_limiter = RateLimiter(limit=4, window_sec=60)
    client = TestClient(app)

    async def lock_timeout(request, lock, request_id, timeout=50.0, user_id=None):
        raise server_module.HTTPException(
            status_code=503, detail="同一用户已有对话正在进行，请稍后再试"
        )

    monkeypatch.setattr(server_module, "_acquire_chat_lock", lock_timeout)
    rejected = client.post(
        "/api/trips/dedupe",
        json={"user_id": "u-dedupe-lock", "dry_run": False},
    )
    assert rejected.status_code == 503, rejected.text[:300]
    assert state.write_limiter.status("testclient")[0] == 3
    assert state.read_limiter.status("testclient")[0] == 4
    preview = client.post(
        "/api/trips/dedupe",
        json={"user_id": "u-dedupe-lock", "dry_run": True},
    )
    assert preview.status_code == 503, preview.text[:300]
    assert state.write_limiter.status("testclient")[0] == 3
    assert state.read_limiter.status("testclient")[0] == 4, (
        "预览取锁失败同样归还读桶额度"
    )
    print("✓ 去重取锁失败归还写桶 / 读桶额度")


def test_user_bucket_late_rejection_refunds_ip_bucket():
    """并发下用户桶迟到拒绝：刚记的 IP 桶名额要归还。"""
    from utils.rate_limit import RateLimiter

    class _LateReject:
        backend = "memory"
        limit = 5
        window_sec = 60.0

        def peek(self, key):
            return None

        def check(self, key):
            return 60.0

        def status(self, key):
            return (0, 60.0)

        def refund(self, key):
            raise AssertionError("用户桶归还不在本分支调用")

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.rate_limiter = _LateReject()
    state.ip_limiter = RateLimiter(limit=4, window_sec=60)
    client = TestClient(app)
    user_id = _unique_user("web_test_ip_refund")
    response = client.post(
        "/api/chat", json={"message": "你好", "user_id": user_id}
    )
    assert response.status_code == 429, response.text[:300]
    assert state.ip_limiter.status("testclient")[0] == 4, (
        "被用户桶拒绝的请求不能消耗共享 IP 桶额度"
    )
    print("✓ 用户桶迟到拒绝归还 IP 桶额度")


def test_doctor_reports_open_breaker():
    """/api/doctor 不再在熔断打开时全绿：显式给出熔断检查项与 fail 状态。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    for _ in range(state.breaker.failure_threshold):
        state.breaker.record_failure()
    data = TestClient(app).get("/api/doctor").json()
    item = next(check for check in data["checks"] if check["name"] == "熔断器")
    assert item["status"] == "fail", item
    assert data["status"] == "fail"
    print("✓ /api/doctor 反映熔断状态")


def test_doctor_flags_half_open_degraded():
    """半开期多数对话仍 503：/api/doctor 的 status 是 ok，但 degraded 必须为真。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.breaker.recovery_timeout_sec = 0
    for _ in range(state.breaker.failure_threshold):
        state.breaker.record_failure()
    data = TestClient(app).get("/api/doctor").json()
    item = next(check for check in data["checks"] if check["name"] == "熔断器")
    assert item["status"] == "warn", item
    # 其它检查项在 CI（无 API Key / 无模型）可能为 fail：只验证半开口径
    assert data["degraded"] is True, data
    print("✓ /api/doctor 半开期标记 degraded")


def test_session_detail_normalizes_usage():
    """会话详情的 _meta.usage 与面板汇总同口径（负数归零、total 按 input+output）。"""
    from context.storage import create_long_term_memory

    client = _client()
    user = _unique_user("web_meta_usage")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "hi", "s-usage")
            store.add_chat_message(
                "assistant",
                '{"results": [], "_meta": {"usage": '
                '{"calls": -2, "input_tokens": 10, "output_tokens": 5, '
                '"total_tokens": 99}}}',
                "s-usage",
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.get("/api/sessions/s-usage", params={"user_id": user})
        assert response.status_code == 200, response.text
        usage = response.json()["messages"][-1]["meta"]["usage"]
        assert usage["calls"] == 0, "负数调用数必须归零"
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 15, "total 按 input+output 重算"
    finally:
        _cleanup(user)
    print("✓ 会话详情 usage 与汇总同口径")


def test_session_detail_normalizes_elapsed():
    """会话详情的 _meta.elapsed 与 /api/status 平均耗时同口径。

    备份恢复可带入负数或超出 float 范围的耗时：面板平均值忽略它们，
    页脚 / 执行路线此前原样渲染（-5s / Infinitys）。
    """
    from context.storage import create_long_term_memory

    client = _client()
    user = _unique_user("web_meta_elapsed")
    try:
        store = create_long_term_memory(user, str(MEMORY_DIR))
        try:
            store.add_chat_message("user", "hi", "s-elapsed")
            store.add_chat_message(
                "assistant",
                json.dumps({"_meta": {"elapsed": -5.0}}, ensure_ascii=False),
                "s-elapsed",
            )
            store.add_chat_message(
                "assistant",
                json.dumps({"_meta": {"elapsed": 10**400}}, ensure_ascii=False),
                "s-elapsed",
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        response = client.get("/api/sessions/s-elapsed", params={"user_id": user})
        assert response.status_code == 200, response.text
        elapsed = [
            message["meta"].get("elapsed")
            for message in response.json()["messages"]
            if message.get("meta")
        ]
        assert elapsed == [None, None], elapsed
    finally:
        _cleanup(user)
    print("✓ 会话详情 elapsed 与汇总同口径")


def test_lru_eviction_carries_pending_usage():
    """LRU 淘汰时未结算用量结转给重建的同名会话（否则用量永久少记）。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 1
    user = _unique_user("web_test_pending_usage")
    try:
        first = state.get_session(user, "s-a")
        first.runtime.seed_unsettled_usage({"calls": 1, "total_tokens": 7})
        state.get_session(user, "s-b")  # 淘汰 s-a，账目结转
        assert f"{user}\ns-a" not in state.sessions
        rebuilt = state.get_session(user, "s-a")  # 重建 s-a，并入结转账目
        assert rebuilt.runtime.take_pending_usage() == {
            "calls": 1,
            "total_tokens": 7,
        }
    finally:
        state.close_all()
        _cleanup(user)
    print("✓ LRU 淘汰结转未结算用量")


def test_pending_usage_carried_per_user():
    """结转表按用户分桶：A 的会话抖动不能挤出 B 的未结算用量。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 1
    user_b = _unique_user("web_test_pending_user_b")
    user_a = _unique_user("web_test_pending_user_a")
    try:
        first = state.get_session(user_b, "s-a")
        first.runtime.seed_unsettled_usage({"calls": 1, "total_tokens": 999})
        state.get_session(user_b, "s-b")  # 淘汰 s-a，账目结转给 B 的桶
        for index in range(3):
            session = state.get_session(user_a, f"a-{index}")
            session.runtime.seed_unsettled_usage(
                {"calls": index + 1, "total_tokens": 10}
            )
            state.get_session(user_a, f"next-{index}")  # 逐次淘汰并结转 A
        rebuilt = state.get_session(user_b, "s-a")
        assert rebuilt.runtime.take_pending_usage() == {
            "calls": 1,
            "total_tokens": 999,
        }
    finally:
        state.close_all()
        _cleanup(user_a)
        _cleanup(user_b)
    print("✓ 结转用量按用户分桶互不挤出")


def test_pending_usage_carry_merges_instead_of_overwrite():
    """同名会话已有结转账目时再次结转必须合并，不能覆盖旧账。

    淘汰方在释放 _sessions_lock 后才结转，而重建方可能在锁内先弹出空值：
    旧账只在稍后落入桶里，若随后新账直接赋值，旧未结算用量永久少记。
    """
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 2
    user = _unique_user("web_test_pending_merge")
    try:
        session = state.get_session(user, "s-merge")
        session.runtime.seed_unsettled_usage({"calls": 1, "total_tokens": 7})
        state._carry_pending_usage(session)
        session.runtime.seed_unsettled_usage({"calls": 1, "total_tokens": 11})
        state._carry_pending_usage(session)
        assert state._pending_usage[user]["s-merge"] == {
            "calls": 2,
            "total_tokens": 18,
        }, state._pending_usage[user]
    finally:
        state.close_all()
        _cleanup(user)
    print("✓ 重复结转合并而非覆盖")


def test_pending_usage_carry_seeds_live_rebuilt_session():
    """重建先于结转：活着的同名会话必须直接并入账目，不能只写桶。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    state.max_sessions = 2
    user = _unique_user("web_test_pending_live")
    try:
        first = state.get_session(user, "s-live")
        # 模拟「重建方已插入、结转方才开始」：把旧实例移出缓存但不结转
        with state._sessions_lock:
            state.sessions.pop(state._session_key(user, "s-live"), None)
        rebuilt = state.get_session(user, "s-live")
        assert rebuilt is not first
        first.runtime.seed_unsettled_usage({"calls": 1, "total_tokens": 7})
        state._carry_pending_usage(first)
        assert rebuilt.runtime.take_pending_usage() == {
            "calls": 1,
            "total_tokens": 7,
        }, "活实例必须直接并入结转账目"
        # 活实例存在时不再写桶（桶只留给下次重建）
        assert user not in state._pending_usage, state._pending_usage.get(user)
    finally:
        first.close()
        state.close_all()
        _cleanup(user)
    print("✓ 结转并入已重建的活会话")


def test_weather_executor_threads_are_daemon_and_not_registered():
    """天气池复用守护执行器：停服不被在途上游请求拖住。"""
    from concurrent.futures.thread import _threads_queues
    from webapp import server as server_module

    executor = server_module._get_weather_executor()
    try:
        assert executor.submit(lambda: 1).result(timeout=5) == 1
        threads = list(executor._threads)
        assert threads and all(thread.daemon for thread in threads)
        assert all(thread not in _threads_queues for thread in threads), (
            "登记到 _threads_queues 会让 atexit 逐个 join，停服仍被天气请求拖住"
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        server_module._weather_executor = None
    print("✓ 天气执行器线程不参与 atexit join")


def test_slow_body_on_api_times_out_with_408(monkeypatch):
    """非导入接口的慢速上传在统一时限后 408 并关闭连接（Slowloris）。"""
    from webapp import server as server_module

    monkeypatch.setattr(server_module, "BODY_READ_TIMEOUT_SEC", 0.05)
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    events: list[tuple] = []

    async def receive():
        if not events:
            events.append(("chunk",))
            return {"type": "http.request", "body": b"{", "more_body": True}
        await asyncio.sleep(10)
        return {"type": "http.request", "body": b"}", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            headers = {k.lower(): v for k, v in message.get("headers") or []}
            events.append((message["status"], headers.get(b"connection")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", b"2"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    starts = [event for event in events if len(event) == 2]
    assert starts and starts[-1][0] == 408, events
    assert starts[-1][1] == b"close", events
    print("✓ 慢速请求体 408 并关闭连接")


def test_rejections_without_reading_body_close_connection():
    """未读请求体的拒绝（405/404/307/Host 400）必须关闭连接。"""
    client = _client()
    for path, expected in (("/api/health", 405), ("/nope", 404)):
        response = client.post(path, content=b'{"x": 1}')
        assert response.status_code == expected, (path, response.text)
        assert response.headers.get("connection") == "close", (
            path,
            response.headers,
        )
    redirect = client.post("/api/import/", content=b"{}", follow_redirects=False)
    assert redirect.status_code in (307, 308), redirect.text
    assert redirect.headers.get("connection") == "close", redirect.headers
    rejected = client.post(
        "/api/health", content=b"{}", headers={"Host": "evil.example"}
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.headers.get("connection") == "close", rejected.headers
    # 正常请求（无 body / body 已读尽）不得关闭连接
    assert client.get("/api/health").headers.get("connection") != "close"
    user = _unique_user("web_test_unread_body")
    try:
        ok = client.put(
            "/api/preferences/hotel_brand",
            json={"user_id": user, "value": "汉庭"},
        )
        assert ok.status_code in (200, 201), ok.text
        assert ok.headers.get("connection") != "close", ok.headers
    finally:
        _cleanup(user)
    print("✓ 未读请求体的拒绝关闭连接、正常请求保持 keep-alive")


def test_head_probe_only_on_lightweight_routes(monkeypatch):
    """HEAD 只补在轻量探活路由上：导出 / 上游类 GET 不得被探测完整执行。"""
    from webapp import server as server_module

    loads: list = []
    monkeypatch.setattr(
        server_module,
        "_load_user_data",
        lambda *args, **kwargs: loads.append(args) or {},
    )
    fetched: list = []
    monkeypatch.setattr(
        server_module,
        "fetch_weather",
        lambda *args, **kwargs: fetched.append(args) or {},
    )
    client = _client()
    user = _unique_user("web_test_head_probe")
    export = client.head("/api/export", params={"user_id": user})
    assert export.status_code == 405, export.text
    assert not loads, "HEAD 不应执行完整导出读取"
    weather = client.head("/api/weather", params={"city": "北京"})
    assert weather.status_code == 405, weather.text
    assert not fetched, "HEAD 不应触发真实上游天气请求"
    for path in ("/", "/api/health", "/api/metrics", "/metrics"):
        assert client.head(path).status_code == 200, path
    print("✓ HEAD 仅用于轻量探活路由")


def test_export_abort_recorded_as_499_and_slot_released():
    """导出流被客户端中断：按 499 记账、导出槽释放。"""
    from utils.metrics import REQUEST_METRICS

    REQUEST_METRICS.reset()
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    user = _unique_user("web_test_export_abort")
    events: list = []

    async def receive():
        await asyncio.sleep(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            events.append(message["status"])
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("broken pipe")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/export",
        "raw_path": b"/api/export",
        "query_string": f"user_id={user}".encode(),
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"x-aligo-client", b"web"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    try:
        asyncio.run(app(scope, receive, send))
    except RuntimeError as propagated:
        # 响应已开始后的异常以占位异常交还服务器：不能携带原始错误文本
        assert "broken pipe" not in str(propagated), propagated
    snapshot = REQUEST_METRICS.snapshot()
    assert 200 in events, events
    assert snapshot["by_status"].get("499") == 1, snapshot
    assert "200" not in snapshot["by_status"], snapshot
    acquired = state.export_slots.acquire(blocking=False)
    assert acquired, "中断的导出必须释放并发槽位"
    if acquired:
        state.export_slots.release()
    REQUEST_METRICS.reset()
    _cleanup(user)
    print("✓ 导出断连记 499 并释放导出槽")


def test_lease_request_cancel_is_idempotent():
    """生成器 finally 与 stop_turn 共用「回合只取消一次」守卫。"""
    from webapp.server import _SessionTurnLease

    class _Task:
        def __init__(self):
            self.cancels = 0

        def done(self):
            return False

        def cancel(self):
            self.cancels += 1

        def __await__(self):
            if False:  # pragma: no cover - 只用于成为生成器
                yield
            return None

    class _Sess:
        runtime = None

        def end_turn(self):
            pass

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        lease = _SessionTurnLease(lock, _Sess())
        task = _Task()
        lease.bind_turn(task)
        lease.request_cancel()
        await lease.stop_turn()
        assert task.cancels == 1, task.cancels
        lease.release()
        assert not lock.locked()

    asyncio.run(scenario())
    print("✓ 租约 request_cancel 幂等（stop_turn 不重复取消）")


def test_close_all_skips_in_use_sessions():
    """停机收尾不关闭仍有在途回合的会话（落库线程仍需存储连接）。"""
    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo

    class _Sess:
        def __init__(self, busy):
            self.busy = busy
            self.closed = False

        def in_use(self):
            return self.busy

        def close(self):
            self.closed = True

    busy = _Sess(True)
    idle = _Sess(False)
    state.sessions["a"] = busy
    state.sessions["b"] = idle
    state.close_all()
    assert busy.closed is False, "在途会话关闭会打断落库并可能卡在等锁"
    assert idle.closed is True
    assert not state.sessions
    print("✓ close_all 跳过在途会话")


def test_chat_lock_timeout_reports_import_resume(monkeypatch):
    """持锁者是备份恢复时，超时文案如实归因，不再误报「已有对话在进行」。"""
    import asyncio as asyncio_module

    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    user_id = _unique_user("web_test_lock_import_reason")
    state.begin_user_import(user_id)
    monkeypatch.setattr(server_module, "_CHAT_LOCK_POLL_SEC", 0.01)

    class _Request:
        def __init__(self) -> None:
            self.app = app

        async def is_disconnected(self) -> bool:
            return False

    async def _timed_out():
        lock = asyncio_module.Lock()
        await lock.acquire()
        try:
            await server_module._acquire_chat_lock(
                _Request(), lock, "rid", timeout=0.05, user_id=user_id
            )
        finally:
            lock.release()

    try:
        with pytest.raises(server_module.HTTPException) as info:
            asyncio_module.run(_timed_out())
        assert info.value.status_code == 503
        assert "恢复备份" in info.value.detail
        assert (info.value.headers or {}).get("Retry-After") == "5"
    finally:
        state.end_user_import(user_id)
        _cleanup(user_id)
    print("✓ 用户锁超时按真实持锁操作归因（备份恢复）")


def test_delete_session_busy_lock_reports_import_resume(monkeypatch):
    """删除会话的 409 在备份恢复持锁时同样如实归因。"""
    import asyncio as asyncio_module

    from webapp import server as server_module

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    client = TestClient(app)

    class _StuckLock:
        async def acquire(self):
            await asyncio_module.sleep(30)

        def release(self):
            pass

    monkeypatch.setattr(state, "lock_for", lambda user_id: _StuckLock())
    monkeypatch.setattr(server_module, "_DELETE_LOCK_TIMEOUT_SEC", 0.05)
    user_id = _unique_user("web_test_delete_import_reason")
    state.begin_user_import(user_id)
    try:
        response = client.delete(
            "/api/sessions/s-busy", params={"user_id": user_id}
        )
        assert response.status_code == 409
        assert "恢复备份" in response.json()["detail"]
    finally:
        state.end_user_import(user_id)
    print("✓ 删除会话锁忙 409 按备份恢复归因")


def test_kb_import_busy_lock_refunds_quota(monkeypatch):
    """知识库写锁忙（零成本拒绝）退还导入额度；普通失败不退款。"""
    from utils import knowledge_admin
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo
    original_limit = state.import_limiter.limit
    state.import_limiter.limit = 5

    def busy(*args, **kwargs):
        raise knowledge_admin.KnowledgeBusyError(
            "知识库正在写入（导入进行中），请稍后重试"
        )

    monkeypatch.setattr(server_module, "import_documents", busy)
    try:
        response = client.post(
            "/api/knowledge/import", json={"documents": [{"content": "x"}]}
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "5"
        assert state.import_limiter.status("testclient")[0] == 5, (
            "写锁忙的零成本 503 必须归还导入桶额度"
        )
        preview = client.post(
            "/api/knowledge/import/preview",
            json={"documents": [{"content": "x"}]},
        )
        assert preview.status_code == 503
        assert state.import_limiter.status("testclient")[0] == 5, (
            "预览同样归还导入桶额度"
        )

        def boom(*args, **kwargs):
            raise RuntimeError("milvus down")

        monkeypatch.setattr(server_module, "import_documents", boom)
        failed = client.post(
            "/api/knowledge/import", json={"documents": [{"content": "x"}]}
        )
        assert failed.status_code == 503
        assert state.import_limiter.status("testclient")[0] == 4, (
            "非零成本失败不应退款（不扩大退款范围）"
        )
    finally:
        state.import_limiter.limit = original_limit
    print("✓ 知识库写锁忙退还导入额度（普通失败不退款）")


def test_weather_upstream_quota_maps_429_and_refunds(monkeypatch):
    """全进程上游额度用尽：429 + Retry-After + 额度头，并归还 IP 天气桶。"""
    from utils.weather import UpstreamQuotaError
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo

    def quota(city):
        raise UpstreamQuotaError("天气查询过于频繁，请稍后再试", retry_after=7)

    monkeypatch.setattr(server_module, "fetch_weather", quota)
    before = state.weather_limiter.status("testclient")[0]
    response = client.get(
        "/api/weather", params={"city": "上游额度测试城"}
    )
    assert response.status_code == 429, response.text[:300]
    assert response.headers.get("Retry-After") == "7"
    assert response.headers.get("X-RateLimit-Limit") == str(
        state.weather_limiter.limit
    )
    assert "天气查询过于频繁" in response.json()["detail"]
    assert state.weather_limiter.status("testclient")[0] == before, (
        "未发出上游请求的额度拒绝必须归还天气桶"
    )
    print("✓ 天气上游额度用尽映射 429 并归还额度")


def test_weather_busy_maps_503_and_refunds(monkeypatch):
    """本地排队繁忙：503 + Retry-After，且归还本次 IP 天气桶。"""
    from utils.weather import WeatherBusyError
    from webapp import server as server_module

    client = _client()
    state = client.app.state.aligo

    def busy(city):
        raise WeatherBusyError("天气服务繁忙，请稍后再试")

    monkeypatch.setattr(server_module, "fetch_weather", busy)
    before = state.weather_limiter.status("testclient")[0]
    response = client.get("/api/weather", params={"city": "本地繁忙测试城"})
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert "繁忙" in response.json()["detail"]
    assert state.weather_limiter.status("testclient")[0] == before, (
        "本地繁忙未产生可用结果，必须归还天气桶"
    )
    print("✓ 天气本地繁忙映射 503 并归还额度")


def test_pending_tombstone_replay_is_single_flight(monkeypatch):
    """恢复期并发触发补写只允许一轮：节流判定与补写循环互斥。"""
    import threading as _threading

    app = create_app(model=UsageTrackingModel(_FakeModel()))
    state = app.state.aligo
    entered = _threading.Event()
    release_evt = _threading.Event()
    runs: list[int] = []

    def slow_replay(limit=20):
        runs.append(1)
        entered.set()
        release_evt.wait(timeout=5)
        return 0

    monkeypatch.setattr(state, "replay_pending_tombstones", slow_replay)
    state._pending_shared_tombstones[("u-single", "s-single")] = (
        "tok",
        time.monotonic() + 3600,
    )
    state._pending_replay_at = 0.0
    try:
        first = _threading.Thread(target=state._maybe_replay_pending)
        first.start()
        assert entered.wait(timeout=5), "第一轮补写应启动"
        # 第一轮仍在运行时，即使节流时间已过也必须直接跳过
        state._pending_replay_at = 0.0
        second = _threading.Thread(target=state._maybe_replay_pending)
        second.start()
        second.join(timeout=2)
        assert not second.is_alive(), "第二个触发必须立即返回"
        assert len(runs) == 1, "补写必须单飞，不能重复执行"
        # 恢复节流窗口，供「结束后仍受 5 秒节流」断言使用
        state._pending_replay_at = time.monotonic() + 5.0
        release_evt.set()
        first.join(timeout=5)
        assert not first.is_alive()
        state._maybe_replay_pending()
        assert len(runs) == 1, "结束后 5 秒节流仍然生效"
    finally:
        release_evt.set()
        state._pending_shared_tombstones.pop(("u-single", "s-single"), None)
        state._pending_replay_at = 0.0
    print("✓ 墓碑补写单飞（并发触发不重复执行）")


def test_knowledge_doc_id_rejects_lax_integer_forms(monkeypatch):
    """知识片段路由的 doc_id 只接受规范十进制，宽松写法不能命中别的记录。"""
    from webapp import server as server_module

    calls = []

    def fake_delete(doc_id, whole_group=False):
        calls.append((doc_id, whole_group))
        return 1

    monkeypatch.setattr(server_module, "delete_document", fake_delete)
    client = _client()
    for raw in ("1_0", "1.0", "+1", "%201"):
        response = client.delete(f"/api/knowledge/documents/{raw}?group=1")
        assert response.status_code == 422, (raw, response.text[:200])
    assert not calls, "非法写法不能落到 delete_document（1_0 曾被读成 10）"
    ok = client.delete("/api/knowledge/documents/10?group=1")
    assert ok.status_code == 200, ok.text[:200]
    assert calls == [(10, True)]
    group = client.get("/api/knowledge/documents/1.0/group")
    assert group.status_code == 422, group.text[:200]
    update = client.put(
        "/api/knowledge/documents/+1", json={"content": "新内容"}
    )
    assert update.status_code == 422, update.text[:200]
    print("✓ 知识片段 doc_id 拒绝 1_0 / 1.0 / +1 等宽松整数")


def test_query_int_params_reject_lax_integer_forms():
    """分页 / top_k 查询参数同样拒绝 1_0、5.00、+2 等非规范写法。"""
    client = _client()
    user_id = _unique_user("strict_query_int")
    for params in ({"limit": "1_0"}, {"offset": "5.00"}, {"limit": "+2"}):
        response = client.get(
            "/api/sessions", params={"user_id": user_id, **params}
        )
        assert response.status_code == 422, (params, response.text[:200])
    trips = client.get(
        "/api/trips", params={"user_id": user_id, "limit": "1_0"}
    )
    assert trips.status_code == 422, trips.text[:200]
    top_k = client.get(
        "/api/knowledge/search", params={"q": "住宿", "top_k": "1_0"}
    )
    assert top_k.status_code == 422, top_k.text[:200]
    print("✓ limit / offset / top_k 拒绝宽松整数写法")


def test_today_query_rejects_epoch_shorthand():
    """today=0 不能被 pydantic 读成 1970-01-01：三个端点统一 422。"""
    client = _client()
    user_id = _unique_user("strict_today")
    for value in ("0", "0.0", "86400"):
        for path in ("/api/status", "/api/trips"):
            response = client.get(
                path, params={"user_id": user_id, "today": value}
            )
            assert response.status_code == 422, (path, value, response.text[:200])
    export = client.get(
        "/api/trips/export",
        params={"user_id": user_id, "today": "0"},
        headers={"X-Aligo-Client": "web"},
    )
    assert export.status_code == 422, export.text[:200]
    ok = client.get(
        "/api/status", params={"user_id": user_id, "today": "2026-11-28"}
    )
    assert ok.status_code == 200, ok.text[:200]
    print("✓ today 只接受 YYYY-MM-DD（epoch 简写 422）")


def test_import_dry_run_count_limit_returns_409_like_real_run(monkeypatch):
    """预览与真实执行的条数轴超限同一结论：容量上限统一 409（不是 413）。"""
    from context.storage import create_long_term_memory
    from utils import user_import as user_import_module

    monkeypatch.setattr(user_import_module, "_MAX_MESSAGES", 2)
    user_id = _unique_user("web_test_import_dry_cap")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("旧问", "旧答", "s1")
        backup = {
            "backup": {
                "chat_history": [
                    {
                        "role": "user",
                        "content": "新问",
                        "session_id": "s2",
                        "timestamp": "2026-01-02T00:00:00",
                    }
                ]
            }
        }
        client = _client()
        preview = client.post(
            "/api/import",
            params={"user_id": user_id, "dry_run": 1},
            json=backup,
        )
        assert preview.status_code == 409, preview.text[:300]
        assert "上限" in preview.json()["detail"]
        real = client.post(
            "/api/import", params={"user_id": user_id}, json=backup
        )
        assert real.status_code == 409, real.text[:300]
        assert "上限" in real.json()["detail"]
    finally:
        _cleanup(user_id)
    print("✓ 备份导入预览与真实执行对条数超限均返回 409")


def test_trip_create_empty_purpose_uses_default_and_preserves_existing():
    """显式空串 purpose 与省略同义：新行程落「出差」，已存在行程保留原目的。"""
    client = _client()
    user_id = _unique_user("web_trip_purpose_empty")
    try:
        created = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "purpose": "",
            },
        )
        assert created.status_code == 201, created.text[:200]
        trips = client.get("/api/trips", params={"user_id": user_id}).json()[
            "trips"
        ]
        assert trips[0]["purpose"] == "出差", trips
        meeting = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-02",
                "purpose": "会议",
            },
        )
        assert meeting.status_code == 201, meeting.text[:200]
        again = client.post(
            "/api/trips",
            json={
                "user_id": user_id,
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-02",
                "purpose": "   ",
            },
        )
        assert again.status_code == 201, again.text[:200]
        assert again.json()["created"] is False
        hangzhou = client.get(
            "/api/trips", params={"user_id": user_id, "q": "杭州"}
        ).json()["trips"]
        assert hangzhou[0]["purpose"] == "会议", hangzhou
    finally:
        _cleanup(user_id)
    print("✓ 空串 purpose：新行程落默认「出差」，已存在行程保留原目的")


def test_session_search_collapses_internal_whitespace():
    """会话搜索折叠查询内部空白：双空格 / Tab 不再把命中会话过滤成 0 结果。"""
    from context.storage import create_long_term_memory

    client = _client()
    user_id = _unique_user("web_search_ws")
    try:
        store = create_long_term_memory(user_id, str(MEMORY_DIR))
        store.add_chat_turn("杭州 北京 出差计划", "好的", "s-ws")
        for query in ("杭州  北京", "杭州\t北京"):
            response = client.get(
                "/api/sessions", params={"user_id": user_id, "q": query}
            )
            assert response.status_code == 200, response.text[:200]
            payload = response.json()
            assert payload["total"] == 1, (query, response.text[:200])
            assert payload["sessions"][0]["snippet"], query
    finally:
        _cleanup(user_id)
    print("✓ 会话搜索折叠内部空白（双空格 / Tab）")


def test_knowledge_list_search_collapses_internal_whitespace(monkeypatch):
    """知识片段搜索同样折叠查询与正文的内部空白。"""
    from utils import knowledge_admin

    rows = [{"id": 1, "content": "住宿 标准说明", "metadata": "{}"}]
    monkeypatch.setattr(
        knowledge_admin, "_iter_all_rows", lambda client: rows
    )
    assert (
        knowledge_admin._list_documents(None, 20, 0, "住宿  标准", "")["total"]
        == 1
    )
    assert (
        knowledge_admin._list_documents(None, 20, 0, "住宿\t标准", "")["total"]
        == 1
    )
    print("✓ 知识片段搜索折叠内部空白（双空格 / Tab）")


def test_import_accepts_utf16_backup_and_rejects_unknown_encoding():
    """带 BOM 的 UTF-16 备份与 json.loads 同口径可恢复；未知编码明确 415。"""
    client = _client()
    user_id = _unique_user("web_test_import_utf16")
    try:
        body = json.dumps(
            {"backup": {"preferences": {"home_location": "北京"}}},
            ensure_ascii=False,
        ).encode("utf-16")
        response = client.post(
            "/api/import",
            params={"user_id": user_id},
            content=body,
            headers={"Content-Type": "application/json; charset=utf-16"},
        )
        assert response.status_code == 200, response.text[:300]
        assert response.json()["preferences"] == 1
    finally:
        _cleanup(user_id)
    bad = client.post(
        "/api/import",
        params={"user_id": _unique_user("web_test_import_badenc")},
        content=b'{"backup": {"preferences": {"x": "\x80"}}}',
        headers={"Content-Type": "application/json"},
    )
    assert bad.status_code == 415, bad.text[:300]
    assert "UTF-8" in bad.json()["detail"]
    print("✓ /api/import 接受 UTF-16 BOM 备份，未知编码 415")


def test_trip_filter_maxlength_matches_backend_q_limit():
    """行程筛选框 maxlength 必须与后端 q / 字段上限（100 字）一致。"""
    index_html = (
        Path(__file__).resolve().parents[1] / "webapp" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert re.search(r'id="tripFilter"[^>]*maxlength="100"', index_html), (
        "行程筛选框 maxlength 必须与后端 100 字上限一致"
    )
    print("✓ 行程筛选框 maxlength 与后端 q 上限一致")


def test_preference_size_limit_uses_compact_json_serialization():
    """偏好大小按紧凑 JSON 计长：2001 项列表（紧凑 8005）不再被误判超限。"""
    from utils.preference_values import preference_value_problem
    from webapp.server import PreferencePayload

    value = ["x"] * 2001
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(compact) == 8005
    assert len(json.dumps(value, ensure_ascii=False)) > 10000, (
        "默认分隔符口径会把该值误判为超限"
    )
    assert preference_value_problem(value) is None
    assert PreferencePayload(user_id="u-compact", value=value).value == value
    assert preference_value_problem(["x"] * 2500) is not None, (
        "超过 10000 字符的偏好仍须拒绝"
    )
    print("✓ 偏好大小校验与前端紧凑 JSON 口径一致")


if __name__ == "__main__":
    # 用例常用 tempfile.mkdtemp() 自建记忆目录：直跑没有 pytest 的
    # conftest 回收，登记后在进程退出时统一清理
    import _tmp_registry

    _tmp_registry.install()
    # 直跑模式自动发现全部用例：monkeypatch / tmp_path 自动提供，
    # 可选 caplog（默认 None，用例自带无 caplog 分支）按 None 传入；
    # 其余需 pytest 专用夹具的用例计入跳过数并明确报告
    import asyncio
    import inspect
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError
    # 把清单截断在第一个用例；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    import pathlib
    import shutil
    import tempfile

    ran = 0
    skipped = 0
    skipped_names: list[str] = []
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        params = inspect.signature(func).parameters
        fixture = None
        caplog_shim = None
        kwargs: dict = {}
        tmp_dir_paths: list[pathlib.Path] = []
        unsupported = False
        for param, spec in params.items():
            if param == "monkeypatch":
                fixture = pytest.MonkeyPatch()
                kwargs[param] = fixture
            elif param == "tmp_path":
                _tmp_dir = pathlib.Path(tempfile.mkdtemp())
                tmp_dir_paths.append(_tmp_dir)
                kwargs[param] = _tmp_dir
            elif param == "caplog":
                # pytest 夹具只在 pytest 内注入：直跑用最小替身真正执行
                # 日志断言，不能静默跳过（否则脱敏回归在直跑清单上假绿）
                import _caplog_shim

                caplog_shim = _caplog_shim.CaplogShim()
                kwargs[param] = caplog_shim
            else:
                unsupported = True
                break
        if unsupported:
            if caplog_shim is not None:
                caplog_shim.close()
            if fixture is not None:
                fixture.undo()
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
            skipped += 1
            skipped_names.append(f"{name}（需 pytest 专用夹具）")
            continue
        # helper 导入必须在兜底 try 之外：_direct_run_kb 缺失 / 隔离装配
        # 失败要显式中止，不能被「user_import 不可用」的兜底吞掉后
        # 静默跑真实知识库
        import _direct_run_kb

        kb_fixture: pytest.MonkeyPatch | None = None
        try:
            from utils import user_import as _ui_for_kb

            kb_fixture = pytest.MonkeyPatch()
            kb_fixture.setattr(
                _ui_for_kb, "_existing_knowledge_rows", lambda: []
            )
            # 预览的知识可用性探针会加载真实嵌入模型：直跑默认 no-op
            kb_fixture.setattr(
                _ui_for_kb, "_knowledge_ready_probe", lambda: None
            )
        except Exception:  # noqa: BLE001 - 模块不可用时不补丁
            if kb_fixture is not None:
                kb_fixture.undo()
            kb_fixture = None
        else:
            # 与 pytest conftest 同口径：规划 / 布局扫描也不能打开真实
            # Milvus（与 CI「无库」结论一致）
            _direct_run_kb.isolate(kb_fixture)
        storage_snapshot: dict | None = None
        try:
            from config import STORAGE_CONFIG

            storage_snapshot = dict(STORAGE_CONFIG)
        except Exception:  # noqa: BLE001 - 配置不可用时不快照
            storage_snapshot = None
        try:
            # async def 用例同样要真正执行：直接调用只创建 coroutine，
            # 断言从不运行，直跑清单会给出假绿
            if inspect.iscoroutinefunction(func):
                asyncio.run(func(**kwargs))
            else:
                func(**kwargs)
            ran += 1
        except pytest.skip.Exception as exc:
            skipped += 1
            skipped_names.append(f"{name}（条件跳过：{exc}）")
        finally:
            if caplog_shim is not None:
                caplog_shim.close()
            if storage_snapshot is not None:
                from typing import cast

                from config import STORAGE_CONFIG

                _cfg = cast(dict, STORAGE_CONFIG)
                _cfg.clear()
                _cfg.update(storage_snapshot)
            if fixture is not None:
                fixture.undo()
            if kb_fixture is not None:
                kb_fixture.undo()
            # 直跑模式自建的临时目录要自己清：否则每跑一次清单就在系统
            # 临时目录里留下一批带记忆文件的目录
            for _tmp_dir in tmp_dir_paths:
                shutil.rmtree(_tmp_dir, ignore_errors=True)
    total = sum(
        1
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    )
    if skipped_names:
        print("直跑跳过：" + "、".join(skipped_names))
    if skipped:
        print(
            f"\n执行完成（{ran} 个通过；{skipped} 个按条件跳过 / "
            "需 pytest 专用夹具）"
        )
    else:
        print(f"\n全部通过（{ran}/{total} 个用例）")


def test_sessions_base_cache_invalidated_after_import():
    """导入后显式失效 /api/sessions 聚合缓存（净零指纹也不会命中旧列表）。"""
    from webapp import server as server_module

    user_id = "u-cache-invalidate"
    data = {
        "chat_history": [
            {
                "role": "user",
                "content": "甲",
                "timestamp": "2026-01-01T00:00:00",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": "乙",
                "timestamp": "2026-01-01T00:00:01",
                "session_id": "s1",
            },
        ],
        "session_titles": {},
        "pinned_sessions": [],
        "archived_sessions": [],
    }
    server_module._SESSIONS_BASE_CACHE.clear()
    try:
        first, _count = server_module._sessions_base_for_user(
            user_id, data, "exclude"
        )
        again, _count = server_module._sessions_base_for_user(
            user_id, data, "exclude"
        )
        assert again is first, "同指纹应命中缓存"
        server_module._invalidate_sessions_base_cache(user_id)
        fresh, _count = server_module._sessions_base_for_user(
            user_id, data, "exclude"
        )
        assert fresh is not first, "失效后必须重新聚合"
    finally:
        server_module._SESSIONS_BASE_CACHE.clear()
    print("✓ /api/sessions 聚合缓存在导入后失效")
