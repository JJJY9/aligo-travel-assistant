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
import logging
import re
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.conversation import ConversationRuntime
from config import LLM_CONFIG, RATE_LIMIT_CONFIG, RESILIENCE_CONFIG
from config_agentscope import create_chat_model, init_agentscope
from context.storage import (
    clear_short_term_session,
    create_long_term_memory,
    describe_backends,
)
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio
from utils.knowledge_stats import knowledge_status
from utils.rate_limit import RateLimiter
from utils.result_formatter import result_to_markdown
from utils.turn_meta import extract_turn_meta

logger = logging.getLogger(__name__)

APP_VERSION = "1.3.0"

# SSE 心跳间隔（秒）：LLM 调用可能持续数十秒，定期发送注释行避免代理/浏览器超时
SSE_HEARTBEAT_SEC = 15.0

# 用户 / 会话 ID 会用于文件名与数据库主键，限制字符集以避免路径穿越等问题
_ID_PATTERN = r"^[\w-]{1,64}$"

# 基础安全响应头：CSP 仅允许同源脚本（主题脚本已拆为 /static/theme.js）；
# 样式允许内联以支持 JS 动态设置 style 属性。
_BASE_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
_SECURITY_HEADERS = _BASE_SECURITY_HEADERS + [
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        b"base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
]
# Swagger UI / ReDoc 需要从 CDN 加载脚本并内联初始化，仅在文档页面放宽 CSP；
# 其它页面仍使用上面的严格策略。
_DOCS_SECURITY_HEADERS = _BASE_SECURITY_HEADERS + [
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        b"img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
        b"font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        b"connect-src 'self'; worker-src blob:; "
        b"base-uri 'none'; form-action 'self'; frame-ancestors 'self'",
    ),
]


def _security_headers_for(path: str) -> list[tuple[bytes, bytes]]:
    """按路径返回安全响应头：文档页面使用放宽 CSP，其它页面保持严格策略。"""
    if path in ("/docs", "/redoc") or path.startswith(("/docs/", "/redoc/")):
        return _DOCS_SECURITY_HEADERS
    return _SECURITY_HEADERS

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "data" / "memory"


class RequestContextMiddleware:
    """为每个 HTTP 请求生成 / 透传 X-Request-ID，并记录 /api/ 请求耗时。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        incoming = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = (
            re.sub(r"[^A-Za-z0-9._-]", "", incoming)[:64] or uuid.uuid4().hex[:12]
        )
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_code = 0
        response_started = False
        path = str(scope.get("path", ""))
        response_headers = _security_headers_for(path)

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 0))
                message["headers"] = (
                    list(message.get("headers") or [])
                    + response_headers
                    + [(b"x-request-id", request_id.encode("latin-1"))]
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - 兜底：未处理异常也要带上安全头与请求 ID
            logger.exception("未处理的请求异常 [%s] %s", request_id, path)
            if response_started:
                # 响应已开始（如流式输出中断），无法再补发 500，交由服务器断开连接
                raise
            try:
                await send_wrapper(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json; charset=utf-8")],
                    }
                )
                await send_wrapper(
                    {
                        "type": "http.response.body",
                        "body": json.dumps(
                            {"detail": f"服务器内部错误，请稍后重试（请求 ID：{request_id}）"},
                            ensure_ascii=False,
                        ).encode("utf-8"),
                    }
                )
            except Exception:  # noqa: BLE001 - 客户端可能已断开
                raise exc from None
        finally:
            if path.startswith("/api/"):
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info(
                    "%s %s -> %s (%.0fms) [%s]",
                    scope.get("method", ""),
                    path,
                    status_code,
                    elapsed_ms,
                    request_id,
                )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="web_user", pattern=_ID_PATTERN)
    session_id: str | None = Field(default=None, pattern=_ID_PATTERN)


_ShortText = Annotated[str, Field(max_length=200)]
PreferenceValue = (
    _ShortText
    | Annotated[list[_ShortText], Field(max_length=20)]
    | int
    | float
    | bool
)


class PreferencePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    value: PreferenceValue


class SessionTitlePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(max_length=60)


class SessionPinPayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    pinned: bool


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
        self.rate_limiter = RateLimiter(
            limit=RATE_LIMIT_CONFIG.get("chat_requests", 30),
            window_sec=RATE_LIMIT_CONFIG.get("window_sec", 60),
        )

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


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _enforce_rate_limit(state: AligoWebApp, user_id: str, request: Request) -> None:
    """对对话接口做简单滑动窗口限流（按 客户端 + 用户）。"""
    client = request.client.host if request.client else "unknown"
    retry_after = state.rate_limiter.check(f"{client}:{user_id}")
    if retry_after is None:
        return
    raise HTTPException(
        status_code=429,
        detail=f"请求过于频繁，请约 {retry_after:.0f} 秒后重试（请求 ID：{_request_id(request)}）",
        headers={"Retry-After": str(retry_after)},
    )


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
            "session_titles": store.get_session_titles(),
            "pinned_sessions": store.get_pinned_sessions(),
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


def _message_search_text(message: dict[str, Any]) -> str:
    """用于全文搜索的文本：助手消息按渲染后的 Markdown 搜索，避免命中的是 JSON 字段名。"""
    if message.get("role") == "assistant":
        return _render_stored_message(message)
    return str(message.get("content", ""))


def _match_snippet(text: str, needle: str, radius: int = 36) -> str:
    """截取命中位置附近的片段（用于会话搜索结果的预览）。"""
    index = text.lower().find(needle)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(needle) + radius)
    snippet = " ".join(text[start:end].split())
    # 片段来自 Markdown 渲染结果，去掉常见标记让预览更干净
    snippet = snippet.replace("**", "").replace("`", "")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet


def _group_sessions(
    history: list[dict[str, Any]],
    limit: int,
    query: str = "",
    titles: dict[str, str] | None = None,
    pinned: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按会话聚合消息；提供 query 时只返回命中该关键字的会话，并附带命中片段。"""
    needle = query.strip().lower()
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
        if needle and "snippet" not in entry:
            snippet = _match_snippet(_message_search_text(message), needle)
            if snippet:
                entry["snippet"] = snippet

    sessions = sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)
    custom_titles = titles or {}
    for entry in sessions:
        custom = custom_titles.get(entry["session_id"])
        if custom:
            entry["title"] = custom

    pinned_ids = set(pinned or [])
    for entry in sessions:
        entry["pinned"] = entry["session_id"] in pinned_ids
    # 稳定排序：置顶会话在前，其余保持最近更新时间倒序
    sessions.sort(key=lambda entry: not entry["pinned"])

    if needle:
        sessions = [
            entry
            for entry in sessions
            if entry.get("snippet") or needle in str(entry.get("title", "")).lower()
        ]
    for entry in sessions:
        if not entry["title"]:
            entry["title"] = "新对话"
    return sessions[:limit]


def _usage_summary(history: list[dict[str, Any]]) -> dict[str, Any]:
    """从历史消息的执行元数据汇总模型用量（旧消息没有 _meta，自动忽略）。"""
    calls = input_tokens = output_tokens = 0
    elapsed_total = 0.0
    elapsed_count = 0
    agent_counts: Counter[str] = Counter()
    by_day: dict[str, dict[str, int]] = {}

    for message in history:
        if message.get("role") != "assistant":
            continue
        meta = extract_turn_meta(str(message.get("content", "")))
        if not meta:
            continue
        usage = meta.get("usage")
        if isinstance(usage, dict):
            message_calls = int(usage.get("calls") or 0)
            calls += message_calls
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)

            timestamp = str(message.get("timestamp") or "")
            try:
                message_day = datetime.fromisoformat(timestamp).date().isoformat()
            except ValueError:
                message_day = ""
            if message_day:
                bucket = by_day.setdefault(message_day, {"calls": 0, "tokens": 0})
                bucket["calls"] += message_calls
                bucket["tokens"] += int(usage.get("total_tokens") or 0)

        elapsed = meta.get("elapsed")
        if isinstance(elapsed, (int, float)):
            elapsed_total += float(elapsed)
            elapsed_count += 1
        for agent in meta.get("agents") or []:
            if isinstance(agent, dict) and agent.get("name"):
                agent_counts[str(agent["name"])] += 1

    today = datetime.now().date()
    recent_days = []
    for offset in range(6, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        bucket = by_day.get(day, {"calls": 0, "tokens": 0})
        recent_days.append({"date": day, "calls": bucket["calls"], "tokens": bucket["tokens"]})

    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "avg_elapsed": round(elapsed_total / elapsed_count, 2) if elapsed_count else 0,
        "top_agents": [
            {"name": name, "count": count} for name, count in agent_counts.most_common(5)
        ],
        "by_day": recent_days,
    }


def _user_data_markdown(user_id: str, data: dict[str, Any]) -> str:
    """把用户长期记忆整理为便于阅读的 Markdown（偏好 / 行程 / 统计 / 最近会话）。"""
    lines = [
        "# Aligo 用户数据导出",
        "",
        f"- 用户：{user_id}",
        f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    preferences = {key: value for key, value in (data.get("preferences") or {}).items() if value}
    lines.extend(["## 用户偏好", ""])
    if preferences:
        for key, value in preferences.items():
            text = "、".join(str(item) for item in value) if isinstance(value, list) else str(value)
            lines.append(f"- {key}：{text}")
    else:
        lines.append("- （暂无）")

    lines.extend(["", "## 最近行程", ""])
    trips = data.get("trip_history") or []
    if trips:
        for trip in trips[-10:]:
            route = f"{trip.get('origin') or '—'} → {trip.get('destination') or '—'}"
            dates = " ~ ".join(
                part for part in (trip.get("start_date"), trip.get("end_date")) if part
            ) or "日期未记录"
            lines.append(f"- {route}（{dates}，{trip.get('purpose') or '出行'}）")
    else:
        lines.append("- （暂无）")

    history = data.get("chat_history") or []
    stats = data.get("statistics") or {}
    usage = _usage_summary(history)
    lines.extend(
        [
            "",
            "## 统计",
            "",
            f"- 历史行程：{stats.get('total_trips', len(trips))}",
            f"- 对话消息：{stats.get('total_messages', len(history))}",
            f"- 模型调用：{usage['calls']} 次，tokens {usage['total_tokens']}",
            "",
        ]
    )

    sessions = _group_sessions(
        history, 10, "", data.get("session_titles") or {}
    )
    lines.extend(["## 最近会话", ""])
    if sessions:
        for session in sessions:
            lines.append(f"- {session['title']}（{session['message_count']} 条消息）")
    else:
        lines.append("- （暂无）")
    lines.append("")
    return "\n".join(lines)


def _ics_date(value: str) -> str | None:
    """把 YYYY-MM-DD 转为 iCalendar 日期（YYYYMMDD）；非法值返回 None。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y%m%d")
    except (TypeError, ValueError):
        return None


def _ics_escape(value: str) -> str:
    """按 RFC 5545 转义文本字段。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _build_trip_ics(trip: dict[str, Any]) -> str | None:
    """把一条行程渲染为全天事件的 iCalendar 文本；缺少出发日期时返回 None。"""
    start = _ics_date(trip.get("start_date") or "")
    if not start:
        return None
    end = _ics_date(trip.get("end_date") or "") or start
    end_exclusive = (datetime.strptime(end, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")

    origin = str(trip.get("origin") or "出发地")
    destination = str(trip.get("destination") or "目的地")
    purpose = str(trip.get("purpose") or "出行")
    summary = f"{origin} → {destination}（{purpose}）"
    description = (
        f"Aligo 行程：{origin} → {destination}，"
        f"{trip.get('start_date')} ~ {trip.get('end_date') or trip.get('start_date')}，{purpose}"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"aligo-{trip.get('trip_id') or 'trip'}-{uuid.uuid4().hex[:8]}@aligo.local"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Aligo//Travel Assistant//CN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start}",
        f"DTEND;VALUE=DATE:{end_exclusive}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(lines)


def create_app(model: Any = None) -> FastAPI:
    """创建 FastAPI 应用（测试时可注入自定义模型）。"""
    state = AligoWebApp(model=model)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.close_all()

    app = FastAPI(title="Aligo 智能旅行助手", version=APP_VERSION, lifespan=lifespan)
    app.state.aligo = state
    app.add_middleware(RequestContextMiddleware)

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

    @app.get("/api/knowledge")
    def knowledge() -> dict[str, Any]:
        """知识库状态（集合 / 片段数 / 是否可用）。"""
        return knowledge_status()

    @app.get("/api/status")
    def status(user_id: str = Query(default="web_user", pattern=_ID_PATTERN)) -> dict[str, Any]:
        data = _load_user_data(user_id)
        preferences = {key: value for key, value in data["preferences"].items() if value}
        return {
            "user_id": user_id,
            "preferences": preferences,
            "trip_count": len(data["trip_history"]),
            "trips": data["trip_history"][-5:],
            "chat_count": len(data["chat_history"]),
            "statistics": data["statistics"],
            "storage": describe_backends(),
            "usage": _usage_summary(data["chat_history"]),
        }

    @app.put("/api/preferences/{pref_type}")
    def update_preference(
        payload: PreferencePayload,
        pref_type: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """新增或覆盖一条用户偏好（记忆面板的手动维护入口）。"""
        store = _open_long_term(payload.user_id)
        try:
            store.save_preference(pref_type, payload.value)
            value = store.get_preference(pref_type)
        finally:
            _close_long_term(store)
        return {"type": pref_type, "value": value}

    @app.delete("/api/preferences/{pref_type}")
    def delete_preference(
        pref_type: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        store = _open_long_term(user_id)
        try:
            removed = store.delete_preference(pref_type)
        finally:
            _close_long_term(store)
        return {"type": pref_type, "deleted": removed}

    @app.get("/api/sessions")
    def list_sessions(
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
        q: str = Query(default="", max_length=100),
    ) -> dict[str, Any]:
        data = _load_user_data(user_id)
        titles = data.get("session_titles") or {}
        pinned = data.get("pinned_sessions") or []
        return {"sessions": _group_sessions(data["chat_history"], limit, q, titles, pinned)}

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

    @app.put("/api/sessions/{session_id}/title")
    def rename_session(
        payload: SessionTitlePayload,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """设置或清除会话自定义标题（空标题恢复自动标题）。"""
        store = _open_long_term(payload.user_id)
        try:
            store.set_session_title(session_id, payload.title)
            saved = (store.get_session_titles() or {}).get(session_id, "")
        finally:
            _close_long_term(store)
        return {"session_id": session_id, "title": saved}

    @app.put("/api/sessions/{session_id}/pin")
    def pin_session(
        payload: SessionPinPayload,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """置顶 / 取消置顶会话。"""
        store = _open_long_term(payload.user_id)
        try:
            store.set_session_pinned(session_id, payload.pinned)
            pinned = store.get_pinned_sessions()
        finally:
            _close_long_term(store)
        return {"session_id": session_id, "pinned": session_id in pinned}

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
    async def chat(request: Request, payload: ChatRequest) -> dict[str, Any]:
        _enforce_rate_limit(state, payload.user_id, request)
        session = state.get_session(payload.user_id, payload.session_id)
        async with state.lock_for(payload.user_id):
            try:
                result = await session.runtime.ask(payload.message)
            except CircuitOpenError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"服务暂时不可用，请稍后再试（请求 ID：{_request_id(request)}）",
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 对外返回友好错误
                raise HTTPException(
                    status_code=500,
                    detail=f"处理失败：{exc}（请求 ID：{_request_id(request)}）",
                ) from exc
        return {"session_id": session.session_id, **result}

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
        """SSE 流式对话：先推送执行进度事件，最后推送完整结果或错误。"""
        _enforce_rate_limit(state, payload.user_id, request)
        rid = _request_id(request)
        session = state.get_session(payload.user_id, payload.session_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def run() -> None:
            try:
                async with state.lock_for(payload.user_id):
                    result = await session.runtime.ask(
                        payload.message, on_event=queue.put_nowait
                    )
                await queue.put({"type": "done", "session_id": session.session_id, **result})
            except asyncio.CancelledError:
                raise
            except CircuitOpenError:
                await queue.put(
                    {
                        "type": "error",
                        "message": f"服务暂时不可用，请稍后再试（请求 ID：{rid}）",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 通过事件返回友好错误
                await queue.put(
                    {"type": "error", "message": f"处理失败：{exc}（请求 ID：{rid}）"}
                )

        async def event_stream() -> Any:
            task = asyncio.create_task(run())
            heartbeat: asyncio.Task[None] | None = None

            async def send_heartbeat() -> None:
                while True:
                    await asyncio.sleep(SSE_HEARTBEAT_SEC)
                    await queue.put({"type": "heartbeat"})

            if SSE_HEARTBEAT_SEC > 0:
                heartbeat = asyncio.create_task(send_heartbeat())
            try:
                while True:
                    event = await queue.get()
                    if event.get("type") == "heartbeat":
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event["type"] in ("done", "error"):
                        break
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
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

    @app.get("/api/trips/{trip_id}/calendar")
    def export_trip_calendar(
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出一条行程为 iCalendar（.ics）全天事件。"""
        data = _load_user_data(user_id)
        trip = next(
            (item for item in data["trip_history"] if str(item.get("trip_id")) == trip_id),
            None,
        )
        if trip is None:
            raise HTTPException(status_code=404, detail="行程不存在")
        content = _build_trip_ics(trip)
        if content is None:
            raise HTTPException(status_code=400, detail="该行程缺少出发日期，无法生成日历")
        return Response(
            content=content,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="aligo_{_safe_filename(trip_id, "trip")}.ics"'
                )
            },
        )

    @app.get("/api/export")
    def export(
        user_id: str = Query(..., pattern=_ID_PATTERN),
        format: Literal["json", "md"] = Query(default="json"),
    ) -> Response:
        data = _load_user_data(user_id)
        safe_user = _safe_filename(user_id)
        if format == "md":
            return Response(
                content=_user_data_markdown(user_id, data),
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="aligo_{safe_user}.md"'
                },
            )

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
                "Content-Disposition": f'attachment; filename="aligo_{safe_user}.json"'
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
