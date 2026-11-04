"""
选择性 gzip 中间件测试（无需网络 / API Key）

验证按路径排除 SSE 与 minimum_size 行为；与响应 Content-Type 无关。

运行：python tests/test_gzip_middleware.py
"""
import os
import sys
from typing import Any

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from webapp.server import SelectiveGZipMiddleware


async def _endpoint(request):
    return PlainTextResponse("x" * 2000)


def _client(minimum_size: int = 100) -> TestClient:
    inner = Starlette(
        routes=[
            Route("/api/chat/stream", _endpoint),
            Route("/api/other", _endpoint),
        ]
    )
    app: Any = SelectiveGZipMiddleware(inner, minimum_size=minimum_size)
    return TestClient(app)


def test_sse_path_never_compressed():
    """即使响应不是 text/event-stream，/api/chat/stream 也不压缩。"""
    client = _client()
    sse = client.get("/api/chat/stream", headers={"Accept-Encoding": "gzip"})
    assert sse.status_code == 200
    assert "content-encoding" not in sse.headers
    assert sse.text == "x" * 2000

    other = client.get("/api/other", headers={"Accept-Encoding": "gzip"})
    assert other.headers.get("content-encoding") == "gzip"
    assert other.text == "x" * 2000
    print("✓ 中间件按路径排除 SSE（与 Content-Type 无关）")


def test_minimum_size_respected():
    client = _client(minimum_size=5000)
    response = client.get("/api/other", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    print("✓ 小于 minimum_size 的响应不压缩")


if __name__ == "__main__":
    test_sse_path_never_compressed()
    test_minimum_size_respected()
    print("\n全部通过")
