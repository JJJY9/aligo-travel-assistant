"""
Aligo Web 界面（FastAPI）

启动：
    python -m webapp.server
默认地址 http://127.0.0.1:8000

接口：
    GET    /                    前端页面
    POST   /api/chat            发送消息（一次性返回完整结果）
    POST   /api/chat/stream     发送消息（SSE 流式返回执行进度，最后返回完整结果）
    GET    /api/health          服务 / 模型 / 存储状态
    GET    /api/status          用户数据概览（偏好 / 行程 / 统计）
    GET    /api/sessions        历史会话列表（按最近更新时间倒序）
    GET    /api/sessions/{id}   某个会话的消息（助手消息已渲染为 Markdown）
    DELETE /api/sessions/{id}   删除某个会话
    GET    /api/export          导出用户长期记忆 JSON（附件下载）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.conversation import ConversationRuntime
from config import LLM_CONFIG, RESILIENCE_CONFIG
from config_agentscope import create_chat_model, init_agentscope
from context.storage import (
    clear_short_term_session,
    create_long_term_memory,
    describe_backends,
)
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio
from utils.result_formatter import result_to_markdown
from utils.turn_meta import extract_turn_meta

APP_VERSION = "1.2.0"

# 用户 / 会话 ID 会用于文件名与数据库主键，限制字符集以避免路径穿越等问题
_ID_PATTERN = r"^[\w-]{1,64}$"

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "data" / "memory"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="web_user", pattern=_ID_PATTERN)
    session_id: str | None = Field(default=None, pattern=_ID_PATTERN)


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

    def close(self) -> None:
        """释放会话资源（PostgreSQL 连接等）。"""
        self.runtime.close()


class AligoWebApp:
    """应用状态：共享模型 / 熔断器 / 会话表。"""

    # 进程内缓存的会话上限（超出后按最久未使用淘汰，避免长时间运行内存与连接增长）。
    # 每个会话在 PostgreSQL 后端下持有 1 个连接，保守取 50，避免撞上数据库连接上限。
    max_sessions = 50

    def __init__(self, model: Any = None) -> None:
        init_agentscope()
        self.model = model if model is not None else create_chat_model()
        config = RESILIENCE_CONFIG
        self.breaker = CircuitBreaker(
            failure_threshold=int(config.get("circuit_failure_threshold", 5)),
            recovery_timeout_sec=float(config.get("circuit_recovery_timeout_sec", 60.0)),
            half_open_successes=int(config.get("circuit_half_open_successes", 2)),
        )
        self.sessions: OrderedDict[str, _WebSession] = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}

    def get_session(self, user_id: str, session_id: str | None) -> _WebSession:
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
        session = self.sessions.get(session_id)
        if session is not None and session.user_id != user_id:
            # 同一会话 ID 被另一个用户使用：丢弃旧会话，避免串用记忆
            self.drop_session(session_id)
            session = None
        if session is None:
            session = _WebSession(user_id, session_id, self.model, self.breaker)
            self.sessions[session_id] = session
            while len(self.sessions) > self.max_sessions:
                _, evicted = self.sessions.popitem(last=False)
                evicted.close()
        else:
            self.sessions.move_to_end(session_id)
        return session

    def drop_session(self, session_id: str) -> None:
        """移除并关闭一个会话。"""
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        """关闭所有会话（进程退出时调用）。"""
        for session in list(self.sessions.values()):
            session.close()
        self.sessions.clear()

    def lock_for(self, user_id: str) -> asyncio.Lock:
        """同一用户的请求串行处理，避免长期记忆并发写覆盖。"""
        if user_id not in self.locks:
            self.locks[user_id] = asyncio.Lock()
        return self.locks[user_id]


# ---------- 长期记忆读取 ----------


def _open_long_term(user_id: str) -> Any:
    """按当前配置打开长期记忆（JSON 或 PostgreSQL）。"""
    return create_long_term_memory(user_id, str(MEMORY_DIR))


def _close_long_term(store: Any) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def _safe_filename(value: str, fallback: str = "aligo") -> str:
    """把用户 / 会话 ID 转为只含 ASCII 的安全下载文件名。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return cleaned or fallback


def _load_user_data(user_id: str) -> dict[str, Any]:
    store = _open_long_term(user_id)
    try:
        return {
            "preferences": store.get_preference(),
            "trip_history": store.get_trip_history(limit=None),
            "chat_history": store.get_chat_history(),
            "statistics": store.get_statistics(),
        }
    finally:
        _close_long_term(store)


def _render_stored_message(message: dict[str, Any]) -> str:
    """把历史消息转换为可展示文本：助手消息存的是编排结果 JSON，需重新渲染为 Markdown。"""
    role = message.get("role", "assistant")
    content = str(message.get("content", ""))
    if role != "assistant":
        return content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(data, dict) and (
        "results" in data or data.get("status") in {"completed", "partial_failure", "no_agents"}
    ):
        try:
            return result_to_markdown(data)
        except Exception:  # noqa: BLE001 - 渲染失败时退回原始内容
            return content
    return content


def _group_sessions(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for message in history:
        session_id = message.get("session_id")
        if not session_id:
            continue
        entry = grouped.setdefault(
            str(session_id),
            {"session_id": str(session_id), "title": "", "message_count": 0, "updated_at": ""},
        )
        entry["message_count"] += 1
        timestamp = str(message.get("timestamp") or "")
        if timestamp:
            entry["updated_at"] = timestamp
        if not entry["title"] and message.get("role") == "user":
            title = " ".join(str(message.get("content", "")).split())
            if title:
                entry["title"] = title[:40]

    sessions = sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)
    for entry in sessions:
        if not entry["title"]:
            entry["title"] = "新对话"
    return sessions[:limit]


def create_app(model: Any = None) -> FastAPI:
    """创建 FastAPI 应用（测试时可注入自定义模型）。"""
    state = AligoWebApp(model=model)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.close_all()

    app = FastAPI(title="Aligo 智能旅行助手", version=APP_VERSION, lifespan=lifespan)
    app.state.aligo = state

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": APP_VERSION,
            "model": LLM_CONFIG.get("model_name", ""),
            "has_api_key": bool(LLM_CONFIG.get("api_key")),
            "storage": describe_backends(),
            "time": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/status")
    def status(user_id: str = Query(default="web_user", pattern=_ID_PATTERN)) -> dict[str, Any]:
        data = _load_user_data(user_id)
        preferences = {key: value for key, value in data["preferences"].items() if value}
        return {
            "user_id": user_id,
            "preferences": preferences,
            "trip_count": len(data["trip_history"]),
            "chat_count": len(data["chat_history"]),
            "statistics": data["statistics"],
            "storage": describe_backends(),
        }

    @app.get("/api/sessions")
    def list_sessions(
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        data = _load_user_data(user_id)
        return {"sessions": _group_sessions(data["chat_history"], limit)}

    @app.get("/api/sessions/{session_id}")
    def get_session_messages(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        store = _open_long_term(user_id)
        try:
            messages = store.get_chat_history(session_id=session_id)
        finally:
            _close_long_term(store)
        return {
            "session_id": session_id,
            "messages": [
                {
                    "role": message.get("role", "assistant"),
                    "content": _render_stored_message(message),
                    "meta": extract_turn_meta(str(message.get("content", "")))
                    if message.get("role") == "assistant"
                    else None,
                    "timestamp": message.get("timestamp", ""),
                }
                for message in messages
            ],
        }

    @app.get("/api/sessions/{session_id}/export")
    def export_session(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """把一个会话导出为 Markdown（便于保存或分享行程）。"""
        store = _open_long_term(user_id)
        try:
            messages = store.get_chat_history(session_id=session_id)
        finally:
            _close_long_term(store)
        if not messages:
            raise HTTPException(status_code=404, detail="会话不存在或没有消息")

        title = "Aligo 对话"
        for message in messages:
            if message.get("role") == "user":
                title = " ".join(str(message.get("content", "")).split())[:40] or title
                break

        lines = [
            f"# {title}",
            "",
            f"- 用户：{user_id}",
            f"- 会话：{session_id}",
            f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
        ]
        for message in messages:
            is_user = message.get("role") == "user"
            role = "用户" if is_user else "Aligo"
            content = (
                str(message.get("content", ""))
                if is_user
                else _render_stored_message(message)
            )
            lines.extend([f"## {role}", "", content.strip(), ""])

        filename = f"aligo_{_safe_filename(session_id, 'conversation')}.md"
        return Response(
            content="\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.delete("/api/sessions/{session_id}")
    def delete_session(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        store = _open_long_term(user_id)
        try:
            removed = store.delete_chat_session(session_id)
        finally:
            _close_long_term(store)
        state.drop_session(session_id)
        clear_short_term_session(session_id)
        return {"session_id": session_id, "deleted": removed}

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

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        """SSE 流式对话：先推送执行进度事件，最后推送完整结果或错误。"""
        session = state.get_session(request.user_id, request.session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def run() -> None:
            try:
                async with state.lock_for(request.user_id):
                    result = await session.runtime.ask(
                        request.message, on_event=queue.put_nowait
                    )
                await queue.put({"type": "done", "session_id": session.session_id, **result})
            except asyncio.CancelledError:
                raise
            except CircuitOpenError:
                await queue.put({"type": "error", "message": "服务暂时不可用，请稍后再试"})
            except Exception as exc:  # noqa: BLE001 - 通过事件返回友好错误
                await queue.put({"type": "error", "message": f"处理失败：{exc}"})

        async def event_stream() -> Any:
            task = asyncio.create_task(run())
            try:
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event["type"] in ("done", "error"):
                        break
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/export")
    def export(user_id: str = Query(..., pattern=_ID_PATTERN)) -> Response:
        data = _load_user_data(user_id)
        payload = json.dumps(
            {
                "user_id": user_id,
                "exported_at": datetime.now().isoformat(),
                **data,
            },
            ensure_ascii=False,
            indent=2,
        )
        return Response(
            content=payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="aligo_{_safe_filename(user_id)}.json"'
            },
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
