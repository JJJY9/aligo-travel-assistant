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
import bisect
import contextlib
import ipaddress
import json
import reprlib
import logging
import math
import re
import threading
import time
import weakref
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues, _worker
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.middleware.gzip import GZipMiddleware

from agents.conversation import ConversationRuntime
from config import LLM_CONFIG, RATE_LIMIT_CONFIG, RESILIENCE_CONFIG, STORAGE_CONFIG
from config_agentscope import create_chat_model, init_agentscope, wrap_usage_tracking
from context.storage import (
    clear_short_term_session,
    create_long_term_memory,
    create_short_term_memory,
    describe_backends,
    get_shared_redis_client,
)
from utils.access_log import JsonLogFormatter, access_log_format, format_access_log
from utils.errors import MemoryPersistenceError, UserFacingError
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio
from utils.doctor import run_doctor
from utils.knowledge_admin import (
    JOINED_MAX_CHARS,
    METADATA_KEY_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
    add_document,
    cleanup_duplicate_contents,
    delete_document,
    export_documents,
    find_duplicate_contents,
    get_document,
    get_document_group,
    import_documents,
    list_categories,
    list_documents,
    search_documents,
    update_document,
)
from utils.knowledge_stats import knowledge_status
from utils.metrics import REQUEST_METRICS
from utils.preference_values import has_preference_value
from utils.rate_limit import create_rate_limiter
from utils.session_ids import RESERVED_SESSION_IDS
from utils.redis_lock import DistributedLock
from utils.stored_message import render_stored_message
from utils.session_html import render_all_sessions_html, render_session_html
from utils.session_markdown import (
    render_all_sessions_markdown,
    sanitize_body,
    sanitize_title,
)
from utils.skill_paths import resolve_skills_root, skill_scripts
from utils.trip_conflicts import filter_trips_by_query as _filter_trips_by_query
from utils.trip_conflicts import trip_state_label as _trip_state_label
from utils.trip_conflicts import find_saved_trip as _find_saved_trip
from utils.trip_conflicts import trip_conflicts as _trip_conflicts
from utils.trip_conflicts import trip_interval as _trip_interval
from utils.trip_conflicts import trips_with_conflicts as _trips_with_conflicts
from utils.trip_dedupe import apply_trip_dedupe, plan_trip_dedupe
from utils.trip_export import (
    render_trip_ics,
    render_trips_csv,
    render_trips_html,
    render_trips_ics,
)
from utils.trip_import import import_trips_from_text, normalize_identity_date
from utils.travel_stats import summarize_travel
from utils.timestamps import chronological_key
from utils.turn_meta import extract_turn_meta
from utils.version import APP_VERSION
from utils.weather import WeatherError, fetch_weather

logger = logging.getLogger(__name__)

# SSE 心跳间隔（秒）：LLM 调用可能持续数十秒，定期发送注释行避免代理/浏览器超时
SSE_HEARTBEAT_SEC = 15.0
# 请求体大小上限（JSON 接口都是小请求；防止超大 body 在 Pydantic 校验前耗尽内存）
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
# /api/import 要一次性接收整份备份（面板文件上限 50 MB，JSON 包装 / 转义
# 还有额外开销）；其它路径维持 2 MB
IMPORT_REQUEST_BODY_BYTES = 64 * 1024 * 1024
_BODY_LIMIT_OVERRIDES = {"/api/import": IMPORT_REQUEST_BODY_BYTES}


def body_limit_for(path: str, default: int = MAX_REQUEST_BODY_BYTES) -> int:
    """路径级请求体上限。"""
    return _BODY_LIMIT_OVERRIDES.get(str(path or ""), default)


def _route_path(scope: dict[str, Any]) -> str:
    """路由视角的请求路径：剥离 root_path（子路径部署）并归一尾斜杠。

    中间件在路由之前执行，看到的 ``scope["path"]`` 在 uvicorn ``--root-path``
    或父级 Mount 部署下带前缀，且 ``/api/import/``（尾斜杠）会在 Starlette
    307 归一之前就到达这里——不归一会让路径级上限 / no-store / 文档 CSP /
    访问日志与请求指标同时失配。
    """
    raw = str(scope.get("path", "") or "/")
    root = str(scope.get("root_path", "") or "")
    if root and raw.startswith(root):
        raw = raw[len(root):] or "/"
    if raw != "/":
        raw = raw.rstrip("/") or "/"
    return raw

# 只有环回地址视为「仅本机」；其它地址启动时提示需要反向代理做认证 / HTTPS
def _is_loopback_host(host: str) -> bool:
    value = host.strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False

# 用户 / 会话 ID 会用于文件名与数据库主键，限制字符集以避免路径穿越等问题
_ID_PATTERN = r"^[\w-]{1,64}$"
# 与静态路由冲突的会话 ID（清单与 CLI 共用）：GET /api/sessions/export 永远
# 命中导出接口，这类会话无法查看 / 继续（聊天请求与备份恢复都必须拒绝）
_RESERVED_SESSION_IDS = RESERVED_SESSION_IDS
# 偏好类型可由模型 / 面板自定义（可能含空格等），只限制长度与非空
_PREF_TYPE_PATTERN = r"^[^/]{1,64}$"
# 会话删除墓碑在 Redis 中的 key 前缀（配置 REDIS_URL 时跨实例共享）
_DELETED_KEY_PREFIX = "aligo:deleted"

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
# 未匹配路由（404 / 405）的指标路径统一取值，避免任意路径撑爆指标基数
_UNMATCHED_METRIC_PATH = "__unmatched__"
# 路由前就被 413 拒绝（Content-Length 预检）的请求没有 route：已知允许
# 请求体的接口按接口名归一标签（与 chunked 超限记录到路由模板的口径一致），
# 未知路径仍归入 __unmatched__——任意路径不能撑爆指标基数
_413_METRIC_PATHS = frozenset(
    {
        "/api/chat",
        "/api/chat/stream",
        "/api/import",
        "/api/knowledge/import",
        "/api/knowledge/import/preview",
        "/api/knowledge/documents",
        "/api/trips",
        "/api/trips/import",
        "/api/trips/dedupe",
    }
)
_413_METRIC_PATTERNS = (
    (
        re.compile(r"^/api/knowledge/documents/\d+$"),
        "/api/knowledge/documents/{doc_id}",
    ),
    (re.compile(r"^/api/trips/\d+$"), "/api/trips/{trip_id}"),
    (
        re.compile(r"^/api/sessions/[\w-]+/last-turn$"),
        "/api/sessions/{session_id}/last-turn",
    ),
)


def _metric_path_before_route(route_path: str) -> str:
    """路由前 413 的指标标签：已知接口归一，未知路径归 __unmatched__。"""
    if route_path in _413_METRIC_PATHS:
        return route_path
    for pattern, label in _413_METRIC_PATTERNS:
        if pattern.fullmatch(route_path):
            return label
    return _UNMATCHED_METRIC_PATH


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
        route_path = _route_path(scope)
        response_headers = _security_headers_for(route_path)

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 0))
                headers_list = list(message.get("headers") or [])
                # 用户数据接口不进入浏览器 / 代理缓存；响应自己声明了 Cache-Control
                # （如导出接口的 no-cache）时保持原样，避免重复头
                if not any(
                    key.lower() == b"cache-control" for key, _ in headers_list
                ):
                    if route_path.startswith("/api/"):
                        headers_list.append((b"cache-control", b"no-store"))
                    elif route_path.startswith("/static/"):
                        # 静态资源每次重新验证：ETag 命中只回 304；避免
                        # 新版 app.js 与旧 index.html 之类的混合缓存
                        headers_list.append((b"cache-control", b"no-cache"))
                message["headers"] = (
                    headers_list
                    + response_headers
                    + [(b"x-request-id", request_id.encode("latin-1"))]
                )
            await send(message)

        try:
            content_length = headers.get(b"content-length", b"")
            if content_length:
                try:
                    body_size = int(content_length)
                except ValueError:
                    body_size = 0
                limit = body_limit_for(route_path)
                if body_size > limit:
                    detail = f"请求体过大（上限 {limit // (1024 * 1024)} MB）"
                    await send_wrapper(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [
                                (b"content-type", b"application/json; charset=utf-8"),
                                # 413 时没有读取请求体：keep-alive 复用会让
                                # 后续请求被当成剩余 body 吞掉（连接卡死）。
                                # 明确关闭连接，客户端重新建连重试
                                (b"connection", b"close"),
                            ],
                        }
                    )
                    await send_wrapper(
                        {
                            "type": "http.response.body",
                            "body": json.dumps(
                                {"detail": detail}, ensure_ascii=False
                            ).encode("utf-8"),
                        }
                    )
                    return
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
            if route_path.startswith("/api/"):
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info(
                    "%s",
                    format_access_log(
                        scope.get("method", ""),
                        path,
                        status_code,
                        elapsed_ms,
                        request_id,
                    ),
                )
                # 指标用路由模板（/api/sessions/{session_id}）而不是原始路径：
                # 会话 ID 等动态段不会撑爆基数，未匹配的 404 统一归入 __unmatched__
                route = scope.get("route")
                metric_path = getattr(route, "path", None) or _UNMATCHED_METRIC_PATH
                # 路由前就被 413 拒绝的请求没有 route：按已知接口归一标签，
                # 未知路径归 __unmatched__（与 chunked 超限口径一致）
                if getattr(route, "path", None) is None and status_code == 413:
                    metric_path = _metric_path_before_route(route_path)
                REQUEST_METRICS.record(
                    scope.get("method", ""),
                    metric_path,
                    status_code,
                    elapsed_ms,
                    # SSE 长连接的整段持续时间不能计入延迟分位
                    streaming=metric_path == "/api/chat/stream",
                )


class _BodyTooLarge(Exception):
    """请求体超过上限的内部信号（由 BodySizeLimitMiddleware 转换为 413）。"""


class BodySizeLimitMiddleware:
    """按实际读取字节数限制请求体大小。

    ``RequestContextMiddleware`` 只检查 ``Content-Length``；分块传输
    （``Transfer-Encoding: chunked``）或 HTTP/2 请求没有该头，若不在这里
    按实际读取量截断，超大请求体会被完整读入内存，绕过 2 MB 上限。
    """

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        received = 0
        exceeded = False
        sent_413 = False
        limit = body_limit_for(_route_path(scope), self.max_bytes)
        payload = json.dumps(
            {"detail": f"请求体过大（上限 {limit // (1024 * 1024)} MB）"},
            ensure_ascii=False,
        ).encode("utf-8")

        async def send_413() -> None:
            nonlocal sent_413
            if sent_413:
                return
            sent_413 = True
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        # 分块流在中止处停止读取：keep-alive 复用会让剩余的
                        # 请求体被当成后续请求解析（或触发 uvicorn 协议错误），
                        # 关闭连接让客户端重新建连
                        (b"connection", b"close"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        async def limited_send(message: dict[str, Any]) -> None:
            if not exceeded:
                await send(message)
                return
            # 应用可能在捕获异常后照常发送 500 / 200，甚至忽略异常继续输出；
            # 统一改写为 413，并丢弃其响应体
            if message.get("type") == "http.response.start":
                await send_413()
            elif message.get("type") not in (
                "http.response.body",
                "http.response.trailers",
            ):
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _BodyTooLarge:
            pass
        # 应用吞掉异常且没有发出任何响应时，在这里补发 413
        if exceeded:
            await send_413()


class SelectiveGZipMiddleware:
    """对除 SSE 流以外的响应启用 gzip。

    SSE 需要逐事件即时送达，压缩会引入缓冲并破坏实时进度，因此按路径排除。
    """

    def __init__(self, app: Any, minimum_size: int = 1024) -> None:
        self.app = app
        self.compressed_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and _route_path(scope) == "/api/chat/stream":
            await self.app(scope, receive, send)
            return
        await self.compressed_app(scope, receive, send)


def _text_write_problem(value: object, depth: int = 0) -> str | None:
    """实时写入路径的文本校验（与备份恢复 utils/user_import 同一口径）。

    PostgreSQL 的 text 列拒绝 NUL（0x00），JSONB 里的 ``\\u0000`` 也会被拒，
    孤立代理项在 UTF-8 写盘时编码失败——两后端行为分叉且都不可恢复，
    在入口按 422 拒绝比写库后永久失败便宜。
    """
    if depth > 16:
        return "嵌套过深"
    if isinstance(value, float):
        # 非有限数写进 JSONB 会被 PostgreSQL 确定性拒绝、JSON 后端写出
        # 非标准文件：与 PUT /api/preferences 的既有护栏保持一致
        if not math.isfinite(value):
            return "包含非有限数（NaN / Infinity）"
        return None
    if isinstance(value, str):
        if "\x00" in value:
            return "包含 NUL 字符"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "包含无法编码的字符（孤立代理项）"
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            problem = _text_write_problem(item, depth + 1)
            if problem:
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            problem = _text_write_problem(str(key), depth + 1) or _text_write_problem(
                item, depth + 1
            )
            if problem:
                return problem
    return None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="web_user", pattern=_ID_PATTERN)
    session_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    # 客户端生成的回合标识：写入助手消息的 _meta，停止 / 断网后的前端
    # 对账用它精确判断这一轮是否已落库（文本比较会把重复提问误判成已保存）
    client_turn_id: str | None = Field(default=None, pattern=_ID_PATTERN)

    @field_validator("session_id")
    @classmethod
    def _session_id_not_reserved(cls, value: str | None) -> str | None:
        # /api/sessions/export 是静态路由，名为 export 的会话永远打不开
        if value in _RESERVED_SESSION_IDS:
            raise ValueError(f"session_id 不能使用保留字 {value}")
        return value

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        # min_length 在 strip 之前校验，"   " 会通过并浪费一次 LLM 调用
        if not value.strip():
            raise ValueError("消息不能为空")
        problem = _text_write_problem(value)
        if problem:
            raise ValueError(f"消息{problem}")
        return value


# 偏好值可以是任意 JSON（与 /api/status 回显、备份恢复落库的形状一致）：
# 历史上导入 / 模型写入过对象、数值列表等，面板未修改直接保存不能 422。
# 具体约束（文本可写、非有限数、序列化大小上限）在 PreferencePayload 的
# 校验器里执行。
_PREFERENCE_VALUE_MAX_JSON = 10000


_KnowledgeMetaKey = Annotated[str, Field(max_length=METADATA_KEY_MAX_CHARS)]
_KnowledgeMetaValue = Annotated[str, Field(max_length=METADATA_VALUE_MAX_CHARS)]


class KnowledgeDocumentPayload(BaseModel):
    """新增知识片段（记忆面板维护入口）。"""

    content: str = Field(min_length=1, max_length=2000)
    # 限制键数 / 键值长度：metadata 原样写入向量库并在列表接口返回，
    # 不放任超大对象拖垮后续读取。省略时为 None：更新接口据此沿用库中
    # 现有元数据（与 CLI kb-update 一致），新增接口按「无元数据」处理
    metadata: dict[_KnowledgeMetaKey, _KnowledgeMetaValue] | None = Field(
        default=None, max_length=10
    )


class KnowledgeUpdatePayload(KnowledgeDocumentPayload):
    """更新知识片段：支持整份文档（整组）编辑后按 400 字重新拆分。"""

    # 文档大小上限 20000 字（分片长度之和）；回填的拼接文本含分片间分隔符，
    # 上限略高（JOINED_MAX_CHARS），面板按 size 字段判断是否可编辑
    content: str = Field(min_length=1, max_length=JOINED_MAX_CHARS)

    @field_validator("metadata", mode="before")
    @classmethod
    def _drop_derived_chunk_keys(cls, value: object) -> object:
        """丢弃回传的派生分片元数据（part/parts/group）。

        面板整份编辑会把读到的完整 metadata 原样提交；这些派生键由服务端
        按新分片重写，先丢弃再校验，避免 8+ 用户键的记录因 11 个键被 422。
        """
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if key not in {"part", "parts", "group"}
            }
        return value


class KnowledgeImportPayload(BaseModel):
    """批量导入知识片段（单次最多 20000 条）。

    面板按 200 条分批；多分片文档的 group 必须整组落在同一批，因此单批
    上限放宽到 20000（单组可有数百片，例如按 FAQ 小节切分的长文档）。
    请求体仍受 2 MB 上限约束。条目放宽为任意 JSON 值：非对象 / 空内容 /
    超长字段由 import_documents 统一跳过或截断，避免一条不合规就让整个
    备份无法恢复（与 CLI 行为一致）。
    """

    documents: list[Any] = Field(min_length=1, max_length=20000)


class KnowledgeImportPreviewPayload(BaseModel):
    """预览导入（不写库）：可一次提交整个备份文件，条目规则与导入一致。"""

    documents: list[Any] = Field(min_length=1, max_length=20000)


class KnowledgeDedupePayload(BaseModel):
    """重复片段清理请求。

    ``dry_run`` 默认 True（只预览）。必须走 JSON body 而不是 URL 参数：
    跨站表单发不出 application/json（会先触发 CORS 预检并被拒绝），
    破坏性接口不能被第三方页面用无预检的简单 POST 触发。
    """

    dry_run: bool = True


class PreferencePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    # 任意 JSON：与 /api/status 能回显、备份恢复能落库的形状一致（对象 /
    # 数值列表 / 长文本），面板对存量值「未修改直接保存」不能 422
    value: Any

    @field_validator("value")
    @classmethod
    def _value_is_writable(cls, value: object) -> object:
        problem = _text_write_problem(value)
        if problem:
            raise ValueError(f"偏好值{problem}")
        try:
            size = len(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("偏好值不是可序列化的 JSON") from exc
        if size > _PREFERENCE_VALUE_MAX_JSON:
            raise ValueError(
                f"偏好值过大（序列化后超过 {_PREFERENCE_VALUE_MAX_JSON} 字符）"
            )
        return value


class LastTurnPayload(BaseModel):
    """删除最后一轮的请求体（POST 形式，长提问的 expect 不再受查询串上限）。"""

    user_id: str = Field(pattern=_ID_PATTERN)
    expect: str | None = Field(default=None, max_length=200000)


_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class TripPayload(BaseModel):
    """手动添加行程（记忆面板入口）。"""

    user_id: str = Field(pattern=_ID_PATTERN)
    origin: str = Field(default="", max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    start_date: str = Field(pattern=_DATE_PATTERN)
    end_date: str | None = Field(default=None, pattern=_DATE_PATTERN)
    purpose: str = Field(default="出差", max_length=100)

    @field_validator("destination")
    @classmethod
    def _destination_not_blank(cls, value: str) -> str:
        # 纯空白通过 min_length，但去重按空目的地跳过，会不断产生重复记录
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("目的地不能为空")
        problem = _text_write_problem(cleaned)
        if problem:
            raise ValueError(f"目的地{problem}")
        return cleaned

    @field_validator("origin", "purpose")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        problem = _text_write_problem(cleaned)
        if problem:
            raise ValueError(f"行程字段{problem}")
        return cleaned


class TripImportPayload(BaseModel):
    """从 CSV / iCalendar 文本导入行程（记忆面板文件选择）。

    必须走 JSON body：跨站表单无法发送 application/json。``content`` 上限
    50 万字符：JSON 序列化后中文约 1.5 MB、转义最多的 ASCII 约 1 MB，仍在
    2 MB 请求体上限内（100 万字符的中文会超过 2 MB，永远无法送达）。
    """

    content: str = Field(min_length=1, max_length=500_000)


class TripUpdatePayload(BaseModel):
    """更新行程（仅修改提供的字段）。"""

    user_id: str = Field(pattern=_ID_PATTERN)
    origin: str | None = Field(default=None, max_length=100)
    destination: str | None = Field(default=None, min_length=1, max_length=100)
    start_date: str | None = Field(default=None, pattern=_DATE_PATTERN)
    end_date: str | None = Field(default=None, pattern=_DATE_PATTERN)
    purpose: str | None = Field(default=None, max_length=100)

    @field_validator("destination")
    @classmethod
    def _destination_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("目的地不能为空")
        problem = _text_write_problem(cleaned)
        if problem:
            raise ValueError(f"目的地{problem}")
        return cleaned

    @field_validator("origin", "purpose")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        problem = _text_write_problem(cleaned)
        if problem:
            raise ValueError(f"行程字段{problem}")
        return cleaned


class TripDedupePayload(BaseModel):
    """清理重复行程（破坏性操作必须走 JSON body）。

    跨站表单只能发送 form / text-plain 等简单请求，发不出 application/json；
    把 user_id 放进 body 让浏览器对跨站调用先做 CORS 预检。此前用 query
    参数 + 无 body，任意网页都能静默触发一次全量去重删除。
    """

    user_id: str = Field(pattern=_ID_PATTERN)
    dry_run: bool = False


class SessionTitlePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(max_length=60)

    @field_validator("title")
    @classmethod
    def _title_text_is_writable(cls, value: str) -> str:
        problem = _text_write_problem(value)
        if problem:
            raise ValueError(f"标题{problem}")
        return value


class SessionPinPayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    pinned: bool


class SessionArchivePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    archived: bool


class UserImportPayload(BaseModel):
    """恢复用户备份：``/api/export`` 导出的 JSON 对象原样放在 backup 字段。"""

    backup: dict[str, Any]


class SessionCapacityError(RuntimeError):
    """会话槽位全部在用、无法为新会话腾位（每个会话常驻一条数据库连接）。"""


class _WebSession:
    """一个浏览器会话（对应一个 ConversationRuntime）。"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        model: Any,
        breaker: CircuitBreaker,
        persist_guard: Callable[[], bool] | None = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.runtime = ConversationRuntime(
            user_id=user_id,
            session_id=session_id,
            model=model,
            circuit_breaker=breaker,
            persist_guard=persist_guard,
        )
        # 正在处理对话的请求数：LRU 淘汰必须跳过在用会话，否则会在
        # runtime 执行中途 close（PostgreSQL 连接被抽走）
        self._turn_lock = threading.Lock()
        self._active_turns = 0
        self._turn_started_at = 0.0

    def begin_turn(self) -> None:
        with self._turn_lock:
            if self._active_turns == 0:
                self._turn_started_at = time.monotonic()
            self._active_turns += 1

    def end_turn(self) -> None:
        with self._turn_lock:
            self._active_turns = max(0, self._active_turns - 1)
            if self._active_turns == 0:
                self._turn_started_at = 0.0

    def in_use(self) -> bool:
        with self._turn_lock:
            return self._active_turns > 0

    def long_in_use(self, limit_sec: float) -> bool:
        """占用时间是否已超过上限（防止标记泄漏把会话永久钉在缓存里）。"""
        with self._turn_lock:
            return (
                self._active_turns > 0
                and time.monotonic() - self._turn_started_at > limit_sec
            )

    def close(self) -> None:
        """释放会话资源（PostgreSQL 连接等）。"""
        self.runtime.close()


class _SessionTurnLease:
    """SSE 响应生命周期内的用户锁 + 会话占用租约（幂等释放）。

    响应体可能在首次迭代前就被取消（客户端在响应开始前断开），此时异步
    生成器的 ``finally`` 不会执行；由 :class:`_LeasedStreamingResponse` 在
    ``__call__`` 的 finally 中兜底释放，避免用户锁与会话占用永久泄漏。
    """

    def __init__(self, lock: asyncio.Lock, session: _WebSession) -> None:
        self._lock = lock
        self._session = session
        self._released = False
        self._turn_task: asyncio.Task[Any] | None = None

    def bind_turn(self, task: asyncio.Task[Any]) -> None:
        """登记本回合的协程任务：释放租约前先由 :meth:`stop_turn` 收尾。"""
        self._turn_task = task

    async def stop_turn(self) -> None:
        """取消在途回合并等待落库线程结束（幂等）。

        取消只中断协程，``asyncio.to_thread`` 里的记忆写线程会继续执行；
        释放用户锁之前旧回合必须停稳，否则随后的删除 / 新一轮对话会与
        迟到的写入并发（已删除的会话被写回复活）。

        注意：生成器可能处在 Starlette 的 anyio 取消作用域内，作用域取消
        后其中任何 await 都会立即再次抛 CancelledError——真正的等待由
        作用域外的 :class:`_LeasedStreamingResponse` 兜底执行，这里在
        作用域内被取消时直接向上抛，仍由兜底路径完成。
        """
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self._session.runtime.drain_pending_persist()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 落库失败已由回合自身映射
            logger.debug("等待记忆落库失败", exc_info=True)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._session.end_turn()
        self._lock.release()


class _LeasedStreamingResponse(StreamingResponse):
    """响应结束（含未开始迭代即被取消）时停稳回合并释放租约。"""

    def __init__(self, content: Any, lease: _SessionTurnLease, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._lease = lease

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await self._lease.stop_turn()
            self._lease.release()


_UNMARK_TOMBSTONE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _DaemonImportExecutor(ThreadPoolExecutor):
    """导入专用执行器：工作线程为守护线程。

    ThreadPoolExecutor 默认创建非守护线程：lifespan 里 wait=False 之后，
    进程退出仍要 join 正在跑的分钟级导入，服务停不下来。这里沿用 CPython
    的内部线程创建逻辑（3.10-3.12 稳定），只把线程标记为 daemon。
    """

    def _adjust_thread_count(self) -> None:  # type: ignore[override]
        if self._idle_semaphore.acquire(timeout=0):  # type: ignore[attr-defined]
            return

        def weakref_cb(_: Any, q: Any = self._work_queue) -> None:  # type: ignore[attr-defined]
            q.put(None)

        num_threads = len(self._threads)  # type: ignore[attr-defined]
        if num_threads < self._max_workers:  # type: ignore[attr-defined]
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            thread = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,  # type: ignore[attr-defined]
                    self._initializer,  # type: ignore[attr-defined]
                    self._initargs,  # type: ignore[attr-defined]
                ),
                daemon=True,
            )
            thread.start()
            self._threads.add(thread)  # type: ignore[attr-defined]
            _threads_queues[thread] = self._work_queue  # type: ignore[index, attr-defined]


class AligoWebApp:
    """应用状态：共享模型 / 熔断器 / 会话表。"""

    # 进程内缓存的会话上限（超出后按最久未使用淘汰，避免长时间运行内存与连接增长）。
    # 每个会话在 PostgreSQL 后端下持有 1 个连接，保守取 50，避免撞上数据库连接上限。
    max_sessions = 50
    # 占用标记超过该时长仍未解除（例如 SSE 在响应开始前客户端断开，生成器
    # 从未启动导致 finally 不执行）视为标记泄漏，允许 LRU 回收
    session_turn_leak_sec = 3600.0
    # 用户锁表同样有上限：用户 ID 由请求方提供，未命中时淘汰未持有的锁
    max_locks = 1000
    # 已删除会话的短时墓碑：在途 / 重连的对话不能把刚删除的会话写回（复活）。
    # TTL 必须覆盖「删除发生时仍在途的回合」的最长生命周期：回合在删除前
    # 开始、落库在删除后完成时，persist_guard 靠墓碑否决写入；多智能体 +
    # 重试的回合可能超过 5 分钟，旧值 300s 过期后迟到落库会把已删除会话
    # 写回（多实例下删除发生在别的实例，用户锁不覆盖）。与
    # session_turn_leak_sec 同值：任何在途回合都不会比这个窗更长。
    max_tombstones = 1000
    tombstone_ttl_sec = 3600.0
    # 共享墓碑故障后的重试冷却：期间只用进程内墓碑，避免每个请求都等 Redis 超时
    tombstone_retry_sec = 60.0

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
        self._deleted_sessions: OrderedDict[str, tuple[float, str]] = OrderedDict()
        # mark/unmark/is_recently_deleted 会被 asyncio.to_thread 在线程池里执行，
        # 不同 user_id 的请求互不串行：OrderedDict 的迭代 + 删除必须加锁
        self._tombstone_lock = threading.Lock()
        # 会话缓存同样会被线程池里的 drop_session 与事件循环里的 get_session
        # 并发修改（插入 / move_to_end / popitem），需要独立互斥
        self._sessions_lock = threading.Lock()
        # 构造 _WebSession 会新建 Redis / PostgreSQL 连接：并发构造数量限制
        # 在会话上限以内，否则一批并发新会话会同时打爆数据库连接数
        self._session_build_slots = threading.BoundedSemaphore(
            max(1, int(self.max_sessions))
        )
        # 备份恢复专用执行器：单 worker 串行化导入，且不占用 asyncio 默认
        # 线程池——分钟级的大导入会让所有对话 / SSE / 删除的 to_thread 排队
        self.import_executor = _DaemonImportExecutor(
            max_workers=1, thread_name_prefix="aligo-import"
        )
        # 导入排队上限（1 执行 + 1 等待）：执行器队列无界，并发大备份
        # （各可达 64MB）会在排队期间常驻内存直到 OOM，排队已满时按 503 背压
        self.import_queue_slots = threading.BoundedSemaphore(2)
        # 知识库批量导入同样可能一次编码上万条分片并长时间持有写锁：放专用
        # 单 worker 执行器，避免 40 个并发导入占满 Starlette 共享线程池后把
        # /api/status、/api/sessions 等同步端点全部排队拖死
        self.kb_import_executor = _DaemonImportExecutor(
            max_workers=1, thread_name_prefix="aligo-kb-import"
        )
        # 知识库导入排队上限（1 执行 + 1 等待）：执行器队列无界，并发导入
        # （各 ≤2MB、最多 2 万条分片）排在单 worker 后面会一直持有载荷
        self.kb_import_queue_slots = threading.BoundedSemaphore(2)
        # 锁表占满且全部在用时的兜底锁：让无法入表的新用户串行处理，
        # 保证锁表内存有界（不能为了收录新用户而丢弃正在持有的锁）
        self.overflow_lock = asyncio.Lock()
        # 配置 REDIS_URL 且可用时，两个桶都使用 Redis 共享计数，多实例共享额度；
        # 未配置或 Redis 不可用时回退进程内滑动窗口（单个实例内仍然有效）
        redis_client = get_shared_redis_client()
        # 配置了 REDIS_URL 但启动时不可达：保留客户端工厂，Redis 恢复后
        # 限流器自动重新接入共享计数，不会整个生命周期固化为进程内限流
        redis_factory = (
            get_shared_redis_client if STORAGE_CONFIG.get("redis_url") else None
        )
        self.rate_limiter = create_rate_limiter(
            "user",
            RATE_LIMIT_CONFIG.get("chat_requests", 30),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 用户 ID 由请求方提供、可以轮换；再按纯客户端 IP 限流，防止换 ID 绕过
        self.ip_limiter = create_rate_limiter(
            "ip",
            RATE_LIMIT_CONFIG.get("ip_requests", 120),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 知识库写入 / 导入 / 语义检索会触发嵌入模型推理，单独按客户端 IP 限流，
        # 不占用对话额度（Web 批量导入每批 200 条，60 次/分钟足够）
        self.kb_limiter = create_rate_limiter(
            "kb",
            RATE_LIMIT_CONFIG.get("kb_requests", 60),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 备份导入的面板循环按 200 条/批调用导入接口：沿用知识库 60 次/分钟
        # 会让数万条的备份在窗口内必然 429，且重试时前几十批都是快速跳过的
        # 已存在内容、永远卡在同一位置——导入单独用更大的桶（6 批/秒）
        raw_import_limit = RATE_LIMIT_CONFIG.get("kb_import_requests", 1200)
        import_limit = (
            int(raw_import_limit)
            if isinstance(raw_import_limit, (int, float))
            else 1200
        )
        self.import_limiter = create_rate_limiter(
            "kb_import",
            import_limit,
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 天气接口每次未命中会上游发两次请求；按客户端 IP 限流，
        # 保护 Open-Meteo 免费额度与共用线程池，不占用对话 / 知识库额度
        self.weather_limiter = create_rate_limiter(
            "weather",
            RATE_LIMIT_CONFIG.get("weather_requests", 30),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 面板数据读接口（会话列表 / 状态 / 导出 / 台账）每次请求都全量读取
        # 用户历史，且走共享线程池；不设额度时几十个并发读就能把线程池占满
        # （额度远高于正常面板刷新频率）
        self.read_limiter = create_rate_limiter(
            "read",
            RATE_LIMIT_CONFIG.get("read_requests", 600),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
            client_factory=redis_factory,
        )
        # 同一时间只允许一个检索评测：配置 Redis 时跨实例共享，故障回退进程内；
        # 工厂让启动时 Redis 不可用的实例在恢复后重新参与共享锁
        self.eval_lock = DistributedLock(
            redis_client,
            "aligo:lock:kb_eval",
            ttl_sec=300,
            client_factory=get_shared_redis_client,
        )
        self._redis_client = redis_client
        self._tombstones_shared = redis_client is not None
        self._tombstone_retry_at = 0.0

    @staticmethod
    def _session_key(user_id: str, session_id: str) -> str:
        return f"{user_id}\n{session_id}"

    def _evict_over_limit_locked(
        self, keep_key: str, evicted: list["_WebSession"]
    ) -> None:
        """缓存超限时淘汰空闲旧会话（调用方必须持有 _sessions_lock）。

        三条取会话路径（缓存命中 / 并发竞态命中 / 新建插入）都要调用：
        只在新建分支淘汰时，并发首请求或命中路径会把缓存推过上限后永不回收。
        """
        while len(self.sessions) > self.max_sessions:
            victim = next(
                (
                    candidate_key
                    for candidate_key, item in self.sessions.items()
                    if candidate_key != keep_key
                    and (
                        not item.in_use()
                        or item.long_in_use(self.session_turn_leak_sec)
                    )
                ),
                None,
            )
            if victim is None:
                break
            evicted.append(self.sessions.pop(victim))

    def get_session(
        self, user_id: str, session_id: str | None, mark_in_use: bool = False
    ) -> _WebSession:
        """获取（或新建）会话；``mark_in_use`` 时先登记占用再返回。

        ``_WebSession`` 构造包含 Redis ping / PostgreSQL 连接等阻塞网络 IO，
        不能持有 ``_sessions_lock``：一次慢构造会把所有用户（包括已有会话）
        的会话查找一起串行阻塞。因此在锁外构造、锁内二次检查插入；并发
        重复构造时后到者关闭自己新建的运行时回收连接。

        ``begin_turn`` 仍在缓存锁内完成：LRU 淘汰不会在「刚取到会话、还没
        开始对话」的间隙把它 close 掉。调用方必须在结束（含异常）后调用
        ``end_turn``。
        """
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
        # 缓存键包含用户：同一 session_id 被不同用户使用时各有独立运行时，
        # 互不覆盖，也不会因为别人关闭同名会话而丢掉自己在进程内的短期记忆
        key = self._session_key(user_id, session_id)
        evicted: list[_WebSession] = []
        session: _WebSession | None = None
        with self._sessions_lock:
            session = self.sessions.get(key)
            if session is not None:
                self.sessions.move_to_end(key)
                if mark_in_use:
                    session.begin_turn()
                self._evict_over_limit_locked(key, evicted)
        if session is None:
            # 构造前预检：_WebSession 构造会打开一条 PostgreSQL 连接并跑建表
            # DDL，全部槽位都在用时不该为注定 503 的请求白付这部分代价。
            # 预检与插入之间仍有竞态，构造后的复检（下方 reject_capacity）
            # 负责把并发下的上界收回到 max_sessions
            with self._sessions_lock:
                can_admit = len(self.sessions) < self.max_sessions or any(
                    (not item.in_use())
                    or item.long_in_use(self.session_turn_leak_sec)
                    for item in self.sessions.values()
                )
            if not can_admit:
                raise SessionCapacityError(
                    f"并发会话过多（上限 {self.max_sessions}），请稍后再试"
                )
            # 每个会话独立的用量跟踪计数，避免并发会话之间的 token 增量互相污染
            self._session_build_slots.acquire()
            reject_capacity = False
            try:
                candidate = _WebSession(
                    user_id,
                    session_id,
                    wrap_usage_tracking(self.model),
                    self.breaker,
                    # 在途落库与删除并发：存储层写入前据此判断会话是否仍可写
                    # （墓碑有效 = 已删除 → 返回 False，整轮不写回）
                    persist_guard=lambda: not self.is_recently_deleted(
                        user_id, session_id
                    ),
                )
                with self._sessions_lock:
                    found = self.sessions.get(key)
                    if found is not None:
                        # 并发首请求：另一线程已抢先插入，使用已有实例
                        session = found
                        self.sessions.move_to_end(key)
                        # 命中路径同样要执行容量回收：只在新建分支淘汰时，
                        # sessions 会先被并发首请求推过上限再永远超限
                        self._evict_over_limit_locked(key, evicted)
                        if mark_in_use:
                            session.begin_turn()
                    else:
                        # 容量守卫：缓存上限只淘汰空闲会话；全部会话都在用的
                        # 突发（同一 IP 突发 120 个并发对话即可）此前会无限
                        # 新建会话——每个 _WebSession 常驻一条 PostgreSQL
                        # 连接，超过数据库 max_connections 后新建失败会触发
                        # 进程级 JSON 回退（读写分叉）。没有可淘汰的空闲会话
                        # 时拒绝本次新建（可重试的 503），连接数量因此有界。
                        evictable = any(
                            (not item.in_use())
                            or item.long_in_use(self.session_turn_leak_sec)
                            for item in self.sessions.values()
                        )
                        if len(self.sessions) >= self.max_sessions and not evictable:
                            reject_capacity = True
                        else:
                            session = candidate
                            self.sessions[key] = candidate
                            # 只淘汰未在用、且不是本次要返回的会话：正在对话的会话被
                            # close 会让 runtime 中途失去存储连接；本次刚取到的会话被
                            # 淘汰则会把 close 掉的实例交给调用方。全部在用则暂时超限，
                            # 等后续请求再回收
                            self._evict_over_limit_locked(key, evicted)
                            if mark_in_use:
                                session.begin_turn()
                if reject_capacity:
                    session = None
                if session is not candidate:
                    # 竞争失败：自己新建的运行时从未暴露给调用方，关闭回收
                    # PostgreSQL 连接等资源
                    candidate.close()
            finally:
                self._session_build_slots.release()
            if reject_capacity:
                raise SessionCapacityError(
                    f"并发会话过多（上限 {self.max_sessions}），请稍后再试"
                )
        # 关闭被淘汰的会话（可能做收尾 IO）放在锁外，避免拖慢其它请求
        for stale in evicted:
            stale.close()
        assert session is not None  # 容量拒绝已在上面抛出
        return session

    def drop_session(self, user_id: str, session_id: str) -> None:
        """移除并关闭某用户的一个会话（其它用户的同名会话不受影响）。"""
        with self._sessions_lock:
            session = self.sessions.pop(
                self._session_key(user_id, session_id), None
            )
        if session is not None:
            session.close()

    @staticmethod
    def _deleted_key(user_id: str, session_id: str) -> str:
        return f"{_DELETED_KEY_PREFIX}:{user_id}:{session_id}"

    def _shared_tombstones(self) -> Any | None:
        """共享墓碑可用时返回 Redis 客户端；故障冷却期内只用进程内墓碑。

        启动时 Redis 不可用（``_redis_client`` 为 None）也允许通过
        ``get_shared_redis_client``（自带 60 秒冷却）在恢复后重新接入。
        """
        if self._redis_client is None:
            self._redis_client = get_shared_redis_client()
            if self._redis_client is None:
                return None
        if not self._tombstones_shared and time.monotonic() < self._tombstone_retry_at:
            return None
        return self._redis_client

    def _tombstone_failed(self, exc: Exception, action: str) -> None:
        self._tombstones_shared = False
        self._tombstone_retry_at = time.monotonic() + self.tombstone_retry_sec
        logger.warning(
            "会话删除墓碑%s失败（%s），本实例回退进程内墓碑，%.0f 秒后重试共享墓碑",
            action,
            exc,
            self.tombstone_retry_sec,
        )

    def mark_deleted(self, user_id: str, session_id: str) -> str:
        """记录短时墓碑，阻止在途对话把刚删除的会话写回，返回本请求的 token。

        配置 Redis 时同时写入共享墓碑（TTL 与本地一致；``SET NX`` 不覆盖
        其它实例已有的墓碑），其它实例的新请求也能看到该会话已删除。
        删除失败回滚时用 token 只撤销本请求写入的墓碑。
        """
        token = uuid.uuid4().hex
        key = self._session_key(user_id, session_id)
        now = time.monotonic()
        with self._tombstone_lock:
            self._deleted_sessions[key] = (now, token)
            self._deleted_sessions.move_to_end(key)
            # 先清掉已过期的墓碑，避免仍有效的墓碑被容量上限淘汰
            expired = [
                candidate
                for candidate, at in self._deleted_sessions.items()
                if now - at[0] > self.tombstone_ttl_sec
            ]
            for candidate in expired:
                del self._deleted_sessions[candidate]
            while len(self._deleted_sessions) > self.max_tombstones:
                self._deleted_sessions.popitem(last=False)
        client = self._shared_tombstones()
        if client is not None:
            try:
                # NX：不覆盖其它实例已有的墓碑（否则回滚时会误删对方的保护）
                client.set(
                    self._deleted_key(user_id, session_id),
                    token,
                    nx=True,
                    ex=int(self.tombstone_ttl_sec),
                )
            except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
                self._tombstone_failed(exc, "写入")
            else:
                self._tombstones_shared = True
                self._tombstone_retry_at = 0.0
        return token

    def unmark_deleted(
        self, user_id: str, session_id: str, token: str | None = None
    ) -> None:
        """撤销墓碑（删除失败时使用）。

        传入 token 时只撤销本请求写入的那条（本地条目 token 匹配、共享
        墓碑值等于 token 才删除）；不传时保持旧的整条撤销语义。
        """
        key = self._session_key(user_id, session_id)
        with self._tombstone_lock:
            if token is None:
                self._deleted_sessions.pop(key, None)
            else:
                current = self._deleted_sessions.get(key)
                if current is not None and current[1] == token:
                    self._deleted_sessions.pop(key, None)
        client = self._shared_tombstones()
        if client is None:
            return
        try:
            if token is None:
                client.delete(self._deleted_key(user_id, session_id))
            else:
                client.eval(
                    _UNMARK_TOMBSTONE_LUA,
                    1,
                    self._deleted_key(user_id, session_id),
                    token,
                )
        except Exception as exc:  # noqa: BLE001 - 留着墓碑由 TTL 过期更安全
            self._tombstone_failed(exc, "撤销")

    def shared_tombstones_active(self) -> bool:
        """共享墓碑是否在用（配置了 Redis 且最近一次写入成功）。"""
        return self._tombstones_shared

    def is_recently_deleted(self, user_id: str, session_id: str | None) -> bool:
        if not session_id:
            return False
        key = self._session_key(user_id, session_id)
        remaining: float | None = None
        token = ""
        with self._tombstone_lock:
            entry = self._deleted_sessions.get(key)
            if entry is not None:
                created, token = entry
                elapsed = time.monotonic() - created
                if elapsed <= self.tombstone_ttl_sec:
                    remaining = self.tombstone_ttl_sec - elapsed
                else:
                    self._deleted_sessions.pop(key, None)
        client = self._shared_tombstones()
        if client is not None:
            try:
                if remaining is not None:
                    # 本地墓碑仍有效：顺带补写共享墓碑（首次写入失败后的
                    # 自愈）。TTL 用剩余时长，不延长删除窗口；NX 不覆盖
                    # 其它实例已有的墓碑
                    client.set(
                        self._deleted_key(user_id, session_id),
                        token,
                        nx=True,
                        ex=max(1, int(remaining)),
                    )
                    self._tombstones_shared = True
                    self._tombstone_retry_at = 0.0
                    return True
                exists = bool(
                    client.exists(self._deleted_key(user_id, session_id))
                )
            except Exception as exc:  # noqa: BLE001 - 读失败时回退进程内墓碑
                self._tombstone_failed(exc, "读取")
            else:
                self._tombstones_shared = True
                self._tombstone_retry_at = 0.0
                return exists
        return remaining is not None

    def close_all(self) -> None:
        """关闭所有会话（进程退出时调用）。"""
        with self._sessions_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()

    def lock_for(self, user_id: str) -> asyncio.Lock:
        """同一用户的请求串行处理，避免长期记忆并发写覆盖。"""
        lock = self.locks.get(user_id)
        overflow_busy = self.overflow_lock.locked() or bool(
            getattr(self.overflow_lock, "_waiters", None)
        )
        if lock is None and overflow_busy:
            # 兜底锁仍被占用（锁表曾满）：新请求继续共用兜底锁。否则锁表
            # 腾出空位后，同一用户的上一个请求还持着兜底锁，这里会新建
            # 私有锁与之并行，按用户串行失效
            return self.overflow_lock
        if lock is None:
            if len(self.locks) >= self.max_locks:
                for key, candidate in list(self.locks.items()):
                    # 除「已持有」外还要排除「有等待者」：持有者刚 release、
                    # 等待者尚未恢复执行时 locked() 为 False，此时淘汰会让
                    # 等待者拿到已脱离锁表的锁，与新请求的锁并行
                    if not candidate.locked() and not getattr(
                        candidate, "_waiters", None
                    ):
                        del self.locks[key]
                        break
            if len(self.locks) >= self.max_locks:
                # 所有锁都在使用中：共享兜底锁（并发高峰下新用户串行，但内存有界）
                return self.overflow_lock
            lock = asyncio.Lock()
            self.locks[user_id] = lock
        return lock


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _client_host(request: Request) -> str:
    """限流使用的客户端地址。

    默认取 TCP 对端地址。部署在反向代理后面时设置 ``TRUST_PROXY_HEADERS=1``，
    改为取 ``X-Forwarded-For`` 的第一个地址（代理需覆盖写入该头）；默认关闭，
    避免客户端自行伪造该头绕过 IP 桶。
    """
    if RATE_LIMIT_CONFIG.get("trust_proxy_headers"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:64]
    return request.client.host if request.client else "unknown"


def _rate_identity(host: str) -> str:
    """限流桶的客户端标识：IPv6 按 /64 聚合。

    同一 /64 子网内可以随意轮换源地址；按完整地址做 key 时每次轮换都是
    新桶，IP 维度限流被直接绕过。
    """
    text = str(host or "").strip()
    try:
        import ipaddress

        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if address.version == 6:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            # IPv4 映射地址（::ffff:1.2.3.4）：按真实 IPv4 聚合，否则所有
            # IPv4 客户端都会落进 ::/64 同一个桶
            return str(mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return text


def _filter_trips_by_status(
    trips: list[dict[str, Any]], status: str | None
) -> list[dict[str, Any]]:
    """按面板状态徽标过滤行程（列表与导出同一口径）。"""
    if status is None:
        return trips
    label = {
        "active": "进行中",
        "upcoming": "即将出行",
        "past": "已结束",
    }[status]
    return [trip for trip in trips if _trip_state_label(trip) == label]


def _limiter_degraded(limiter: Any) -> bool:
    """限流桶当前是否降级（降级且冷却已过时主动探测一次 Redis）。

    桶的 ``degraded`` 标记只会在同一桶的成功 Redis 调用后清除；低频桶
    （weather / import / kb_import）在 Redis 恢复后可能长期没有请求，
    健康检查不能因此永远报降级（README 承诺恢复共享后自动变回 false）。
    """
    probe = getattr(limiter, "probe", None)
    if callable(probe):
        return not probe()
    return bool(getattr(limiter, "degraded", False))


def _rate_limit_info(state: AligoWebApp) -> dict[str, Any]:
    """限流后端与状态（健康检查与指标接口共用）。

    配置了 ``REDIS_URL`` 但限流器仍是进程内实现（启动时 Redis 不可用），
    同样视为降级；否则运维无法区分「未配置」与「配置了但回退了」。
    """
    backend = getattr(state.rate_limiter, "backend", "memory")
    configured = bool(STORAGE_CONFIG.get("redis_url"))
    # 各桶独立回退：任一桶处于降级都上报，不能只看用户桶
    limiters = (
        state.rate_limiter,
        state.ip_limiter,
        state.kb_limiter,
        state.weather_limiter,
        state.import_limiter,
        state.read_limiter,
    )
    degraded = any(_limiter_degraded(limiter) for limiter in limiters) or (
        backend == "memory" and configured
    )
    return {
        "backend": backend,
        "configured": configured,
        "degraded": degraded,
        "user_limit": state.rate_limiter.limit,
        "ip_limit": state.ip_limiter.limit,
        "kb_limit": state.kb_limiter.limit,
        "kb_import_limit": state.import_limiter.limit,
        "weather_limit": state.weather_limiter.limit,
        "read_limit": state.read_limiter.limit,
        "window_sec": state.rate_limiter.window_sec,
    }


def _enforce_rate_limit(
    state: AligoWebApp, user_id: str, request: Request
) -> dict[str, str]:
    """对对话接口做滑动窗口限流（按 客户端 + 用户，以及客户端 IP 两个维度）。

    Returns:
        供响应附带的额度头（X-RateLimit-Limit / Remaining / Reset）。
    """
    client = _rate_identity(_client_host(request))
    user_key = f"{client}:{user_id}"

    def _over_limit(limit: int, retry_after: float) -> HTTPException:
        """429 响应：Retry-After 取整数秒（向上取整、至少 1 秒）。"""
        wait_seconds = max(1, math.ceil(retry_after))
        return HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请约 {wait_seconds} 秒后重试（请求 ID：{_request_id(request)}）",
            headers={
                "Retry-After": str(wait_seconds),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(wait_seconds),
            },
        )

    # 先用 peek 检查两个桶，都有额度才记录：否则被 429 的请求会消耗
    # 另一个桶的配额，一个客户端的重试循环会把同 IP 的其它客户端一起锁死
    retry_after = state.rate_limiter.peek(user_key)
    ip_retry_after = state.ip_limiter.peek(client)
    if retry_after is not None or ip_retry_after is not None:
        # 429 的额度头描述真正触发限制的桶（等待更久的那个）
        if (retry_after or 0.0) >= (ip_retry_after or 0.0):
            limit = state.rate_limiter.limit
        else:
            limit = state.ip_limiter.limit
        raise _over_limit(limit, max(retry_after or 0.0, ip_retry_after or 0.0))
    # peek 与 check 之间额度可能被并发请求抢走：check 返回非 None 时请求已
    # 超额，必须按它的结果拒绝，否则会放行超出额度的请求。先记 IP 桶再记
    # 用户桶：IP 桶拒绝时不消耗用户额度（并发下也不会）；反过来用户桶迟到
    # 拒绝只会浪费共享 IP 桶的一个名额，影响面更小
    ip_retry = state.ip_limiter.check(client)
    if ip_retry is not None:
        raise _over_limit(state.ip_limiter.limit, ip_retry)
    user_retry = state.rate_limiter.check(user_key)
    if user_retry is not None:
        raise _over_limit(state.rate_limiter.limit, user_retry)
    remaining_user, reset_user = state.rate_limiter.status(user_key)
    remaining_ip, reset_ip = state.ip_limiter.status(client)
    # 额度头取自当前更紧的桶（剩余更少；剩余相同时取重置更晚的），
    # 避免 Limit 来自用户桶而 Remaining 来自 IP 桶导致三个值自相矛盾
    if (remaining_ip, -reset_ip) < (remaining_user, -reset_user):
        limit, remaining, reset = state.ip_limiter.limit, remaining_ip, reset_ip
    else:
        limit, remaining, reset = state.rate_limiter.limit, remaining_user, reset_user
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(1, math.ceil(reset))),
    }


def _enforce_import_rate_limit(state: AligoWebApp, request: Request) -> dict[str, str]:
    """备份导入接口的独立限流（比知识库维护接口更宽松，按客户端 IP）。"""
    client = _rate_identity(_client_host(request))
    retry_after = state.import_limiter.check(client)
    if retry_after is not None:
        wait_seconds = max(1, math.ceil(retry_after))
        raise HTTPException(
            status_code=429,
            detail=f"导入过于频繁，请约 {wait_seconds} 秒后重试"
            f"（请求 ID：{_request_id(request)}）",
            headers={
                "Retry-After": str(wait_seconds),
                "X-RateLimit-Limit": str(state.import_limiter.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(wait_seconds),
            },
        )
    remaining, reset = state.import_limiter.status(client)
    return {
        "X-RateLimit-Limit": str(state.import_limiter.limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(1, math.ceil(reset))),
    }


def _enforce_kb_rate_limit(state: AligoWebApp, request: Request) -> dict[str, str]:
    """知识库高开销接口的独立限流（按客户端 IP，与对话额度互不影响）。

    Returns:
        供响应附带的额度头（X-RateLimit-Limit / Remaining / Reset）。
    """
    client = _rate_identity(_client_host(request))
    # 单桶直接 check：内存实现内部加锁、Redis 实现 Lua 原子，避免 peek/check
    # 之间被并发请求插队导致超发；check 超限时本身不会记录事件
    retry_after = state.kb_limiter.check(client)
    if retry_after is not None:
        wait_seconds = max(1, math.ceil(retry_after))
        raise HTTPException(
            status_code=429,
            detail=f"知识库操作过于频繁，请约 {wait_seconds} 秒后重试"
            f"（请求 ID：{_request_id(request)}）",
            headers={
                "Retry-After": str(wait_seconds),
                "X-RateLimit-Limit": str(state.kb_limiter.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(wait_seconds),
            },
        )
    remaining, reset = state.kb_limiter.status(client)
    return {
        "X-RateLimit-Limit": str(state.kb_limiter.limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(1, math.ceil(reset))),
    }


def _enforce_weather_rate_limit(
    state: AligoWebApp, request: Request
) -> dict[str, str]:
    """天气接口的独立限流（按客户端 IP，与对话 / 知识库额度互不影响）。

    Returns:
        供响应附带的额度头（X-RateLimit-Limit / Remaining / Reset）。
    """
    client = _rate_identity(_client_host(request))
    retry_after = state.weather_limiter.check(client)
    if retry_after is not None:
        wait_seconds = max(1, math.ceil(retry_after))
        raise HTTPException(
            status_code=429,
            detail=f"天气查询过于频繁，请约 {wait_seconds} 秒后重试"
            f"（请求 ID：{_request_id(request)}）",
            headers={
                "Retry-After": str(wait_seconds),
                "X-RateLimit-Limit": str(state.weather_limiter.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(wait_seconds),
            },
        )
    remaining, reset = state.weather_limiter.status(client)
    return {
        "X-RateLimit-Limit": str(state.weather_limiter.limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(1, math.ceil(reset))),
    }


def _enforce_read_rate_limit(
    state: AligoWebApp, request: Request
) -> dict[str, str]:
    """面板数据读接口的独立限流（按客户端 IP）。

    会话列表 / 状态 / 导出 / 台账每次请求都全量读取该用户历史；额度必须
    远高于正常面板刷新频率，只用于挡住并发拉满线程池的脚本行为。
    """
    client = _rate_identity(_client_host(request))
    retry_after = state.read_limiter.check(client)
    if retry_after is not None:
        wait_seconds = max(1, math.ceil(retry_after))
        raise HTTPException(
            status_code=429,
            detail=f"数据读取过于频繁，请约 {wait_seconds} 秒后重试"
            f"（请求 ID：{_request_id(request)}）",
            headers={
                "Retry-After": str(wait_seconds),
                "X-RateLimit-Limit": str(state.read_limiter.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(wait_seconds),
            },
        )
    remaining, reset = state.read_limiter.status(client)
    return {
        "X-RateLimit-Limit": str(state.read_limiter.limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(max(1, math.ceil(reset))),
    }


_WEATHER_EXECUTOR_MAX_WORKERS = 8
_weather_executor: ThreadPoolExecutor | None = None
_weather_executor_lock = threading.Lock()


def _get_weather_executor() -> ThreadPoolExecutor:
    """天气查询专用线程池。

    上游（Open-Meteo）变慢时，天气请求只占满自己的线程，不会把 FastAPI
    执行同步端点的共享线程池一起拖住；超出的请求在事件循环里排队。
    """
    global _weather_executor
    with _weather_executor_lock:
        if _weather_executor is None:
            _weather_executor = ThreadPoolExecutor(
                max_workers=_WEATHER_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="aligo-weather",
            )
        return _weather_executor


# ---------- 长期记忆读取 ----------


def _open_long_term(user_id: str, strict: bool = False) -> Any:
    """按当前配置打开长期记忆（JSON 或 PostgreSQL）。

    ``strict=True`` 用于删除等破坏性操作：配置了 PostgreSQL 但当前不可用
    （连接失败 / 失败冷却期）时工厂抛 ``MemoryPersistenceError``（不回退
    JSON），这里映射为可重试的 503——在回退库上「删除成功」会留下
    PostgreSQL 里的数据，恢复后会话 / 消息重新出现。

    非 strict 时 PostgreSQL 连接失败按既有降级策略回退本地 JSON，不会走到
    这里的异常分支；这里兜住的是其余打开失败（文件损坏 / 依赖缺失等），
    统一映射为可重试的 503，避免所有调用点都抛裸 500。调用点仍负责关闭
    返回的 store。
    """
    try:
        return create_long_term_memory(user_id, str(MEMORY_DIR), strict=strict)
    except HTTPException:
        raise
    except MemoryPersistenceError as exc:
        logger.warning("打开长期记忆失败（%s，strict）：%s", user_id, exc)
        raise HTTPException(
            status_code=503, detail="存储暂时不可用，请稍后再试"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 存储故障统一可重试的 503
        logger.warning("打开长期记忆失败（%s）：%s", user_id, exc)
        raise HTTPException(status_code=503, detail=f"存储暂不可用：{exc}") from exc


def _close_long_term(store: Any) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def _safe_filename(value: str, fallback: str = "aligo") -> str:
    """把用户 / 会话 ID 转为只含 ASCII 的安全下载文件名。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return cleaned or fallback


def _json_safe_value(value: Any) -> Any:
    """把历史遗留的 NaN / Infinity 归一为 None，避免 JSON 响应序列化失败。"""
    from utils.json_safe import sanitize_json

    return sanitize_json(value)


def _trim_validation_item(item: dict[str, Any]) -> dict[str, Any]:
    """422 错误条目：input 截断为 200 字符并转义无法编码的字符。

    /api/import 的 64MB 上限下，完整回显 input 会把 422 响应放大到同尺寸
    （内存 / 响应体积双放大）；孤立代理项会让响应自身编码失败（500）。
    全局 RequestValidationError 处理器与 /api/import 的手工解析错误都走它。
    """
    trimmed = dict(item)
    raw = trimmed.get("input")
    if raw is not None:
        # 先按原始值截断再看 repr：对大请求体（64MB 备份）做完整
        # repr 会在事件循环上把整份 input 物化成字符串
        if isinstance(raw, str):
            text = raw
        else:
            text = reprlib.repr(raw)
        text = text if len(text) <= 200 else text[:200] + "…"
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            text = text.encode("utf-8", "backslashreplace").decode("utf-8")
        trimmed["input"] = text
    return trimmed


def _ambiguous_trip_id(trips: list[dict[str, Any]], trip_id: str) -> bool:
    """历史数据里同一 trip_id 可能对应多条不同行程，这类 id 不能按 id 操作。"""
    return sum(1 for trip in trips if str(trip.get("trip_id")) == trip_id) > 1


def _load_user_data(user_id: str) -> dict[str, Any]:
    try:
        store = _open_long_term(user_id)
        try:
            return {
                "preferences": {
                    key: _json_safe_value(value)
                    for key, value in store.get_preference().items()
                },
                "trip_history": store.get_trip_history(limit=None),
                "chat_history": store.get_chat_history(),
                "session_titles": store.get_session_titles(),
                "pinned_sessions": store.get_pinned_sessions(),
                "archived_sessions": store.get_archived_sessions(),
                "statistics": store.get_statistics(),
            }
        finally:
            _close_long_term(store)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 存储故障统一可重试的 503
        logger.warning("读取用户数据失败（%s）：%s", user_id, exc)
        raise HTTPException(status_code=503, detail="读取用户数据失败，请稍后重试") from exc


def _archived_count(data: dict[str, Any]) -> int:
    """实际仍存在于历史中的归档会话数（消息被删光的归档标记不计入）。"""
    session_ids = {
        str(message.get("session_id"))
        for message in data.get("chat_history") or []
        if message.get("session_id")
    }
    archived = data.get("archived_sessions") or []
    return sum(1 for session_id in archived if str(session_id) in session_ids)


def _render_stored_message(message: dict[str, Any]) -> str:
    """把历史消息转换为可展示文本（与 CLI 导出共用同一渲染逻辑）。"""
    return render_stored_message(message)


def _message_search_text(message: dict[str, Any]) -> str:
    """用于全文搜索的文本：助手消息按渲染后的 Markdown 搜索，避免命中的是 JSON 字段名。"""
    if message.get("role") == "assistant":
        return _render_stored_message(message)
    return str(message.get("content", ""))


_MARKDOWN_SNIPPET_PREFIX = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)"
)
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _strip_markdown_links(text: str) -> str:
    """线性剥离 ``[文本](链接)`` / ``![说明](链接)``。

    等价正则 ``\\[([^\\]]+)\\]\\([^)]*\\)`` 对未闭合的 ``[`` 会在每个起点
    回溯到串尾（O(n²)），导入一条构造消息就能让会话搜索卡住事件循环数十
    秒以上。这里改为单遍扫描：``]`` 的位置预排序后二分，``)`` 的失败搜索
    以「该下标之后没有 ``)``」缓存，总代价与文本长度线性相关。
    """
    close_positions = [index for index, char in enumerate(text) if char == "]"]
    if not close_positions:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    no_paren_from = length  # 该下标之后已确认没有 ")"，避免重复失败扫描
    while index < length:
        char = text[index]
        if char == "!":
            if index + 1 < length and text[index + 1] == "[":
                open_index = index + 1
                is_image = True
            else:
                out.append(char)
                index += 1
                continue
        elif char == "[":
            open_index = index
            is_image = False
        else:
            out.append(char)
            index += 1
            continue
        slot = bisect.bisect_right(close_positions, open_index)
        if slot >= len(close_positions):
            out.append(text[index:])
            break
        close = close_positions[slot]
        # 正则要求普通链接的方括号内至少一个字（[]() 不匹配），图片说明可为空
        if (is_image or close > open_index + 1) and (
            close + 1 < length and text[close + 1] == "("
        ):
            if close + 2 >= no_paren_from:
                paren = -1
            else:
                paren = text.find(")", close + 2)
                if paren == -1:
                    no_paren_from = close + 2
            if paren != -1:
                if not is_image:
                    out.append(text[open_index + 1:close])
                index = paren + 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_markdown_for_snippet(text: str) -> str:
    """去掉常见 Markdown 记号（标题 / 列表 / 表格 / 引用 / 强调），供搜索预览使用。"""
    lines: list[str] = []
    for line in str(text).splitlines():
        line = _MARKDOWN_SNIPPET_PREFIX.sub("", line)
        if _MARKDOWN_TABLE_SEPARATOR.fullmatch(line):
            continue
        if line.lstrip().startswith("|"):
            line = line.replace("|", " ")
        lines.append(line)
    cleaned = " ".join(" ".join(lines).split())
    cleaned = _strip_markdown_links(cleaned)
    cleaned = re.sub(r"\*\*|__|~~|`", "", cleaned)
    return cleaned.strip()


def _ci_find(haystack: str, needle: str) -> int:
    """大小写不敏感查找，返回**原文**下标。

    ``lower()`` 可能改变字符串长度（如土耳其语 ``İ`` 小写后是两个码点），
    用 lower 串里的下标去切原文会让窗口整体错位；长度变化时改用正则
    在原文上定位（re.IGNORECASE 返回的就是原文下标）。
    """
    if not needle:
        return -1
    lowered = haystack.lower()
    index = lowered.find(needle.lower())
    if index >= 0 and len(lowered) == len(haystack):
        return index
    match = re.search(re.escape(needle), haystack, re.IGNORECASE)
    return match.start() if match else -1


def _match_snippet(text: str, needle: str, radius: int = 36) -> str:
    """截取命中位置附近的片段（用于会话搜索结果的预览）。"""
    haystack = str(text)
    if not needle:
        return ""
    # 先做廉价的原文包含判断：整条消息做 Markdown 剥离（多条正则）在超长
    # 行上有回溯开销，没命中的消息根本不需要清理
    position = _ci_find(haystack, needle)
    if position < 0:
        return ""
    # 只清理命中位置附近的窗口：单条消息可达数 MB（导入上限 64MB），整条
    # 清理没有意义，超长单行还会放大任何正则的扫描范围
    span = radius * 4 + len(needle)
    window_start = max(0, position - span)
    window_end = min(len(haystack), position + len(needle) + span)
    window = haystack[window_start:window_end]
    cleaned = _strip_markdown_for_snippet(window)
    index = _ci_find(cleaned, needle)
    if index < 0:
        # 关键字本身含被剥离的记号时回退到原文匹配
        cleaned = " ".join(window.split())
        index = _ci_find(cleaned, needle)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(cleaned), index + len(needle) + radius)
    snippet = " ".join(cleaned[start:end].split())
    if window_start > 0 or start > 0:
        snippet = "…" + snippet
    if window_end < len(haystack) or end < len(cleaned):
        snippet += "…"
    return snippet


def _group_sessions(
    history: list[dict[str, Any]],
    limit: int,
    query: str = "",
    titles: dict[str, str] | None = None,
    pinned: list[str] | None = None,
    archived: list[str] | None = None,
    archived_mode: str = "exclude",
) -> list[dict[str, Any]]:
    """按会话聚合消息；提供 query 时只返回命中该关键字的会话，并附带命中片段。

    ``archived_mode``：``exclude`` 只返回未归档会话，``only`` 只返回归档会话。
    """
    # 片段 / 标题匹配都用原文查询：_ci_find 自己处理大小写（lower() 改变
    # 长度时改在原文上用正则定位）。先把查询 lower 会让含 İ 的关键字在
    # 原文里完全匹配不到——仅助手消息命中「İstanbul」的会话被整条过滤，
    # 仅标题命中的会话也拿不到 snippet / match_index
    raw_query = query.strip()
    grouped: dict[str, dict[str, Any]] = {}
    # 每条消息的行内时间戳只解析一次：/api/sessions 每次请求都重新聚合全量
    # 历史，原实现对同一个最新时间戳反复 fromisoformat，几十万条消息时 CPU
    # 直接翻倍
    updated_keys: dict[str, Any] = {}
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
            parsed_key = chronological_key(timestamp)
            current_key = updated_keys.get(str(session_id))
            if current_key is None or parsed_key > current_key:
                # 取会话内最大时间戳：恢复早于现有会话的备份后，消息按导出
                # 顺序（时间升序）追加，直接覆盖会让 updated_at 回拨、排序错位
                updated_keys[str(session_id)] = parsed_key
                entry["updated_at"] = timestamp
        if not entry["title"] and message.get("role") == "user":
            title = " ".join(str(message.get("content", "")).split())
            if title:
                entry["title"] = title[:40]
        if raw_query and "snippet" not in entry:
            snippet = _match_snippet(_message_search_text(message), raw_query)
            if snippet:
                entry["snippet"] = snippet
                # 会话内消息序号（0 起），供前端打开会话时定位到命中消息
                entry["match_index"] = entry["message_count"] - 1

    sessions = sorted(
        grouped.values(),
        key=lambda item: chronological_key(item["updated_at"]),
        reverse=True,
    )
    custom_titles = titles or {}
    for entry in sessions:
        custom = custom_titles.get(entry["session_id"])
        if custom:
            entry["title"] = custom

    pinned_ids = set(pinned or [])
    archived_ids = set(archived or [])
    for entry in sessions:
        entry["pinned"] = entry["session_id"] in pinned_ids
        entry["archived"] = entry["session_id"] in archived_ids
    if archived_mode == "only":
        sessions = [entry for entry in sessions if entry["archived"]]
        for entry in sessions:
            entry["pinned"] = False
    else:
        sessions = [entry for entry in sessions if not entry["archived"]]
        # 稳定排序：置顶会话在前，其余保持最近更新时间倒序
        sessions.sort(key=lambda entry: not entry["pinned"])

    if raw_query:
        sessions = [
            entry
            for entry in sessions
            if entry.get("snippet")
            or _ci_find(str(entry.get("title", "")), raw_query) >= 0
        ]
    for entry in sessions:
        if not entry["title"]:
            entry["title"] = "新对话"
    return sessions[:limit]


def _safe_usage_int(value: Any) -> int:
    """_meta.usage 数字的防御性转换。

    备份恢复可能带入 NaN / Infinity（Python json.loads 接受它们）或历史脏
    数据：int(NaN) 会 ValueError、int(Infinity) 会 OverflowError，而
    /api/status 依赖本函数汇总——一次坏数据不能让面板概览永久 500。
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        # 超大整数（10**400）转 float 会 OverflowError：同样按坏数据归零
        return 0
    if not math.isfinite(number):
        return 0
    return int(number)


def _visible_preferences(preferences: dict[str, Any]) -> dict[str, Any]:
    """面板 / 导出的偏好可见口径：与 CLI 的 has_preference_value 一致。

    过滤 None / 空白串 / 空容器，列表内逐项剔除 null 与空白项；全空的
    列表整体隐藏（此前 [null] / "   " 会原样返回，面板渲染成「东航、null」）。
    """
    visible: dict[str, Any] = {}
    for key, value in preferences.items():
        if isinstance(value, list):
            items = [
                item
                for item in value
                if not (item is None or (isinstance(item, str) and not item.strip()))
            ]
            if items:
                visible[key] = items
        elif has_preference_value(value):
            visible[key] = value
    return visible


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
            message_calls = _safe_usage_int(usage.get("calls"))
            calls += message_calls
            input_tokens += _safe_usage_int(usage.get("input_tokens"))
            output_tokens += _safe_usage_int(usage.get("output_tokens"))

            timestamp = str(message.get("timestamp") or "")
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp)
                if parsed_timestamp.tzinfo is not None:
                    # PostgreSQL timestamptz 序列化为 UTC；统一转到本地时区再取日期
                    parsed_timestamp = parsed_timestamp.astimezone()
                message_day = parsed_timestamp.date().isoformat()
            except (ValueError, OSError, OverflowError):
                # Windows 上 astimezone() 对越界年份还会抛 OSError/OverflowError
                message_day = ""
            if message_day:
                bucket = by_day.setdefault(message_day, {"calls": 0, "tokens": 0})
                bucket["calls"] += message_calls
                bucket["tokens"] += _safe_usage_int(usage.get("total_tokens"))

        elapsed = meta.get("elapsed")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            try:
                value = float(elapsed)
            except OverflowError:
                value = float("nan")
            # 历史数据可能含 NaN / Infinity（旧版本导入），或两条超大的
            # 有限值相加溢出为 inf：非有限值会让 avg_elapsed 变成 inf，
            # 状态接口的 JSON 序列化（allow_nan=False）每次 500
            if math.isfinite(value):
                elapsed_total += value
                elapsed_count += 1
        agents = meta.get("agents")
        # 备份导入可能带入非列表 agents（如 5 / true）：直接迭代会 TypeError，
        # 让状态接口 / Markdown 导出对这条坏数据永久 500
        if isinstance(agents, list):
            for agent in agents:
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
        "avg_elapsed": (
            round(elapsed_total / elapsed_count, 2)
            if elapsed_count and math.isfinite(elapsed_total)
            else 0
        ),
        "top_agents": [
            {"name": name, "count": count} for name, count in agent_counts.most_common(5)
        ],
        "by_day": recent_days,
    }


def _md_inline(value: object) -> str:
    """导出 Markdown 的单行内联文本：折叠空白 + 尖括号转义（与 CLI 同口径）。"""
    return (
        " ".join(str(value if value is not None else "").split())
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _user_data_markdown(user_id: str, data: dict[str, Any]) -> str:
    """把用户长期记忆整理为便于阅读的 Markdown（偏好 / 行程 / 统计 / 最近会话）。"""
    lines = [
        "# Aligo 用户数据导出",
        "",
        f"- 用户：{_md_inline(user_id)}",
        f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    preferences = _visible_preferences(data.get("preferences") or {})
    lines.extend(["## 用户偏好", ""])
    if preferences:
        for key, value in preferences.items():
            text = (
                "、".join(str(item) for item in value)
                if isinstance(value, list)
                else str(value)
            )
            lines.append(f"- {_md_inline(key)}：{_md_inline(text)}")
    else:
        lines.append("- （暂无）")

    lines.extend(["", "## 最近行程", ""])
    trips = data.get("trip_history") or []
    if trips:
        for trip in trips[-10:]:
            route = (
                f"{_md_inline(trip.get('origin') or '—')} → "
                f"{_md_inline(trip.get('destination') or '—')}"
            )
            dates = " ~ ".join(
                part for part in (trip.get("start_date"), trip.get("end_date")) if part
            ) or "日期未记录"
            lines.append(
                f"- {route}（{_md_inline(dates)}，"
                f"{_md_inline(trip.get('purpose') or '出行')}）"
            )
    else:
        lines.append("- （暂无）")

    history = data.get("chat_history") or []
    stats = data.get("statistics") or {}
    usage = _usage_summary(history)
    archived_ids = set(data.get("archived_sessions") or [])
    lines.extend(
        [
            "",
            "## 统计",
            "",
            f"- 历史行程：{stats.get('total_trips', len(trips))}",
            f"- 对话消息：{stats.get('total_messages', len(history))}",
            f"- 归档会话：{_archived_count(data)}",
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
            suffix = "（已归档）" if session["session_id"] in archived_ids else ""
            lines.append(
                f"- {_md_inline(session['title'])}"
                f"（{session['message_count']} 条消息）{suffix}"
            )
    else:
        lines.append("- （暂无）")
    lines.append("")
    return "\n".join(lines)


async def _get_session_marked(
    state: AligoWebApp, user_id: str, session_id: str | None
) -> _WebSession:
    """在线程池打开会话并标记占用；等待期间被取消时由完成回调补 end_turn。

    ``asyncio.to_thread`` 里的会话构造不受协程取消影响：客户端断开 / 进程
    停机时线程仍会 ``begin_turn``，而被取消的调用方拿不到会话对象、无法
    ``end_turn``——占用标记会一直钉住会话（1h 租约兜底才回收），累计到
    并发上限后新会话全部 503。这里 shield 内层任务，取消时挂完成回调。
    """
    future = asyncio.ensure_future(
        asyncio.to_thread(state.get_session, user_id, session_id, True)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:

        def _release_late(task: asyncio.Future) -> None:
            if task.cancelled():
                return
            try:
                late_session = task.result()
            except Exception:  # noqa: BLE001 - 构造失败没有占用可释放
                return
            late_session.end_turn()

        future.add_done_callback(_release_late)
        raise


def create_app(model: Any = None) -> FastAPI:
    """创建 FastAPI 应用（测试时可注入自定义模型）。"""
    state = AligoWebApp(model=model)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.close_all()
        # 取消排队未执行的导入；正在执行的线程是 daemon，不阻塞进程退出
        state.import_executor.shutdown(wait=False, cancel_futures=True)
        state.kb_import_executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Aligo 智能旅行助手", version=APP_VERSION, lifespan=lifespan)
    app.state.aligo = state
    # Starlette 后注册的中间件在最外层：压缩放在内层，保证 RequestContextMiddleware
    # 能给压缩后的响应补安全头 / 请求 ID，并在异常时可靠地兜底 500。
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    # 分块传输没有 Content-Length，需在读取时按实际字节数兜底限制
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 校验错误的 input 可能包含 NaN（JSON 解析允许），直接返回会序列化失败；
        # /api/import 的 64MB 上限下还会把整份备份回显进 422 响应（内存与
        # 响应体积都被放大）——input 统一截断为 200 字符（见
        # _trim_validation_item，/api/import 的手工解析错误共用同一实现）
        errors = [_trim_validation_item(dict(item)) for item in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_value(jsonable_encoder(errors))},
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        # 入口页必须每次重新验证：缓存的旧 index.html 搭配新 app.js（新增
        # DOM id）会让 bindEvents 找不到元素、整页脚本中止；ETag 命中时
        # 重验证只回 304，开销可以忽略
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        rate = _rate_limit_info(state)
        skills_root = resolve_skills_root()
        breaker_state = state.breaker.get_status()["state"]
        return {
            # 熔断打开说明服务已无法正常对话：健康检查不能继续报 ok
            "status": "degraded" if breaker_state == "open" else "ok",
            "circuit_breaker": breaker_state,
            "version": APP_VERSION,
            "model": LLM_CONFIG.get("model_name", ""),
            "has_api_key": bool(LLM_CONFIG.get("api_key")),
            "storage": describe_backends(),
            "rate_limit": rate["backend"],
            "rate_limit_configured": rate["configured"],
            "rate_limit_degraded": rate["degraded"],
            # 并发会话容量：全部会话在用时的超额对话会收到 503（见
            # SessionCapacityError），运维需要能看到当前占用与上限
            "sessions": {
                "cached": len(state.sessions),
                "max": state.max_sessions,
            },
            "skills": {
                "count": len(skill_scripts(skills_root)),
                "dir_found": skills_root.is_dir(),
            },
            "time": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/weather")
    async def weather(
        request: Request,
        city: str = Query(..., min_length=1, max_length=50, pattern=r"^[^\x00-\x1F\x7F]*$"),
    ) -> JSONResponse:
        """目的地天气（Open-Meteo，无 API Key；专用线程池，进程内缓存 10 分钟）。"""
        name = city.strip()
        if not name:
            raise HTTPException(status_code=422, detail="城市不能为空")
        headers = await asyncio.to_thread(
            _enforce_weather_rate_limit, state, request
        )
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _get_weather_executor(), fetch_weather, name
            )
        except WeatherError as exc:
            message = str(exc)
            logger.warning("天气查询失败（%s）：%s", name, message)
            status_code = 404 if message.startswith("未找到城市") else 503
            raise HTTPException(status_code=status_code, detail=message) from exc
        return JSONResponse(content=jsonable_encoder(data), headers=headers)

    @app.get("/api/metrics")
    def metrics() -> dict[str, Any]:
        """进程内请求指标与限流后端信息。

        多进程 / 多实例部署时请求指标各进程独立统计；限流在配置 Redis 后共享。
        """
        return {
            "requests": REQUEST_METRICS.snapshot(),
            "circuit_breaker": state.breaker.get_status(),
            "sessions_cached": len(state.sessions),
            "sessions_max": state.max_sessions,
            "rate_limit": _rate_limit_info(state),
        }

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        """Prometheus 文本指标（进程内统计，多实例需分别采集）。"""
        breaker = state.breaker.get_status()
        breaker_state = {"closed": 0, "half_open": 1, "open": 2}.get(
            str(breaker.get("state")), 3
        )
        body = REQUEST_METRICS.render_prometheus()
        rate = _rate_limit_info(state)
        body += (
            "# HELP aligo_rate_limit_degraded 1 if any rate limiter is in fallback.\n"
            "# TYPE aligo_rate_limit_degraded gauge\n"
            f"aligo_rate_limit_degraded {1 if rate['degraded'] else 0}\n"
            "# HELP aligo_rate_limit_backend Current rate limit backend (one-hot).\n"
            "# TYPE aligo_rate_limit_backend gauge\n"
            f'aligo_rate_limit_backend{{backend="{rate["backend"]}"}} 1\n'
        )
        body += (
            "# HELP aligo_circuit_breaker_state Circuit breaker state "
            "(0 closed, 1 half_open, 2 open, 3 unknown).\n"
            "# TYPE aligo_circuit_breaker_state gauge\n"
            f"aligo_circuit_breaker_state {breaker_state}\n"
            "# HELP aligo_circuit_breaker_failures Circuit breaker failure count "
            "(frozen at the trip threshold while open / half_open).\n"
            "# TYPE aligo_circuit_breaker_failures gauge\n"
            f"aligo_circuit_breaker_failures {int(breaker.get('failure_count') or 0)}\n"
            "# HELP aligo_sessions_cached Cached sessions in this process.\n"
            "# TYPE aligo_sessions_cached gauge\n"
            f"aligo_sessions_cached {len(state.sessions)}\n"
            "# HELP aligo_sessions_max Concurrent session capacity "
            "(excess chats get 503 while all sessions are in use).\n"
            "# TYPE aligo_sessions_max gauge\n"
            f"aligo_sessions_max {state.max_sessions}\n"
        )
        backends = describe_backends()
        body += (
            "# HELP aligo_storage_backend Current storage backend (one-hot).\n"
            "# TYPE aligo_storage_backend gauge\n"
            f'aligo_storage_backend{{short_term="{backends["short_term"]}",'
            f'long_term="{backends["long_term"]}"}} 1\n'
            "# HELP aligo_short_term_degraded 1 if short-term backend fell back.\n"
            "# TYPE aligo_short_term_degraded gauge\n"
            f"aligo_short_term_degraded {1 if backends.get('short_term_degraded') else 0}\n"
            "# HELP aligo_long_term_degraded 1 if long-term backend fell back.\n"
            "# TYPE aligo_long_term_degraded gauge\n"
            f"aligo_long_term_degraded {1 if backends.get('long_term_degraded') else 0}\n"
            "# HELP aligo_tombstones_shared 1 if shared deletion tombstones are active.\n"
            "# TYPE aligo_tombstones_shared gauge\n"
            f"aligo_tombstones_shared {1 if state.shared_tombstones_active() else 0}\n"
        )
        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/doctor")
    async def doctor(
        request: Request,
        response: Response,
        network: bool = Query(default=False),
    ) -> dict[str, Any]:
        """环境自检（默认不做网络请求；network=true 时检查 LLM 连通性）。"""
        # network=true 每次都会真实调用一次 LLM：未鉴权的自检接口同样
        # 需要限流（与知识库高开销接口共用桶）
        response.headers.update(
            await asyncio.to_thread(_enforce_kb_rate_limit, state, request)
        )
        results = await run_doctor(include_network=network)
        return {
            "status": "fail" if any(item.status == "fail" for item in results) else "ok",
            "checks": [
                {"name": item.name, "status": item.status, "detail": item.detail}
                for item in results
            ],
        }

    @app.get("/api/knowledge")
    def knowledge() -> dict[str, Any]:
        """知识库状态（集合 / 片段数 / 是否可用）。"""
        return knowledge_status()

    @app.get("/api/knowledge/documents")
    def knowledge_documents(
        request: Request,
        response: Response,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=100),
        category: str = Query(default="", max_length=METADATA_VALUE_MAX_CHARS),
    ) -> dict[str, Any]:
        """列出知识片段（记忆面板的知识库维护）。

        每页都在写锁内全量扫描整个集合，按 IP 走知识库限流桶，避免单个
        客户端循环调用把 CPU / 写锁占满。
        """
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return list_documents(limit=limit, offset=offset, query=q, category=category)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{exc}（先运行 python .claude/skills/ask-question/script/init_knowledge_base.py）",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转为 503，避免泄露内部堆栈
            logger.warning("列出知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"知识库暂不可用：{exc}") from exc

    @app.get("/api/knowledge/categories")
    def knowledge_categories(request: Request, response: Response) -> dict[str, Any]:
        """知识片段分类统计（维护面板筛选下拉框）。

        统计会全量读取整库，同样按 IP 走知识库限流桶。
        """
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return {"categories": list_categories()}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=f"{exc}（先运行初始化脚本）") from exc
        except Exception as exc:  # noqa: BLE001 - 统一转为 503
            logger.warning("统计知识分类失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"知识库暂不可用：{exc}") from exc

    @app.get("/api/knowledge/search")
    def knowledge_search(
        request: Request,
        response: Response,
        q: str = Query(..., min_length=1, max_length=100),
        top_k: int = Query(default=5, ge=1, le=10),
    ) -> dict[str, Any]:
        """语义检索知识片段（向量相似度排序；首次调用会加载嵌入模型）。"""
        query = q.strip()
        if not query:
            # 纯空白是客户端输入错误，不能被后面的「查询内容为空」映射成 503
            # 服务端故障；校验放在限流之前，避免无效请求消耗知识库额度
            raise HTTPException(status_code=422, detail="查询内容不能为空")
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            results = search_documents(query, top_k=top_k)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 检索失败统一提示
            logger.warning("语义检索知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"检索失败：{exc}") from exc
        return {"query": query, "results": results}

    @app.get("/api/knowledge/eval")
    def knowledge_eval(request: Request, response: Response) -> dict[str, Any]:
        """离线检索评测：14 个差旅问题计算 hit@1/3/5 与 MRR（不调用大模型）。"""
        token = state.eval_lock.acquire()
        if token is None:
            raise HTTPException(
                status_code=429,
                detail="已有检索评测在运行，请稍后再试",
                headers={"Retry-After": "5"},
            )
        try:
            # 先拿评测锁再计额度：已有评测时的 429 不应消耗知识库配额
            response.headers.update(_enforce_kb_rate_limit(state, request))
            status = knowledge_status()
            if not status.get("available"):
                raise HTTPException(
                    status_code=503,
                    detail=f"知识库不可用：{status.get('reason', '未知原因')}",
                )

            try:
                from agents.rag_knowledge_agent import RAGKnowledgeAgent
                from utils.rag_eval import evaluate_retrieval
            except ImportError as exc:
                logger.warning("检索评测依赖不可用: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"评测依赖不可用：{exc}"
                ) from exc

            try:
                agent = RAGKnowledgeAgent(collection_name=status["collection"])
            except Exception as exc:  # noqa: BLE001 - 嵌入模型缺失 / 离线下载失败等
                logger.warning("检索评测初始化失败: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"评测初始化失败：{exc}"
                ) from exc
            if not agent.initialized:
                raise HTTPException(status_code=503, detail="RAG 依赖未安装")
            try:
                # 14 个问题逐个检索，首次会加载嵌入模型；接口为 sync，FastAPI 在线程池执行
                from config import RAG_CONFIG

                result = evaluate_retrieval(
                    lambda query, top_k: agent.search_knowledge(
                        query, top_k=top_k, strict=True
                    ),
                    # 与问答链路同一相似度阈值：低于阈值的命中不计入
                    min_score=float(RAG_CONFIG.get("min_score", 0.5)),
                )
                # 心跳续期发现共享锁已失效（过期后被其它实例拿走），或配置了
                # Redis 但加锁时回退到进程内（共享锁实际不存在）：本次评测
                # 可能与之并发，结果需要打标而不能当作独占评测
                lock_lost = bool(getattr(state.eval_lock, "lost", False))
                lock_degraded = bool(getattr(state.eval_lock, "degraded", False))
                lock_expected = bool(STORAGE_CONFIG.get("redis_url"))
                if (
                    lock_lost or (lock_degraded and lock_expected)
                ) and isinstance(result, dict):
                    logger.warning(
                        "检索评测期间共享锁不可用（lost=%s, degraded=%s），"
                        "结果可能受并发评测影响",
                        lock_lost,
                        lock_degraded,
                    )
                    result["warning"] = (
                        "评测期间共享锁不可用（可能存在并发评测），结果仅供参考"
                    )
                return result
            except Exception as exc:  # noqa: BLE001 - 检索失败统一提示
                logger.warning("检索评测失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"评测失败：{exc}") from exc
        finally:
            state.eval_lock.release(token)

    @app.get("/api/knowledge/duplicates")
    def knowledge_duplicates(
        request: Request,
        response: Response,
        limit: int = Query(default=20, ge=0, le=200),
    ) -> dict[str, Any]:
        """列出内容完全相同的独立知识片段组（与 CLI ``kb-duplicates`` 同一实现）。

        ``limit=0`` 返回全部组。只统计可单删的独立片段：带 ``group`` 或
        旧版 ``parts > 1`` 的多分片记录内部重复属于同一篇文档，不算重复。
        """
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            groups = find_duplicate_contents()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("读取重复知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"读取失败：{exc}") from exc
        shown = groups if limit <= 0 else groups[:limit]
        return {
            "total_groups": len(groups),
            "total_fragments": sum(len(ids) for _, ids in groups),
            "shown": len(shown),
            "groups": [
                {
                    "content": content,
                    "preview": " ".join(content.split())[:60],
                    "ids": list(ids),
                }
                for content, ids in shown
            ],
        }

    @app.post("/api/knowledge/duplicates/cleanup")
    def knowledge_duplicates_cleanup(
        payload: KnowledgeDedupePayload,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        """清理内容完全相同的独立片段（每组保留最小 id；默认只预览）。

        与 CLI ``kb-dedupe`` 同一语义：``dry_run=false`` 才真正删除。扫描
        与删除在同一把写锁内完成、逐条复核内容未变化（并发编辑 / 删除不会
        被按过期 id 误删），已消失 / 已变化的条目记入 ``skipped``。
        """
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return cleanup_duplicate_contents(dry_run=payload.dry_run)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 读取 / 删除失败统一提示
            logger.warning("清理重复知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"清理失败：{exc}") from exc

    @app.post("/api/knowledge/documents", status_code=201)
    def knowledge_add(
        payload: KnowledgeDocumentPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        """新增知识片段（首次调用会加载嵌入模型，可能需要数秒）。"""
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return add_document(payload.content, payload.metadata)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{exc}（先运行 python .claude/skills/ask-question/script/init_knowledge_base.py）",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 嵌入 / 写入失败统一提示
            logger.warning("添加知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"添加失败：{exc}") from exc

    @app.put("/api/knowledge/documents/{doc_id}")
    def knowledge_update(
        payload: KnowledgeUpdatePayload,
        request: Request,
        response: Response,
        doc_id: int = ApiPath(..., ge=1),
    ) -> dict[str, Any]:
        """更新知识片段（内容重新向量化；片段不存在返回 404）。"""
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            metadata = payload.metadata
            if metadata is None:
                # 请求省略 metadata：沿用库中现有元数据（与 CLI kb-update
                # 一致），否则只改正文就会把 category / title / source
                # 等一并清空
                existing = get_document(doc_id)
                if existing is None:
                    raise LookupError("知识片段不存在")
                metadata = dict(existing.get("metadata") or {})
            return update_document(doc_id, payload.content, metadata)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{exc}（先运行 python .claude/skills/ask-question/script/init_knowledge_base.py）",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 嵌入 / 写入失败统一提示
            logger.warning("更新知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"更新失败：{exc}") from exc

    @app.get("/api/knowledge/documents/{doc_id}/group")
    def knowledge_document_group(
        request: Request, response: Response, doc_id: int = ApiPath(..., ge=1)
    ) -> dict[str, Any]:
        """返回片段所属的整篇文档（多分片按 part 拼接），供整份编辑回填。"""
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            result = get_document_group(doc_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("读取整组知识文档失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"读取失败：{exc}") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="知识片段不存在")
        return result

    @app.delete("/api/knowledge/documents/{doc_id}")
    def knowledge_delete(
        request: Request,
        response: Response,
        doc_id: int = ApiPath(..., ge=1),
        group: bool = Query(default=False),
    ) -> dict[str, Any]:
        """按 id 删除知识片段；``group=1`` 时连同同组分片一起删除。"""
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            removed = delete_document(doc_id, whole_group=group)
        except ValueError as exc:
            # 多分片记录不允许单条删除（须整组删除），属于客户端用法错误
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=f"{exc}（先运行初始化脚本）") from exc
        except Exception as exc:  # noqa: BLE001 - 删除失败统一提示
            logger.warning("删除知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc
        if not removed:
            raise HTTPException(status_code=404, detail="知识片段不存在")
        return {
            "id": doc_id,
            "deleted": True,
            "group": bool(group),
            "count": int(removed),
        }

    @app.get("/api/knowledge/export")
    def knowledge_export(request: Request) -> Response:
        """导出全部知识片段为 JSON 文件（备份 / 迁移）。"""
        rate_headers = _enforce_kb_rate_limit(state, request)
        try:
            documents = export_documents()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 导出失败统一提示
            logger.warning("导出知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"导出失败：{exc}") from exc
        payload = {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "documents": documents,
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="aligo-knowledge.json"',
                **rate_headers,
            },
        )

    @app.post("/api/knowledge/import")
    async def knowledge_import(
        payload: KnowledgeImportPayload,
        request: Request,
        response: Response,
        dry_run: bool = Query(default=False),
    ) -> dict[str, Any]:
        """批量导入知识片段（来自导出的 JSON；单次最多 200 条）。

        ``dry_run=1`` 时只校验 / 去重并返回 ``would_import``，不写库。

        导入在知识库专用单 worker 执行器里执行：整批嵌入会在进程写锁内
        串行跑分钟级，占用 Starlette 共享线程池会让面板数据接口一起排队。
        """
        response.headers.update(
            await asyncio.to_thread(_enforce_import_rate_limit, state, request)
        )
        if not state.kb_import_queue_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "已有知识库导入任务在处理，请稍后再试"
                    f"（请求 ID：{_request_id(request)}）"
                ),
            )
        try:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(
                    state.kb_import_executor,
                    lambda: import_documents(payload.documents, dry_run=dry_run),
                )
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503, detail=f"{exc}（先运行初始化脚本）"
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - 导入失败统一提示
                logger.warning("导入知识片段失败: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"导入失败：{exc}"
                ) from exc
        finally:
            state.kb_import_queue_slots.release()

    @app.post("/api/knowledge/import/preview")
    async def knowledge_import_preview(
        payload: KnowledgeImportPreviewPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        """预览导入（不写库）：整个备份一次校验 / 去重，返回将导入 / 跳过的数量。"""
        response.headers.update(
            await asyncio.to_thread(_enforce_import_rate_limit, state, request)
        )
        if not state.kb_import_queue_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "已有知识库导入任务在处理，请稍后再试"
                    f"（请求 ID：{_request_id(request)}）"
                ),
            )
        try:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(
                    state.kb_import_executor,
                    lambda: import_documents(payload.documents, dry_run=True),
                )
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503, detail=f"{exc}（先运行初始化脚本）"
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - 预览失败统一提示
                logger.warning("预览导入知识片段失败: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"预览失败：{exc}"
                ) from exc
        finally:
            state.kb_import_queue_slots.release()

    @app.get("/api/status")
    def status(
        request: Request,
        response: Response,
        user_id: str = Query(default="web_user", pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        response.headers.update(_enforce_read_rate_limit(state, request))
        data = _load_user_data(user_id)
        preferences = _visible_preferences(data["preferences"])
        today = datetime.now().date()
        travel_summary = summarize_travel(data["trip_history"], today=today)
        active_trips: list[tuple[datetime, dict[str, Any]]] = []
        upcoming_trips: list[tuple[datetime, dict[str, Any]]] = []
        for trip in data["trip_history"]:
            interval = _trip_interval(trip)
            if interval is None:
                continue
            start_dt, end_dt = interval
            if start_dt.date() <= today <= end_dt.date():
                active_trips.append((end_dt, trip))
            elif start_dt.date() > today:
                upcoming_trips.append((start_dt, trip))
        # 进行中的行程优先展示（多条时取结束最晚的）
        active_trips.sort(key=lambda item: item[0])
        upcoming_trips.sort(
            key=lambda item: (item[0], 0 if item[1].get("session_id") else 1)
        )
        if active_trips:
            featured_trip = active_trips[-1][1]
        elif upcoming_trips:
            featured_trip = upcoming_trips[0][1]
        else:
            featured_trip = None
        # 最近 5 条行程附带与其它行程的日期重叠信息（列表用徽标提示）
        trips_view = _trips_with_conflicts(
            data["trip_history"][-5:], data["trip_history"]
        )
        return {
            "user_id": user_id,
            "preferences": preferences,
            "trip_count": len(data["trip_history"]),
            "travel_summary": travel_summary,
            "trips": trips_view,
            "next_trip": featured_trip,
            "chat_count": len(data["chat_history"]),
            "archived_count": _archived_count(data),
            "statistics": data["statistics"],
            "storage": describe_backends(),
            "usage": _usage_summary(data["chat_history"]),
        }

    @app.put("/api/preferences/{pref_type}")
    def update_preference(
        payload: PreferencePayload,
        pref_type: str = ApiPath(..., pattern=_PREF_TYPE_PATTERN),
    ) -> dict[str, Any]:
        """新增或覆盖一条用户偏好（记忆面板的手动维护入口）。"""
        # 与 CLI pref-set / 面板显示口径一致：空值 = 未设置，不能回显 200
        # 却让偏好从 /api/status 消失（清空请用 DELETE）
        if not has_preference_value(payload.value):
            raise HTTPException(
                status_code=422, detail="偏好值不能为空（清空请使用 DELETE 接口）"
            )
        store = _open_long_term(payload.user_id)
        try:
            store.save_preference(pref_type, payload.value)
            value = store.get_preference(pref_type)
        except Exception as exc:  # noqa: BLE001 - 写盘失败统一提示
            logger.warning("保存偏好失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"保存失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {"type": pref_type, "value": value}

    @app.delete("/api/preferences/{pref_type}")
    def delete_preference(
        pref_type: str = ApiPath(..., pattern=_PREF_TYPE_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """删除一条用户偏好（记忆面板的手动维护入口）。"""
        # 删除是破坏性操作：PostgreSQL 不可用时不能静默作用在回退 JSON 上
        # （恢复后偏好会「复活」），strict 打开会返回 503
        store = _open_long_term(user_id, strict=True)
        try:
            removed = store.delete_preference(pref_type)
        except Exception as exc:  # noqa: BLE001 - 写盘失败统一提示
            logger.warning("删除偏好失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {"type": pref_type, "deleted": removed}

    @app.get("/api/sessions")
    def list_sessions(
        request: Request,
        response: Response,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=100),
        archived: bool = Query(default=False),
    ) -> dict[str, Any]:
        response.headers.update(_enforce_read_rate_limit(state, request))
        data = _load_user_data(user_id)
        titles = data.get("session_titles") or {}
        pinned = data.get("pinned_sessions") or []
        archived_ids = data.get("archived_sessions") or []
        sessions = _group_sessions(
            data["chat_history"],
            1_000_000,
            q,
            titles,
            pinned,
            archived_ids,
            "only" if archived else "exclude",
        )
        total = len(sessions)
        return {
            "sessions": sessions[offset : offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "archived": archived,
            "archived_count": _archived_count(data),
        }

    @app.get("/api/sessions/export")
    def export_sessions(
        request: Request,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        format: Literal["md", "html"] = Query(default="md"),
    ) -> Response:
        """导出全部会话为单个文件（按最近更新排序，含归档会话）。

        ``format=html`` 与 CLI ``sessions-export --html`` 同一渲染口径
        （单文件内嵌样式、全部内容单次转义）。

        必须声明在 ``/api/sessions/{session_id}`` 之前，否则会被动态路由捕获。
        """
        rate_headers = _enforce_read_rate_limit(state, request)
        store = _open_long_term(user_id)
        try:
            messages = store.get_chat_history()
            titles = store.get_session_titles()
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出全部会话失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"读取会话失败：{exc}") from exc
        finally:
            _close_long_term(store)
        if not messages:
            raise HTTPException(status_code=404, detail="没有可导出的会话")
        safe_user = _safe_filename(user_id, "user")
        if format == "html":
            html, count = render_all_sessions_html(user_id, messages, titles)
            filename = f"aligo_sessions_{safe_user}.html"
            logger.info("Exported %s sessions (html) for %s", count, user_id)
            return Response(
                content=html,
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    **rate_headers,
                },
            )
        markdown, count = render_all_sessions_markdown(user_id, messages, titles)
        filename = f"aligo_sessions_{safe_user}.md"
        logger.info("Exported %s sessions for %s", count, user_id)
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **rate_headers,
            },
        )

    @app.get("/api/sessions/{session_id}")
    def get_session_messages(
        request: Request,
        response: Response,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        response.headers.update(_enforce_read_rate_limit(state, request))
        store = _open_long_term(user_id)
        try:
            messages = store.get_chat_history(session_id=session_id)
            titles = store.get_session_titles()
            archived_ids = set(store.get_archived_sessions())
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("读取会话失败（%s）：%s", session_id, exc)
            raise HTTPException(status_code=503, detail=f"读取会话失败：{exc}") from exc
        finally:
            _close_long_term(store)
        title = str(titles.get(session_id) or "")
        if not title:
            for message in messages:
                if message.get("role") == "user":
                    title = " ".join(str(message.get("content", "")).split())[:40]
                    break
        return {
            "session_id": session_id,
            "title": title or "新对话",
            "archived": session_id in archived_ids,
            "messages": [
                {
                    "role": message.get("role", "assistant"),
                    "content": _render_stored_message(message),
                    # 旧数据 / 手写记忆文件里的 _meta 可能含 NaN：原样放进
                    # 响应会让 JSON 序列化（allow_nan=False）失败、该会话
                    # 每次打开都 500，这里统一归一为 None
                    "meta": _json_safe_value(
                        extract_turn_meta(str(message.get("content", "")))
                    )
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
            # 存在性检查与写入在存储层的同一把写锁内完成：
            # 并发 DELETE 落在两者之间时不会写入幽灵标题
            if not store.set_session_title(session_id, payload.title):
                raise LookupError("会话不存在")
            saved = (store.get_session_titles() or {}).get(session_id, "")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
        except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
            logger.warning("设置会话标题失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"保存失败：{exc}") from exc
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
            if not store.set_session_pinned(session_id, payload.pinned):
                raise LookupError("会话不存在")
            pinned = store.get_pinned_sessions()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
        except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
            logger.warning("设置会话置顶失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"保存失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {"session_id": session_id, "pinned": session_id in pinned}

    @app.put("/api/sessions/{session_id}/archive")
    def archive_session(
        payload: SessionArchivePayload,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """归档 / 恢复会话（归档只从列表隐藏，不删除消息）。"""
        store = _open_long_term(payload.user_id)
        try:
            if not store.set_session_archived(session_id, payload.archived):
                raise HTTPException(status_code=404, detail="会话不存在或没有消息")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
            logger.warning("归档会话失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"操作失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {"session_id": session_id, "archived": payload.archived}

    @app.get("/api/sessions/{session_id}/export")
    def export_session(
        request: Request,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
        format: Literal["md", "html"] = Query(default="md"),
    ) -> Response:
        """把一个会话导出为 Markdown 或独立 HTML（便于保存或分享行程）。

        ``format=html`` 与 CLI ``session-export --html`` 同一渲染口径
        （单文件内嵌样式、全部动态文本单次转义，可分享 / 打印）。
        """
        rate_headers = _enforce_read_rate_limit(state, request)
        store = _open_long_term(user_id)
        try:
            messages = store.get_chat_history(session_id=session_id)
            titles = store.get_session_titles()
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出会话失败（%s）：%s", session_id, exc)
            raise HTTPException(status_code=503, detail=f"读取会话失败：{exc}") from exc
        finally:
            _close_long_term(store)
        if not messages:
            raise HTTPException(status_code=404, detail="会话不存在或没有消息")

        raw_title = str(titles.get(session_id) or "")
        if not raw_title:
            raw_title = "Aligo 对话"
            for message in messages:
                if message.get("role") == "user":
                    raw_title = (
                        " ".join(str(message.get("content", "")).split())[:40]
                        or raw_title
                    )
                    break
        filename_base = _safe_filename(session_id, "conversation")
        if format == "html":
            # 标题传原文：render_session_html 在出口统一转义（预转义会变成
            # 字面量 &lt;b&gt;，与 CLI session-export --html 同一口径）
            return Response(
                content=render_session_html(user_id, session_id, raw_title, messages),
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="aligo_{filename_base}.html"'
                    ),
                    **rate_headers,
                },
            )
        # 自定义标题允许含换行与尖括号：不折叠会伪造导出的段落结构，
        # 不转义则 <script> 原样落盘（与合集导出、CLI 单会话导出同一口径）
        title = sanitize_title(raw_title)

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
            lines.extend(
                [f"## {role}", "", sanitize_body(content.strip()), ""]
            )

        filename = f"aligo_{filename_base}.md"
        return Response(
            content="\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **rate_headers,
            },
        )

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """删除会话的全部消息与元数据；等待进行中的对话结束后再删除，避免写回。"""
        async with state.lock_for(user_id):
            def _exists() -> bool:
                # 破坏性操作：PostgreSQL 不可用时 strict 打开返回 503，
                # 不能在回退 JSON 上判断「不存在」并撤销墓碑
                store = _open_long_term(user_id, strict=True)
                try:
                    return bool(store.get_chat_history(session_id=session_id))
                finally:
                    _close_long_term(store)

            # 先落墓碑再查存在性：从请求开始其它在途对话就会被拒绝，
            # 不会在存在性检查期间把已删除的会话写回。重复删除（之前已有
            # 墓碑）也要重新写一次：上一次的共享墓碑可能因 Redis 故障没写
            # 成功，这里顺带修复；从未存在的 id 在确认后撤销本地墓碑。
            # token 标记本请求写入的墓碑，回滚时只撤销自己写的那条
            # （共享墓碑可能属于其它实例，不能代为删除）。
            already_marked = await asyncio.to_thread(
                state.is_recently_deleted, user_id, session_id
            )
            token = await asyncio.to_thread(
                state.mark_deleted, user_id, session_id
            )

            try:
                existed = await asyncio.to_thread(_exists)
            except Exception as exc:  # noqa: BLE001 - 读失败统一提示
                if not already_marked:
                    # 没读到存在性：撤销本请求的墓碑（允许该 id 继续使用），
                    # 若共享墓碑属于其它实例，token 不匹配会保留对方的保护
                    await asyncio.to_thread(
                        state.unmark_deleted, user_id, session_id, token
                    )
                logger.warning("检查会话是否存在失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc

            def _delete() -> int:
                store = _open_long_term(user_id, strict=True)
                try:
                    return store.delete_chat_session(session_id)
                finally:
                    _close_long_term(store)

            try:
                # PostgreSQL / JSON 的打开与删除是阻塞 IO，放到线程池，
                # 慢库不能让整个事件循环（其它会话的 SSE 心跳）停住
                removed = await asyncio.to_thread(_delete)
            except Exception as exc:  # noqa: BLE001 - 删除失败统一提示
                if not already_marked:
                    try:
                        still_exists = await asyncio.to_thread(_exists)
                    except Exception:  # noqa: BLE001 - 查询失败：保守保留墓碑
                        still_exists = None
                    if still_exists:
                        # 删除失败但会话仍存在：撤销本请求落的墓碑，恢复正常对话
                        await asyncio.to_thread(
                            state.unmark_deleted, user_id, session_id, token
                        )
                    elif still_exists is False and not existed:
                        # 读到了「确实不存在」且删除前也不存在（从未存在的 id）：
                        # 撤销本请求的墓碑，允许该 id 新开对话；属于其它实例
                        # 的共享墓碑因 token 不匹配而保留
                        await asyncio.to_thread(
                            state.unmark_deleted, user_id, session_id, token
                        )
                logger.warning("删除会话失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc
            if removed == 0 and not existed and not already_marked:
                # 从未存在的 id：撤销本请求的墓碑，允许用该 id 新开对话；
                # 属于其它实例的共享墓碑因 token 不匹配而保留
                await asyncio.to_thread(
                    state.unmark_deleted, user_id, session_id, token
                )
            await asyncio.to_thread(state.drop_session, user_id, session_id)
            await asyncio.to_thread(clear_short_term_session, session_id, user_id)
        return {"session_id": session_id, "deleted": removed}

    async def _delete_last_turn_impl(
        user_id: str, session_id: str, expect: str | None
    ) -> dict[str, Any]:
        """删除最后一轮的共享实现（DELETE 查询串 / POST body 两种入口）。"""
        async with state.lock_for(user_id):
            def _remove_last_turn() -> int:
                # 破坏性操作：PostgreSQL 不可用时 strict 打开返回 503，
                # 不能在回退 JSON 上删除并谎报成功
                store = _open_long_term(user_id, strict=True)
                try:
                    return store.delete_last_turn(session_id, expect)
                finally:
                    _close_long_term(store)

            try:
                # 阻塞的数据库 / 文件操作放到线程池
                removed = await asyncio.to_thread(_remove_last_turn)
            except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
                logger.warning("删除最后一轮对话失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc

            if removed > 0:
                def _truncate_short_term() -> None:
                    # 短期记忆只是上下文缓存：清理失败不影响已完成的长期记忆删除。
                    # Redis 键按 user_id + session_id 隔离，未缓存的会话也可安全清理。
                    session = state.sessions.get(
                        state._session_key(user_id, session_id)
                    )
                    if session is not None:
                        session.runtime.memory.short_term.truncate_last_turn(expect)
                    else:
                        create_short_term_memory(
                            session_id, user_id=user_id
                        ).truncate_last_turn(expect)

                try:
                    await asyncio.to_thread(_truncate_short_term)
                except Exception as exc:  # noqa: BLE001 - 缓存清理失败只记录
                    logger.warning("清理短期记忆失败（%s）: %s", session_id, exc)
        return {
            "session_id": session_id,
            "removed": removed,
            "mismatch": removed == 0 and expect is not None,
        }

    @app.delete("/api/sessions/{session_id}/last-turn")
    async def delete_last_turn(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
        expect: str | None = Query(default=None, max_length=6000),
    ) -> dict[str, Any]:
        """删除会话最后一轮对话（供「编辑上一条提问」后重新发送使用）。

        ``expect`` 为待编辑提问的原文：与最后一轮用户提问不一致时不删除任何
        内容（例如这条提问在意图识别阶段就失败、并未写入历史），避免误删上一轮。
        查询串形式限制 6000 字（h11 请求行上限）；更长的提问用 POST body 形式。
        """
        return await _delete_last_turn_impl(user_id, session_id, expect)

    @app.post("/api/sessions/{session_id}/last-turn")
    async def delete_last_turn_post(
        payload: LastTurnPayload,
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """同 DELETE，但 ``expect`` 走 JSON body。

        CLI 交互 / 备份导入产生的长提问没有 4000 字上限，放进查询串会撞
        FastAPI max_length 或 h11 请求行上限——面板的编辑 / 重新生成在长
        提问上会必然失败。
        """
        return await _delete_last_turn_impl(
            payload.user_id, session_id, payload.expect
        )

    @app.post("/api/chat")
    async def chat(
        request: Request, payload: ChatRequest, response: Response
    ) -> dict[str, Any]:
        rate_headers = await asyncio.to_thread(
            _enforce_rate_limit, state, payload.user_id, request
        )
        async with state.lock_for(payload.user_id):
            if await asyncio.to_thread(
                state.is_recently_deleted, payload.user_id, payload.session_id
            ):
                raise HTTPException(
                    status_code=404, detail="会话已删除，请开始新对话"
                )
            # 构造会话会同步连接 Redis / PostgreSQL（最长数秒），放到线程池；
            # mark_in_use 阻止会话在 ask 期间被 LRU 淘汰 close；请求在等待
            # 期间被取消时，_get_session_marked 负责在构造完成后补 end_turn
            try:
                session = await _get_session_marked(
                    state, payload.user_id, payload.session_id
                )
            except MemoryPersistenceError as exc:
                logger.warning(
                    "打开会话失败（%s）: %s", _request_id(request), exc
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "存储暂时不可用，请稍后再试"
                        f"（请求 ID：{_request_id(request)}）"
                    ),
                ) from exc
            except SessionCapacityError as exc:
                logger.warning(
                    "会话容纳上限（%s）: %s", _request_id(request), exc
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"{exc}（请求 ID：{_request_id(request)}）",
                ) from exc
            try:
                result = await session.runtime.ask(
                    payload.message, turn_id=payload.client_turn_id or ""
                )
            except CircuitOpenError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"服务暂时不可用，请稍后再试（请求 ID：{_request_id(request)}）",
                ) from exc
            except MemoryPersistenceError as exc:
                logger.warning(
                    "对话存储失败（%s）: %s", _request_id(request), exc
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "存储暂时不可用，请稍后再试"
                        f"（请求 ID：{_request_id(request)}）"
                    ),
                ) from exc
            except UserFacingError as exc:
                # 该类异常的消息是给用户看的固定文案（流式路径同样原样返回），
                # 不能吞成通用 500 丢失「重试」提示
                logger.warning(
                    "对话失败（%s）: %s", _request_id(request), exc
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"{exc}（请求 ID：{_request_id(request)}）",
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 对外返回友好错误
                logger.warning(
                    "对话处理失败（%s）: %s",
                    _request_id(request),
                    exc,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "服务器内部错误，请稍后重试"
                        f"（请求 ID：{_request_id(request)}）"
                    ),
                ) from exc
            finally:
                # 取消 / 出错只中断协程：在途的落库线程仍会写完，先等它
                # 结束再释放用户锁，否则删除会话 / 下一轮对话会与迟到的
                # 写入并发（已删除的会话被写回复活）
                try:
                    await session.runtime.drain_pending_persist()
                finally:
                    session.end_turn()
        response.headers.update(rate_headers)
        # reply / agents 来自模型输出：孤立代理项（截断 emoji 的转义）会让
        # JSONResponse 的 UTF-8 编码抛 UnicodeEncodeError（500），响应前统一转义
        return {"session_id": session.session_id, **_json_safe_value(result)}

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
        """SSE 流式对话：先推送执行进度事件，最后推送完整结果或错误。"""
        rate_headers = await asyncio.to_thread(
            _enforce_rate_limit, state, payload.user_id, request
        )
        rid = _request_id(request)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # 用户锁与会话构造放在返回 StreamingResponse 之前：此时 200 响应头
        # 尚未发出，会话已删除返回 404、存储故障返回 503，客户端能拿到明确
        # 的状态码；此前异常发生在首个事件之前，只能得到一个已建立又中断的
        # 空流。锁在流结束时释放。
        lock = state.lock_for(payload.user_id)
        await lock.acquire()
        lease: _SessionTurnLease | None = None
        try:
            if await asyncio.to_thread(
                state.is_recently_deleted, payload.user_id, payload.session_id
            ):
                raise HTTPException(
                    status_code=404, detail="会话已删除，请开始新对话"
                )
            try:
                session = await _get_session_marked(
                    state, payload.user_id, payload.session_id
                )
            except MemoryPersistenceError as exc:
                logger.warning("打开会话失败（%s）: %s", rid, exc)
                raise HTTPException(
                    status_code=503,
                    detail=f"存储暂时不可用，请稍后再试（请求 ID：{rid}）",
                ) from exc
            except SessionCapacityError as exc:
                logger.warning("会话容纳上限（%s）: %s", rid, exc)
                raise HTTPException(
                    status_code=503,
                    detail=f"{exc}（请求 ID：{rid}）",
                ) from exc
            lease = _SessionTurnLease(lock, session)
        except BaseException:
            if lease is not None:
                lease.release()
            else:
                lock.release()
            raise

        async def event_stream() -> Any:
            task: asyncio.Task[None] | None = None
            heartbeat: asyncio.Task[None] | None = None
            try:

                async def run() -> None:
                    try:
                        result = await session.runtime.ask(
                            payload.message,
                            on_event=queue.put_nowait,
                            turn_id=payload.client_turn_id or "",
                        )
                        await queue.put(
                            {"type": "done", "session_id": session.session_id, **result}
                        )
                    except asyncio.CancelledError:
                        raise
                    except CircuitOpenError:
                        logger.warning("流式对话触发熔断（%s）", rid)
                        await queue.put(
                            {
                                "type": "error",
                                "message": f"服务暂时不可用，请稍后再试（请求 ID：{rid}）",
                            }
                        )
                    except MemoryPersistenceError as exc:
                        logger.warning("流式对话存储失败（%s）: %s", rid, exc)
                        await queue.put(
                            {
                                "type": "error",
                                "message": f"存储暂时不可用，请稍后再试（请求 ID：{rid}）",
                            }
                        )
                    except UserFacingError as exc:
                        # 异常消息是给用户看的固定文案（如「意图识别结果解析
                        # 失败，请重试」），可以原样返回
                        logger.warning("流式对话失败（%s）: %s", rid, exc)
                        await queue.put(
                            {
                                "type": "error",
                                "message": f"处理失败：{exc}（请求 ID：{rid}）",
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - 通过事件返回友好错误
                        # 流式响应已发出 200，异常只能通过 error 事件返回。
                        # 原始异常文本可能含内部细节（SQL / 文件路径 / 上游
                        # 响应体），只写日志，不返回给客户端；请求 ID 用于
                        # 与服务端日志对照
                        logger.warning("流式对话失败（%s）: %s", rid, exc, exc_info=True)
                        await queue.put(
                            {
                                "type": "error",
                                "message": (
                                    "处理失败：服务器内部错误，请稍后再试"
                                    f"（请求 ID：{rid}）"
                                ),
                            }
                        )

                # 先告知会话 ID：即使生成被停止或失败，前端也能对这条提问执行编辑 / 重试
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "session", "session_id": session.session_id},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                task = asyncio.create_task(run())
                lease.bind_turn(task)

                async def send_heartbeat() -> None:
                    while True:
                        await asyncio.sleep(SSE_HEARTBEAT_SEC)
                        await queue.put({"type": "heartbeat"})

                if SSE_HEARTBEAT_SEC > 0:
                    heartbeat = asyncio.create_task(send_heartbeat())
                while True:
                    event = await queue.get()
                    if event.get("type") == "heartbeat":
                        yield ": keep-alive\n\n"
                        continue
                    if event.get("type") == "error":
                        # HTTP 已发出 200：对话错误率需要独立口径
                        REQUEST_METRICS.record_sse_error()
                    # SSE 帧按 UTF-8 传输：事件里的模型输出若含孤立代理项，
                    # json.dumps(ensure_ascii=False) 之后编码即抛错、200 流
                    # 无 error/done 直接中断；发送前统一转义
                    yield (
                        f"data: {json.dumps(_json_safe_value(event), ensure_ascii=False)}"
                        "\n\n"
                    )
                    if event["type"] in ("done", "error"):
                        break
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                # 取消在途回合并等待落库线程结束。生成器可能处在 Starlette
                # 的 anyio 取消作用域内，作用域取消后这里的 await 会立刻
                # 再次抛 CancelledError：同步的 task.cancel() 必须在这里
                # 做，等待落库线程由作用域外的 _LeasedStreamingResponse 兜底
                if task is not None and not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease.stop_turn()
                # 释放租约统一由 _LeasedStreamingResponse 在取消作用域外执行：
                # 本轮在作用域内被取消时上面的等待无法完成，这里提前 release
                # 会让删除 / 新一轮请求在落库线程结束前拿到用户锁

        return _LeasedStreamingResponse(
            event_stream(),
            lease,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                **rate_headers,
            },
        )

    @app.get("/api/trips")
    def list_trips(
        request: Request,
        response: Response,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        q: str = Query(default="", max_length=100),
        status: Literal["active", "upcoming", "past"] | None = Query(default=None),
    ) -> dict[str, Any]:
        """列出历史行程（默认记录顺序，支持分页；附带日期重叠信息）。

        ``order=desc`` 返回从最新记录开始的顺序，供记忆面板展示「最近 N 条」；
        ``q`` 按路线 / 日期 / 目的过滤（与面板、CLI ``trips --query`` 同一
        口径：按空白拆词，全部词出现在五字段拼接文本中即命中，词序无关），
        ``status`` 按面板状态徽标过滤（active / upcoming / past ↔
        进行中 / 即将出行 / 已结束），日期重叠仍按完整历史计算。
        """
        response.headers.update(_enforce_read_rate_limit(state, request))
        store = _open_long_term(user_id)
        try:
            all_trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("列出历史行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"读取行程失败：{exc}") from exc
        finally:
            _close_long_term(store)
        trips = _filter_trips_by_query(all_trips, q)
        trips = _filter_trips_by_status(trips, status)
        status_label = ""
        if status is not None:
            status_label = {
                "active": "进行中",
                "upcoming": "即将出行",
                "past": "已结束",
            }[status]
        total = len(trips)
        ordered = trips[::-1] if order == "desc" else trips
        page = ordered[offset : offset + limit]
        return {
            "trips": _trips_with_conflicts(page, all_trips),
            "total": total,
            "offset": offset,
            "limit": limit,
            "order": order,
            "query": q.strip(),
            "status": status_label,
        }

    @app.post("/api/trips", status_code=201)
    def create_trip(payload: TripPayload) -> dict[str, Any]:
        """手动添加一条行程（同路线 + 同起止日期自动去重）。"""
        try:
            start = datetime.strptime(payload.start_date, "%Y-%m-%d")
            end = (
                datetime.strptime(payload.end_date, "%Y-%m-%d")
                if payload.end_date
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
        if end is not None and end < start:
            raise HTTPException(status_code=422, detail="结束日期不能早于出发日期")

        store = _open_long_term(payload.user_id)
        created = False
        conflicts: list[dict[str, Any]] = []
        try:
            before = len(store.get_trip_history(limit=None))
            trip_id = store.save_trip_history(
                {
                    "origin": payload.origin.strip(),
                    "destination": payload.destination.strip(),
                    "start_date": payload.start_date,
                    "end_date": payload.end_date,
                    "purpose": payload.purpose.strip() or "出差",
                }
            )
            trips = store.get_trip_history(limit=None)
            created = len(trips) > before
            # 按「id + 关键字段」定位本次保存的那条并按身份排除：历史里存在
            # 重复 trip_id 时，按 id 整条排除会连带隐藏其它行程的真实重叠
            saved_record = _find_saved_trip(
                trips,
                trip_id,
                {
                    "start_date": payload.start_date,
                    "end_date": payload.end_date,
                    "destination": payload.destination.strip(),
                },
            )
            conflicts = _trip_conflicts(
                trips,
                start,
                end or start,
                exclude_id=trip_id,
                exclude_trip=saved_record,
            )
        except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
            logger.warning("添加行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"添加行程失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {
            "trip_id": trip_id,
            "saved": True,
            "created": created,
            "conflicts": conflicts,
        }

    @app.post("/api/trips/import")
    def import_trips_endpoint(
        payload: TripImportPayload,
        request: Request,
        response: Response,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        dry_run: bool = Query(default=False),
    ) -> dict[str, Any]:
        """从 CSV / iCalendar 文本导入行程（与 CLI ``trips-import`` 同语义）。

        解析与批量写入在导入专用执行器里执行（单 worker + 背压），批量
        保存是 O(N²) 且持用户锁：不能占用共享线程池，也不能并发跑多份。
        """
        response.headers.update(_enforce_import_rate_limit(state, request))
        if not state.import_queue_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "已有导入任务在处理，请稍后再试"
                    f"（请求 ID：{_request_id(request)}）"
                ),
            )
        try:

            def work() -> dict[str, Any]:
                # 与备份恢复一致：配置了 PostgreSQL 但不可用时拒绝写入回退的
                # JSON 库，否则导入「成功」的数据在恢复后凭空消失
                store = _open_long_term(user_id, strict=True)
                try:
                    return import_trips_from_text(
                        store, payload.content, dry_run=dry_run
                    )
                finally:
                    _close_long_term(store)

            try:
                result = state.import_executor.submit(work).result()
            except HTTPException:
                # _open_long_term(strict=True) 的 503（存储暂时不可用）原样
                # 上抛，不能再包一层「导入行程失败」把状态 / 文案改掉
                raise
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - 读取 / 写入失败统一提示
                logger.warning("导入行程失败: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"导入行程失败：{exc}"
                ) from exc
        finally:
            state.import_queue_slots.release()
        if not result["rows"]:
            reasons = "；".join(
                (
                    f"第{item['line']}行：{item['reason']}"
                    if result["format"] == "csv"
                    else str(item["reason"])
                )
                for item in result["skipped"][:5]
            )
            raise HTTPException(
                status_code=400,
                detail=f"没有可导入的行程{f'（{reasons}）' if reasons else ''}",
            )
        return result

    @app.put("/api/trips/{trip_id}")
    def update_trip(
        payload: TripUpdatePayload,
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """更新一条行程（仅修改提供的字段；与其它行程重复时返回 409）。"""
        fields = payload.model_dump(exclude_unset=True)
        fields.pop("user_id", None)
        if not fields:
            raise HTTPException(status_code=400, detail="没有需要更新的字段")
        if "destination" in fields and not fields["destination"]:
            raise HTTPException(status_code=422, detail="目的地不能为空")
        if "start_date" in fields and not fields["start_date"]:
            raise HTTPException(status_code=422, detail="出发日期不能为空")

        store = _open_long_term(payload.user_id)
        conflicts: list[dict[str, Any]] = []
        try:
            trips = store.get_trip_history(limit=None)
            if _ambiguous_trip_id(trips, trip_id):
                raise HTTPException(
                    status_code=409,
                    detail="该 trip_id 对应多条行程，请先运行 python cli.py trips-dedupe 清理",
                )
            current = next(
                (trip for trip in trips if str(trip.get("trip_id")) == trip_id),
                None,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="行程不存在")

            merged = {**current, **fields}
            for key in ("start_date", "end_date"):
                value = fields.get(key)
                if value:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except ValueError as exc:
                        raise HTTPException(
                            status_code=422, detail="日期格式应为 YYYY-MM-DD"
                        ) from exc

            # 两个日期独立解析：返程日期非法（历史脏数据）时不能把已解析
            # 的出发日期一并置空——否则响应里的 conflicts 被静默清空
            start = None
            end = None
            if merged.get("start_date"):
                try:
                    # 存量未补零日期（2026-1-5）先归一：否则仅改其它字段时
                    # conflicts 恒空、补填返程日期被误拒「先设置出发日期」
                    start = datetime.strptime(
                        normalize_identity_date(merged["start_date"]),
                        "%Y-%m-%d",
                    )
                except ValueError:
                    start = None
            if merged.get("end_date"):
                try:
                    end = datetime.strptime(
                        normalize_identity_date(merged["end_date"]), "%Y-%m-%d"
                    )
                except ValueError:
                    end = None
            if fields.get("end_date") and start is None:
                raise HTTPException(status_code=422, detail="填写返程日期前请先设置出发日期")
            if start is not None and end is not None and end < start:
                raise HTTPException(status_code=422, detail="结束日期不能早于出发日期")

            try:
                updated = store.update_trip(trip_id, fields)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not updated:
                raise HTTPException(status_code=404, detail="行程不存在")

            trips = store.get_trip_history(limit=None)
            saved = next(
                (trip for trip in trips if str(trip.get("trip_id")) == trip_id),
                {"trip_id": trip_id, **merged},
            )
            if start is not None:
                conflicts = _trip_conflicts(
                    trips, start, end or start, exclude_id=trip_id
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 写入失败统一提示
            logger.warning("更新行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"更新行程失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return {
            "trip_id": trip_id,
            "saved": True,
            "trip": saved,
            "conflicts": conflicts,
        }

    @app.get("/api/trips/calendar")
    def export_trips_calendar(
        request: Request,
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出全部历史行程为一个 iCalendar 文件。"""
        rate_headers = _enforce_read_rate_limit(state, request)
        store = _open_long_term(user_id)
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出行程日历失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"导出失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return Response(
            content=render_trips_ics(trips, user_id),
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="aligo_trips.ics"',
                **rate_headers,
            },
        )

    @app.get("/api/trips/export")
    def export_trips_file(
        request: Request,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        q: str = Query(default="", max_length=100),
        status: Literal["active", "upcoming", "past"] | None = Query(default=None),
        format: Literal["csv", "html", "ics"] = Query(default="csv"),
    ) -> Response:
        """导出历史行程为 CSV（UTF-8 BOM）/ HTML 台账 / iCalendar 合集。

        ``q`` / ``status`` 与列表接口同一口径过滤后再导出（与面板筛选、
        CLI ``trips-export --status`` 保持一致）；``format=html`` 与 CLI
        ``trips-export --html``、``format=ics`` 与 CLI ``trips-export --ics``
        同一渲染口径（ics 缺少出发日期的行程按既有语义跳过）。
        """
        rate_headers = _enforce_read_rate_limit(state, request)
        store = _open_long_term(user_id)
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"导出失败：{exc}") from exc
        finally:
            _close_long_term(store)
        filtered = _filter_trips_by_status(_filter_trips_by_query(trips, q), status)
        if format == "ics":
            # 与 CLI `trips-export --ics` 同一渲染：过滤后的行程合成一个
            # VCALENDAR（/api/trips/calendar 保留为不带筛选的旧入口）
            return Response(
                content=render_trips_ics(filtered, user_id),
                media_type="text/calendar; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="aligo_trips.ics"',
                    **rate_headers,
                },
            )
        if format == "html":
            return Response(
                content=render_trips_html(filtered, user_id),
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="aligo_trips.html"',
                    **rate_headers,
                },
            )
        return Response(
            content=render_trips_csv(filtered),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="aligo_trips.csv"',
                **rate_headers,
            },
        )

    @app.post("/api/trips/dedupe")
    async def dedupe_trips_endpoint(
        payload: TripDedupePayload, request: Request, response: Response
    ) -> dict[str, Any]:
        """清理重复行程（与 CLI ``trips-dedupe`` 同语义）。

        同路线 + 同起止日期视为重复，保留最早一条；``dry_run`` 只返回
        将删除的条目。判定 / 复核 / 删除与 CLI 共用 ``utils.trip_dedupe``，
        两条入口不会漂移。破坏性操作走 strict 打开：PostgreSQL 配置了但
        不可用时 503，不在回退 JSON 上「清理成功」；全量扫描按读取限流桶
        限速（与列表 / 导出同额度）。

        整个「计划 + 执行」持用户锁：并发的两个去重请求（双标签页 / 超时
        重试）不能各自基于同一快照各删一次（同 id 副本会被删光）。
        """
        user_id = payload.user_id
        dry_run = payload.dry_run
        response.headers.update(_enforce_read_rate_limit(state, request))

        def _run() -> dict[str, Any]:
            store = _open_long_term(user_id, strict=True)
            try:
                try:
                    plan = plan_trip_dedupe(store)
                except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
                    logger.warning("读取行程失败（去重，%s）: %s", user_id, exc)
                    raise HTTPException(
                        status_code=503, detail=f"读取行程失败：{exc}"
                    ) from exc
                if dry_run:
                    return {
                        "user_id": user_id,
                        "dry_run": True,
                        "total_trips": plan["total_trips"],
                        "would_remove": len(plan["duplicates"]),
                        "duplicates": plan["duplicates"],
                        "ambiguous_ids": plan["ambiguous_ids"],
                        "removed": 0,
                    }
                removed, failure = apply_trip_dedupe(
                    store, plan["duplicate_entries"]
                )
                if failure is not None:
                    # 部分删除已生效：如实报告已删除数，重试可继续清理剩余
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"删除过程中失败：{failure}（已删除 {removed} 条，"
                            "可重试）"
                        ),
                    )
                return {
                    "user_id": user_id,
                    "dry_run": False,
                    "total_trips": plan["total_trips"],
                    "removed": removed,
                    "duplicates": plan["duplicates"],
                    "ambiguous_ids": plan["ambiguous_ids"],
                }
            finally:
                _close_long_term(store)

        async with state.lock_for(user_id):
            return await asyncio.to_thread(_run)

    @app.get("/api/trips/{trip_id}/calendar")
    def export_trip_calendar(
        request: Request,
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出一条行程为 iCalendar（.ics）全天事件。"""
        rate_headers = _enforce_read_rate_limit(state, request)
        store = _open_long_term(user_id)
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出行程日历失败（%s）：%s", trip_id, exc)
            raise HTTPException(status_code=503, detail=f"导出失败：{exc}") from exc
        finally:
            _close_long_term(store)
        if _ambiguous_trip_id(trips, trip_id):
            raise HTTPException(
                status_code=409,
                detail="该 trip_id 对应多条行程，请先运行 python cli.py trips-dedupe 清理",
            )
        trip = next(
            (item for item in trips if str(item.get("trip_id")) == trip_id),
            None,
        )
        if trip is None:
            raise HTTPException(status_code=404, detail="行程不存在")
        content = render_trip_ics(trip, user_id)
        if content is None:
            raise HTTPException(status_code=400, detail="该行程缺少出发日期，无法生成日历")
        return Response(
            content=content,
            media_type="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="aligo_{_safe_filename(trip_id, "trip")}.ics"'
                ),
                **rate_headers,
            },
        )

    @app.delete("/api/trips/{trip_id}")
    def delete_trip(
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """删除一条历史行程（清理误记录）。"""
        # 破坏性操作：PostgreSQL 不可用时不能静默作用在回退 JSON 上
        # （恢复后行程会「复活」），strict 打开会返回 503
        store = _open_long_term(user_id, strict=True)
        try:
            trips = store.get_trip_history(limit=None)
            if _ambiguous_trip_id(trips, trip_id):
                raise HTTPException(
                    status_code=409,
                    detail="该 trip_id 对应多条行程，请先运行 python cli.py trips-dedupe 清理",
                )
            removed = store.delete_trip(trip_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 存储写入失败要显式返回 503
            logger.warning("删除行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc
        finally:
            _close_long_term(store)
        if not removed:
            raise HTTPException(status_code=404, detail="行程不存在")
        return {"trip_id": trip_id, "deleted": True}

    @app.get("/api/export")
    def export(
        request: Request,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        format: Literal["json", "md"] = Query(default="json"),
        with_knowledge: bool = Query(default=False),
    ) -> Response:
        """导出用户数据（JSON 备份或 Markdown 摘要）。

        ``with_knowledge=1``（仅 JSON）把全局知识库片段一并写入备份的
        ``knowledge`` 字段（与 CLI ``user-export --with-knowledge`` 同一
        字段），可由 ``/api/import`` 一体化恢复；``format=md`` 忽略该参数。
        """
        rate_headers = _enforce_read_rate_limit(state, request)
        data = _load_user_data(user_id)
        safe_user = _safe_filename(user_id)
        if format == "md":
            return Response(
                content=_user_data_markdown(user_id, data),
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="aligo_{safe_user}.md"',
                    **rate_headers,
                },
            )

        backup: dict[str, Any] = {
            "user_id": user_id,
            "exported_at": datetime.now().isoformat(),
            **data,
        }
        if with_knowledge:
            try:
                backup["knowledge"] = export_documents()
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"知识库导出失败：{exc}（先运行初始化脚本）",
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 导出失败统一提示
                logger.warning("导出知识片段失败: %s", exc)
                raise HTTPException(
                    status_code=503, detail=f"知识库导出失败：{exc}"
                ) from exc
        payload = json.dumps(backup, ensure_ascii=False, indent=2)
        return Response(
            content=payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="aligo_{safe_user}.json"',
                **rate_headers,
            },
        )

    @app.post("/api/import")
    async def user_data_import(
        request: Request,
        response: Response,
        user_id: str = Query(..., pattern=_ID_PATTERN),
        dry_run: bool = Query(default=False),
    ) -> dict[str, Any]:
        """从 ``/api/export`` 的 JSON 备份恢复用户数据（面板「恢复备份」）。

        ``dry_run=1`` 只预览将新增 / 更新 / 跳过的数量，不写库（与知识库
        导入预览同一语义）。

        恢复偏好 / 行程 / 聊天消息 / 会话标题与置顶归档；重复导入幂等
        （行程按路线+日期去重、消息按「会话/角色/内容」多重集去重）。
        整个恢复持有该用户锁（与对话 / 删除串行，避免在途回合把删除的会话
        写回或并发导入重复插入）；墓碑期（最近被删除）的会话消息会被跳过，
        恢复不能复活已删除的会话。PostgreSQL 配置但不可用时用 strict 打开
        返回 503——恢复不能写进回退的本地 JSON。
        """
        import asyncio

        from utils.user_import import (
            drop_sessions,
            import_user_data,
            parse_user_export_data,
            preview_user_import,
        )

        # 破坏性写入接口只接受 application/json：text/plain / 表单属于
        # CORS 简单请求（无预检），任意网页都能跨站提交备份、把会话与行程
        # 写进任意 user_id；JSON 请求会先触发预检，浏览器会拦截跨站写入
        content_type = (
            (request.headers.get("content-type") or "")
            .split(";")[0]
            .strip()
            .lower()
        )
        if content_type != "application/json":
            raise HTTPException(
                status_code=415,
                detail="备份恢复只接受 application/json 请求体",
            )

        # 限流先于解析，而且都走线程池：请求体可达 64MB，Pydantic body 参数
        # 会让 FastAPI 在事件循环上同步 json.loads（实测 64MB 约 3 秒），
        # 所有 SSE 与请求一起停摆；超限的风暴请求也不该先付出解析代价
        response.headers.update(
            await asyncio.to_thread(_enforce_kb_rate_limit, state, request)
        )
        # 排队背压：单 worker + 无界队列会让并发大备份（各 64MB）全部
        # 驻留内存直到 OOM；已有一个导入在跑 / 排队时直接 503，不读请求体
        if not state.import_queue_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail=(
                    "已有导入任务在处理，请稍后再试"
                    f"（请求 ID：{_request_id(request)}）"
                ),
            )
        try:
            loop = asyncio.get_running_loop()
            raw_body = await request.body()
            try:
                payload = await loop.run_in_executor(
                    state.import_executor, UserImportPayload.model_validate_json, raw_body
                )
            except ValidationError as exc:
                # 与全局 RequestValidationError 处理器同一口径：input 截断为
                # 200 字符并归一 NaN——手工解析路径直接回显整份 body 会把 64MB
                # 备份放大进 422 响应（内存 / 响应体积双放大），NaN 还会让响应
                # 序列化失败变 500
                raise HTTPException(
                    status_code=422,
                    detail=_json_safe_value(
                        jsonable_encoder(
                            [_trim_validation_item(dict(item)) for item in exc.errors()]
                        )
                    ),
                ) from exc
            try:
                sections, errors = await loop.run_in_executor(
                    state.import_executor, parse_user_export_data, payload.backup
                )
            except Exception as exc:  # noqa: BLE001 - 边界年份 / 畸形条目等解析异常
                logger.warning("备份解析失败：%s", exc)
                raise HTTPException(
                    status_code=400, detail=f"备份解析失败：{exc}"
                ) from exc
            if not sections:
                detail = "；".join(errors[:3]) or "没有可恢复的数据"
                raise HTTPException(status_code=400, detail=f"无法恢复：{detail}")

            async with state.lock_for(user_id):
                # 墓碑期内的会话刚被删除：跳过其消息与元数据，避免恢复把
                # 已删除的会话重新写回（DELETE 的墓碑只需覆盖这段时间）。
                # 行程里引用的会话同样计入：只有行程、没有消息的保留字会话
                # 也要清掉关联，否则面板里出现一条永远打不开的行程会话链接
                unique_sessions = {
                    message["session_id"]
                    for message in sections["messages"]
                    if message["session_id"]
                } | {
                    trip.get("session_id")
                    for trip in sections["trips"]
                    if trip.get("session_id")
                }
                if unique_sessions:
                    # 按会话而不是按消息查询（配置 Redis 时每条消息一次 EXISTS
                    # 意味着数十万次线程跳转与网络往返），且整批放在一次
                    # to_thread 里完成
                    deleted_sessions = await asyncio.to_thread(
                        lambda: {
                            session_id
                            for session_id in unique_sessions
                            if state.is_recently_deleted(user_id, session_id)
                        }
                    )
                else:
                    deleted_sessions = set()
                # 保留字会话（如 export）在 Web 端永远打不开：备份恢复时与墓碑
                # 同一处理，跳过其消息与元数据，避免导入一个无法查看的会话
                reserved_sessions = {
                    session_id
                    for session_id in unique_sessions
                    if session_id in _RESERVED_SESSION_IDS
                }
                skipped_sessions = deleted_sessions | reserved_sessions
                active_sessions = set(unique_sessions) - skipped_sessions
                for session_id in sorted(deleted_sessions):
                    errors.append(f"会话 {session_id} 最近被删除，已跳过其消息")
                for session_id in sorted(reserved_sessions):
                    errors.append(f"会话 {session_id} 使用了保留字，已跳过其消息")
                # 剔除消息 / 标题 / 置顶 / 归档并清空行程关联（与 CLI user-import
                # 共用同一实现，两条恢复路径不再有语义差）
                drop_sessions(sections, skipped_sessions)

                def _run_import() -> dict[str, Any]:
                    if not dry_run:
                        # 排队期间（单 worker 可能等数分钟）会话可能被其它
                        # 实例删除：快照与写库之间必须复查墓碑，避免把已删除
                        # 会话写回
                        recheck_sessions = {
                            message["session_id"]
                            for message in sections["messages"]
                            if message["session_id"]
                        } | {
                            trip.get("session_id")
                            for trip in sections["trips"]
                            if trip.get("session_id")
                        }
                        late_deleted = {
                            session_id
                            for session_id in recheck_sessions
                            if state.is_recently_deleted(user_id, session_id)
                        }
                        if late_deleted:
                            for session_id in sorted(late_deleted):
                                errors.append(
                                    f"会话 {session_id} 在恢复排队期间被删除，"
                                    "已跳过其消息"
                                )
                            drop_sessions(sections, late_deleted)
                    store = _open_long_term(user_id, strict=True)
                    try:
                        if dry_run:
                            return preview_user_import(store, sections)
                        counts = import_user_data(store, sections)
                        # 写库期间（大备份可能数十秒）其它实例 / CLI 可能删除
                        # 某个会话：最后再查一次墓碑并整会话回滚，避免迟到的
                        # 删除被恢复写回。若删除发生在本次检查之后，它自己的
                        # DELETE 会再次清掉已插入的行，不会被复活。
                        late_deleted = {
                            session_id
                            for session_id in active_sessions
                            if state.is_recently_deleted(user_id, session_id)
                        }
                        removed_total = 0
                        for session_id in sorted(late_deleted):
                            try:
                                removed_total += int(
                                    store.delete_chat_session(session_id) or 0
                                )
                            except Exception as exc:  # noqa: BLE001 - 尽力回滚
                                logger.warning(
                                    "回滚恢复期间被删除的会话 %s 失败：%s",
                                    session_id,
                                    exc,
                                )
                                continue
                            errors.append(
                                f"会话 {session_id} 在恢复写入期间被删除，"
                                f"已回滚 {removed_total} 条消息"
                            )
                        if removed_total:
                            counts["messages_added"] = max(
                                0,
                                int(counts.get("messages_added") or 0)
                                - removed_total,
                            )
                        return counts
                    finally:
                        _close_long_term(store)

                try:
                    counts = await loop.run_in_executor(state.import_executor, _run_import)
                except HTTPException:
                    # 存储打开失败的 503（_open_long_term strict）原样上抛，
                    # 不再被下面的通用分支改成「恢复失败」丢掉原因
                    raise
                except MemoryPersistenceError as exc:
                    logger.warning("恢复用户备份失败（%s）：%s", user_id, exc)
                    raise HTTPException(
                        status_code=503, detail="存储暂时不可用，请稍后重试"
                    ) from exc
                except Exception as exc:  # noqa: BLE001 - 写入失败统一可重试提示
                    # 恢复可能部分成功（按条目写入）：详情只写日志，避免把
                    # 底层存储异常文本暴露给客户端；重复导入可补齐剩余部分
                    logger.warning("恢复用户备份失败（%s）：%s", user_id, exc)
                    raise HTTPException(
                        status_code=503, detail="恢复失败，请稍后重试"
                    ) from exc
            return {"ok": True, "dry_run": dry_run, **counts, "skipped": errors}
        finally:
            state.import_queue_slots.release()

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
    parser.add_argument(
        "--version",
        action="version",
        version=f"Aligo Web {APP_VERSION}",
        help="显示版本号并退出",
    )
    args = parser.parse_args()

    configure_stdio()
    # uvicorn 只配置自己的日志：为应用日志（访问日志 / 智能体等）补一个根 handler。
    # LOG_FORMAT=json 时整行输出 JSON（time / level / logger + 访问日志字段），
    # 而不是在 JSON 前拼文本前缀。
    if access_log_format() == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    if not _is_loopback_host(args.host):
        logger.warning(
            "服务监听在 %s，可能对外可访问；请通过反向代理提供认证与 HTTPS，"
            "并按 SECURITY.md 配置 REDIS_URL / TRUST_PROXY_HEADERS",
            args.host,
        )
    try:
        application = create_app()
    except Exception as exc:  # noqa: BLE001 - 启动失败时给出友好提示
        print(f"启动失败：{exc}")
        print("请先配置 API Key（环境变量 LLM_API_KEY，或创建 config_local.py）")
        raise SystemExit(1) from exc

    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level="info",
        # SSE 心跳让长回合连接持续活跃：不给优雅退出设上限会让
        # Ctrl+C / SIGTERM 无限等待在途回合，被 SIGKILL 时 lifespan 的
        # close_all() 不会执行
        timeout_graceful_shutdown=5,
        # h11 默认 16 KB 的请求行上限放不下长中文提问的 expect 查询串
        # （4000 字 × 9 字节 percent-encoding），编辑 / 重发会被协议层拒绝
        h11_max_incomplete_event_size=64 * 1024,
        # 自带访问日志不记录查询串（避免把编辑原文等用户内容写入日志），
        # /api/ 请求由 RequestContextMiddleware 结构化记录
        access_log=False,
    )


if __name__ == "__main__":
    main()
