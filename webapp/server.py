"""
Aligo Web 界面（FastAPI）

启动：
    python -m webapp.server
默认地址 http://127.0.0.1:8000

接口：
    GET  /              前端页面
    POST /api/chat      发送消息（JSON: message / user_id / session_id）
    GET  /api/status    用户数据概览（偏好 / 行程数 / 统计）
    GET  /api/export    导出用户长期记忆 JSON（附件下载）
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.conversation import ConversationRuntime
from config import RESILIENCE_CONFIG
from config_agentscope import create_chat_model, init_agentscope
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "data" / "memory"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="web_user", min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)


class _WebSession:
    """一个浏览器会话（对应一个 ConversationRuntime）。"""

    def __init__(self, user_id: str, session_id: str, model: Any, breaker: CircuitBreaker) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.runtime = ConversationRuntime(
            user_id=user_id,
            session_id=session_id,
            model=model,
            circuit_breaker=breaker,
        )


class AligoWebApp:
    """应用状态：共享模型 / 熔断器 / 会话表。"""

    def __init__(self, model: Any = None) -> None:
        init_agentscope()
        self.model = model if model is not None else create_chat_model()
        config = RESILIENCE_CONFIG
        self.breaker = CircuitBreaker(
            failure_threshold=int(config.get("circuit_failure_threshold", 5)),
            recovery_timeout_sec=float(config.get("circuit_recovery_timeout_sec", 60.0)),
            half_open_successes=int(config.get("circuit_half_open_successes", 2)),
        )
        self.sessions: dict[str, _WebSession] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def get_session(self, user_id: str, session_id: str | None) -> _WebSession:
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id:
            session = _WebSession(user_id, session_id, self.model, self.breaker)
            self.sessions[session_id] = session
        return session

    def lock_for(self, user_id: str) -> asyncio.Lock:
        """同一用户的请求串行处理，避免长期记忆并发写覆盖。"""
        if user_id not in self.locks:
            self.locks[user_id] = asyncio.Lock()
        return self.locks[user_id]


def _read_user_memory(user_id: str) -> dict[str, Any]:
    """只读地读取用户长期记忆文件（不存在时返回空结构）。"""
    empty = {
        "user_id": user_id,
        "preferences": [],
        "trip_history": [],
        "chat_history": [],
        "statistics": {},
    }
    path = MEMORY_DIR / f"{user_id}.json"
    if not path.is_file():
        return empty
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty


def create_app(model: Any = None) -> FastAPI:
    """创建 FastAPI 应用（测试时可注入自定义模型）。"""
    app = FastAPI(title="Aligo 智能旅行助手", version="1.1.0")
    state = AligoWebApp(model=model)
    app.state.aligo = state

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status(user_id: str = Query(default="web_user", min_length=1, max_length=64)) -> dict[str, Any]:
        memory = _read_user_memory(user_id)
        preferences = {
            item.get("type"): item.get("value")
            for item in memory.get("preferences") or []
            if isinstance(item, dict) and item.get("type")
        }
        return {
            "user_id": user_id,
            "preferences": preferences,
            "trip_count": len(memory.get("trip_history") or []),
            "statistics": memory.get("statistics") or {},
        }

    @app.post("/api/chat")
    async def chat(request: ChatRequest) -> dict[str, Any]:
        session = state.get_session(request.user_id, request.session_id)
        async with state.lock_for(request.user_id):
            try:
                result = await session.runtime.ask(request.message)
            except CircuitOpenError as exc:
                raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后再试") from exc
            except Exception as exc:  # noqa: BLE001 - 对外返回友好错误
                raise HTTPException(status_code=500, detail=f"处理失败：{exc}") from exc
        return {"session_id": session.session_id, **result}

    @app.get("/api/export")
    def export(user_id: str = Query(..., min_length=1, max_length=64)) -> Response:
        memory = _read_user_memory(user_id)
        payload = json.dumps(memory, ensure_ascii=False, indent=2)
        return Response(
            content=payload,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="aligo_{user_id}.json"'},
        )

    return app


def main() -> None:
    """启动 Web 服务（默认仅监听本机 8000 端口）。

    应用在启动时创建（而非模块导入时）：缺少 API Key 等配置错误只会让启动失败，
    不会影响其它模块导入（例如 CI 中导入 create_app 进行 API 测试）。
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="启动 Aligo Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args()

    configure_stdio()
    try:
        application = create_app()
    except Exception as exc:  # noqa: BLE001 - 启动失败时给出友好提示
        print(f"启动失败：{exc}")
        print("请先配置 API Key（环境变量 LLM_API_KEY，或创建 config_local.py）")
        raise SystemExit(1) from exc

    uvicorn.run(application, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
