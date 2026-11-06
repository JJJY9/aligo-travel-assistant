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
import ipaddress
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
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
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.console import configure_stdio
from utils.doctor import run_doctor
from utils.knowledge_admin import (
    JOINED_MAX_CHARS,
    add_document,
    delete_document,
    export_documents,
    get_document_group,
    import_documents,
    list_categories,
    list_documents,
    search_documents,
    update_document,
)
from utils.knowledge_stats import knowledge_status
from utils.metrics import REQUEST_METRICS
from utils.rate_limit import create_rate_limiter
from utils.redis_lock import DistributedLock
from utils.stored_message import render_stored_message
from utils.session_markdown import render_all_sessions_markdown
from utils.skill_paths import resolve_skills_root, skill_scripts
from utils.trip_conflicts import trip_conflicts as _trip_conflicts
from utils.trip_conflicts import trip_interval as _trip_interval
from utils.trip_conflicts import trips_with_conflicts as _trips_with_conflicts
from utils.trip_export import render_trip_ics, render_trips_csv, render_trips_ics
from utils.turn_meta import extract_turn_meta
from utils.version import APP_VERSION
from utils.weather import WeatherError, fetch_weather

logger = logging.getLogger(__name__)

# SSE 心跳间隔（秒）：LLM 调用可能持续数十秒，定期发送注释行避免代理/浏览器超时
SSE_HEARTBEAT_SEC = 15.0
# 请求体大小上限（JSON 接口都是小请求；防止超大 body 在 Pydantic 校验前耗尽内存）
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024

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
                headers_list = list(message.get("headers") or [])
                # 用户数据接口不进入浏览器 / 代理缓存；响应自己声明了 Cache-Control
                # （如导出接口的 no-cache）时保持原样，避免重复头
                if path.startswith("/api/") and not any(
                    key.lower() == b"cache-control" for key, _ in headers_list
                ):
                    headers_list.append((b"cache-control", b"no-store"))
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
                if body_size > MAX_REQUEST_BODY_BYTES:
                    detail = (
                        f"请求体过大（上限 {MAX_REQUEST_BODY_BYTES // (1024 * 1024)} MB）"
                    )
                    await send_wrapper(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [
                                (b"content-type", b"application/json; charset=utf-8")
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
            if path.startswith("/api/"):
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
                REQUEST_METRICS.record(
                    scope.get("method", ""), metric_path, status_code, elapsed_ms
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
        payload = json.dumps(
            {"detail": f"请求体过大（上限 {self.max_bytes // (1024 * 1024)} MB）"},
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
                    "headers": [(b"content-type", b"application/json; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": payload})

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
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
        if scope.get("type") == "http" and scope.get("path") == "/api/chat/stream":
            await self.app(scope, receive, send)
            return
        await self.compressed_app(scope, receive, send)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="web_user", pattern=_ID_PATTERN)
    session_id: str | None = Field(default=None, pattern=_ID_PATTERN)

    @field_validator("session_id")
    @classmethod
    def _session_id_not_reserved(cls, value: str | None) -> str | None:
        # /api/sessions/export 是静态路由，名为 export 的会话永远打不开
        if value == "export":
            raise ValueError("session_id 不能使用保留字 export")
        return value

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        # min_length 在 strip 之前校验，"   " 会通过并浪费一次 LLM 调用
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


_ShortText = Annotated[str, Field(max_length=500)]
_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PreferenceValue = (
    _ShortText
    | Annotated[list[_ShortText], Field(max_length=50)]
    | int
    | _FiniteFloat
    | bool
)


_KnowledgeMetaKey = Annotated[str, Field(max_length=32)]
_KnowledgeMetaValue = Annotated[str, Field(max_length=200)]


class KnowledgeDocumentPayload(BaseModel):
    """新增知识片段（记忆面板维护入口）。"""

    content: str = Field(min_length=1, max_length=2000)
    # 限制键数 / 键值长度：metadata 原样写入向量库并在列表接口返回，
    # 不放任超大对象拖垮后续读取
    metadata: dict[_KnowledgeMetaKey, _KnowledgeMetaValue] = Field(
        default_factory=dict, max_length=10
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


class PreferencePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    value: PreferenceValue


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
        return cleaned

    @field_validator("origin", "purpose")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


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
        return cleaned

    @field_validator("origin", "purpose")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class SessionTitlePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(max_length=60)


class SessionPinPayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    pinned: bool


class SessionArchivePayload(BaseModel):
    user_id: str = Field(pattern=_ID_PATTERN)
    archived: bool


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
    # 用户锁表同样有上限：用户 ID 由请求方提供，未命中时淘汰未持有的锁
    max_locks = 1000
    # 已删除会话的短时墓碑：在途 / 重连的对话不能把刚删除的会话写回（复活）
    max_tombstones = 1000
    tombstone_ttl_sec = 300.0
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
        self._deleted_sessions: OrderedDict[str, float] = OrderedDict()
        # mark/unmark/is_recently_deleted 会被 asyncio.to_thread 在线程池里执行，
        # 不同 user_id 的请求互不串行：OrderedDict 的迭代 + 删除必须加锁
        self._tombstone_lock = threading.Lock()
        # 会话缓存同样会被线程池里的 drop_session 与事件循环里的 get_session
        # 并发修改（插入 / move_to_end / popitem），需要独立互斥
        self._sessions_lock = threading.Lock()
        # 锁表占满且全部在用时的兜底锁：让无法入表的新用户串行处理，
        # 保证锁表内存有界（不能为了收录新用户而丢弃正在持有的锁）
        self.overflow_lock = asyncio.Lock()
        # 配置 REDIS_URL 且可用时，两个桶都使用 Redis 共享计数，多实例共享额度；
        # 未配置或 Redis 不可用时回退进程内滑动窗口（单个实例内仍然有效）
        redis_client = get_shared_redis_client()
        self.rate_limiter = create_rate_limiter(
            "user",
            RATE_LIMIT_CONFIG.get("chat_requests", 30),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
        )
        # 用户 ID 由请求方提供、可以轮换；再按纯客户端 IP 限流，防止换 ID 绕过
        self.ip_limiter = create_rate_limiter(
            "ip",
            RATE_LIMIT_CONFIG.get("ip_requests", 120),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
        )
        # 知识库写入 / 导入 / 语义检索会触发嵌入模型推理，单独按客户端 IP 限流，
        # 不占用对话额度（Web 批量导入每批 200 条，60 次/分钟足够）
        self.kb_limiter = create_rate_limiter(
            "kb",
            RATE_LIMIT_CONFIG.get("kb_requests", 60),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
        )
        # 天气接口每次未命中会上游发两次请求；按客户端 IP 限流，
        # 保护 Open-Meteo 免费额度与共用线程池，不占用对话 / 知识库额度
        self.weather_limiter = create_rate_limiter(
            "weather",
            RATE_LIMIT_CONFIG.get("weather_requests", 30),
            RATE_LIMIT_CONFIG.get("window_sec", 60),
            redis_client,
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

    def get_session(self, user_id: str, session_id: str | None) -> _WebSession:
        if not session_id:
            session_id = uuid.uuid4().hex[:12]
        # 缓存键包含用户：同一 session_id 被不同用户使用时各有独立运行时，
        # 互不覆盖，也不会因为别人关闭同名会话而丢掉自己在进程内的短期记忆
        key = self._session_key(user_id, session_id)
        evicted: list[_WebSession] = []
        with self._sessions_lock:
            session = self.sessions.get(key)
            if session is None:
                # 每个会话独立的用量跟踪计数，避免并发会话之间的 token 增量互相污染
                session = _WebSession(
                    user_id, session_id, wrap_usage_tracking(self.model), self.breaker
                )
                self.sessions[key] = session
                while len(self.sessions) > self.max_sessions:
                    _, stale = self.sessions.popitem(last=False)
                    evicted.append(stale)
            else:
                self.sessions.move_to_end(key)
        # 关闭被淘汰的会话（可能做收尾 IO）放在锁外，避免拖慢其它请求
        for stale in evicted:
            stale.close()
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

    def mark_deleted(self, user_id: str, session_id: str) -> None:
        """记录短时墓碑，阻止在途对话把刚删除的会话写回。

        配置 Redis 时同时写入共享墓碑（TTL 与本地一致），其它实例的新请求
        也能看到该会话已删除；运行中 Redis 故障时本实例回退进程内墓碑，
        并在冷却后自动重试共享墓碑。
        """
        key = self._session_key(user_id, session_id)
        now = time.monotonic()
        with self._tombstone_lock:
            self._deleted_sessions[key] = now
            self._deleted_sessions.move_to_end(key)
            # 先清掉已过期的墓碑，避免仍有效的墓碑被容量上限淘汰
            expired = [
                candidate
                for candidate, at in self._deleted_sessions.items()
                if now - at > self.tombstone_ttl_sec
            ]
            for candidate in expired:
                del self._deleted_sessions[candidate]
            while len(self._deleted_sessions) > self.max_tombstones:
                self._deleted_sessions.popitem(last=False)
        client = self._shared_tombstones()
        if client is not None:
            try:
                client.setex(
                    self._deleted_key(user_id, session_id),
                    int(self.tombstone_ttl_sec),
                    "1",
                )
            except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
                self._tombstone_failed(exc, "写入")
            else:
                self._tombstones_shared = True
                self._tombstone_retry_at = 0.0

    def unmark_deleted(self, user_id: str, session_id: str) -> None:
        """撤销墓碑（删除失败时使用），避免把仍存在的会话误判为已删除。"""
        with self._tombstone_lock:
            self._deleted_sessions.pop(self._session_key(user_id, session_id), None)
        client = self._shared_tombstones()
        if client is None:
            return
        try:
            client.delete(self._deleted_key(user_id, session_id))
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
        with self._tombstone_lock:
            created = self._deleted_sessions.get(key)
            if created is not None:
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
                    # 自愈）。TTL 用剩余时长，不延长删除窗口
                    client.setex(
                        self._deleted_key(user_id, session_id),
                        max(1, int(remaining)),
                        "1",
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
        if lock is None:
            if len(self.locks) >= self.max_locks:
                for key, candidate in list(self.locks.items()):
                    if not candidate.locked():
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


def _rate_limit_info(state: AligoWebApp) -> dict[str, Any]:
    """限流后端与状态（健康检查与指标接口共用）。

    配置了 ``REDIS_URL`` 但限流器仍是进程内实现（启动时 Redis 不可用），
    同样视为降级；否则运维无法区分「未配置」与「配置了但回退了」。
    """
    backend = getattr(state.rate_limiter, "backend", "memory")
    configured = bool(STORAGE_CONFIG.get("redis_url"))
    # 各桶独立回退：任一桶处于降级冷却期都应上报，不能只看用户桶
    degradation_states = (
        getattr(state.rate_limiter, "degraded", False),
        getattr(state.ip_limiter, "degraded", False),
        getattr(state.kb_limiter, "degraded", False),
        getattr(state.weather_limiter, "degraded", False),
    )
    degraded = any(degradation_states) or (backend == "memory" and configured)
    return {
        "backend": backend,
        "configured": configured,
        "degraded": degraded,
        "user_limit": state.rate_limiter.limit,
        "ip_limit": state.ip_limiter.limit,
        "kb_limit": state.kb_limiter.limit,
        "weather_limit": state.weather_limiter.limit,
        "window_sec": state.rate_limiter.window_sec,
    }


def _enforce_rate_limit(
    state: AligoWebApp, user_id: str, request: Request
) -> dict[str, str]:
    """对对话接口做滑动窗口限流（按 客户端 + 用户，以及客户端 IP 两个维度）。

    Returns:
        供响应附带的额度头（X-RateLimit-Limit / Remaining / Reset）。
    """
    client = _client_host(request)
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
    # 超额，必须按它的结果拒绝，否则会放行超出额度的请求
    user_retry = state.rate_limiter.check(user_key)
    if user_retry is not None:
        raise _over_limit(state.rate_limiter.limit, user_retry)
    ip_retry = state.ip_limiter.check(client)
    if ip_retry is not None:
        raise _over_limit(state.ip_limiter.limit, ip_retry)
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


def _enforce_kb_rate_limit(state: AligoWebApp, request: Request) -> dict[str, str]:
    """知识库高开销接口的独立限流（按客户端 IP，与对话额度互不影响）。

    Returns:
        供响应附带的额度头（X-RateLimit-Limit / Remaining / Reset）。
    """
    client = _client_host(request)
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
    client = _client_host(request)
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


def _open_long_term(user_id: str) -> Any:
    """按当前配置打开长期记忆（JSON 或 PostgreSQL）。

    注意：PostgreSQL 连接失败时存储工厂会按既有降级策略回退到本地 JSON，
    不会走到这里的异常分支；这里兜住的是其余打开失败（文件损坏 / 依赖缺失
    等），统一映射为可重试的 503，避免所有调用点都抛裸 500。调用点仍负责
    关闭返回的 store。
    """
    try:
        return create_long_term_memory(user_id, str(MEMORY_DIR))
    except HTTPException:
        raise
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
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    return value


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
        raise HTTPException(status_code=503, detail=f"读取用户数据失败：{exc}") from exc


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
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"\*\*|__|~~|`", "", cleaned)
    return cleaned.strip()


def _match_snippet(text: str, needle: str, radius: int = 36) -> str:
    """截取命中位置附近的片段（用于会话搜索结果的预览）。"""
    cleaned = _strip_markdown_for_snippet(text)
    index = cleaned.lower().find(needle)
    if index < 0:
        # 关键字本身含被剥离的记号时回退到原文匹配
        cleaned = " ".join(str(text).split())
        index = cleaned.lower().find(needle)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(cleaned), index + len(needle) + radius)
    snippet = " ".join(cleaned[start:end].split())
    if start > 0:
        snippet = "…" + snippet
    if end < len(cleaned):
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
                # 会话内消息序号（0 起），供前端打开会话时定位到命中消息
                entry["match_index"] = entry["message_count"] - 1

    sessions = sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)
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
                parsed_timestamp = datetime.fromisoformat(timestamp)
                if parsed_timestamp.tzinfo is not None:
                    # PostgreSQL timestamptz 序列化为 UTC；统一转到本地时区再取日期
                    parsed_timestamp = parsed_timestamp.astimezone()
                message_day = parsed_timestamp.date().isoformat()
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

    preferences = {
        key: value
        for key, value in (data.get("preferences") or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }
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
                f"- {session['title']}（{session['message_count']} 条消息）{suffix}"
            )
    else:
        lines.append("- （暂无）")
    lines.append("")
    return "\n".join(lines)


def create_app(model: Any = None) -> FastAPI:
    """创建 FastAPI 应用（测试时可注入自定义模型）。"""
    state = AligoWebApp(model=model)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.close_all()

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
        # 校验错误的 input 可能包含 NaN（JSON 解析允许），直接返回会序列化失败
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_value(jsonable_encoder(exc.errors()))},
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        rate = _rate_limit_info(state)
        skills_root = resolve_skills_root()
        return {
            "status": "ok",
            "version": APP_VERSION,
            "model": LLM_CONFIG.get("model_name", ""),
            "has_api_key": bool(LLM_CONFIG.get("api_key")),
            "storage": describe_backends(),
            "rate_limit": rate["backend"],
            "rate_limit_configured": rate["configured"],
            "rate_limit_degraded": rate["degraded"],
            "skills": {
                "count": len(skill_scripts(skills_root)),
                "dir_found": skills_root.is_dir(),
            },
            "time": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/weather")
    async def weather(
        request: Request, city: str = Query(..., min_length=1, max_length=50)
    ) -> JSONResponse:
        """目的地天气（Open-Meteo，无 API Key；专用线程池，进程内缓存 10 分钟）。"""
        name = city.strip()
        if not name:
            raise HTTPException(status_code=422, detail="城市不能为空")
        headers = _enforce_weather_rate_limit(state, request)
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
        )
        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/doctor")
    async def doctor(network: bool = Query(default=False)) -> dict[str, Any]:
        """环境自检（默认不做网络请求；network=true 时检查 LLM 连通性）。"""
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
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=100),
        category: str = Query(default="", max_length=20),
    ) -> dict[str, Any]:
        """列出知识片段（记忆面板的知识库维护）。"""
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
    def knowledge_categories() -> dict[str, Any]:
        """知识片段分类统计（维护面板筛选下拉框）。"""
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
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            results = search_documents(q, top_k=top_k)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 检索失败统一提示
            logger.warning("语义检索知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"检索失败：{exc}") from exc
        return {"query": q, "results": results}

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
                result = evaluate_retrieval(
                    lambda query, top_k: agent.search_knowledge(
                        query, top_k=top_k, strict=True
                    )
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
            return update_document(doc_id, payload.content, payload.metadata)
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
    def knowledge_import(
        payload: KnowledgeImportPayload,
        request: Request,
        response: Response,
        dry_run: bool = Query(default=False),
    ) -> dict[str, Any]:
        """批量导入知识片段（来自导出的 JSON；单次最多 200 条）。

        ``dry_run=1`` 时只校验 / 去重并返回 ``would_import``，不写库。
        """
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return import_documents(payload.documents, dry_run=dry_run)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 导入失败统一提示
            logger.warning("导入知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"导入失败：{exc}") from exc

    @app.post("/api/knowledge/import/preview")
    def knowledge_import_preview(
        payload: KnowledgeImportPreviewPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        """预览导入（不写库）：整个备份一次校验 / 去重，返回将导入 / 跳过的数量。"""
        response.headers.update(_enforce_kb_rate_limit(state, request))
        try:
            return import_documents(payload.documents, dry_run=True)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail=f"{exc}（先运行初始化脚本）"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 预览失败统一提示
            logger.warning("预览导入知识片段失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"预览失败：{exc}") from exc

    @app.get("/api/status")
    def status(user_id: str = Query(default="web_user", pattern=_ID_PATTERN)) -> dict[str, Any]:
        data = _load_user_data(user_id)
        preferences = {
            key: value
            for key, value in data["preferences"].items()
            # 保留 0 / false 等合法值，仅过滤空值
            if value is not None and value != "" and value != [] and value != {}
        }
        today = datetime.now().date()
        active_trips: list[tuple[datetime, dict[str, Any]]] = []
        upcoming_trips: list[tuple[datetime, dict[str, Any]]] = []
        travel_days = 0
        travel_cities: set[str] = set()
        for trip in data["trip_history"]:
            interval = _trip_interval(trip)
            if interval is None:
                continue
            start_dt, end_dt = interval
            travel_days += (end_dt.date() - start_dt.date()).days + 1
            destination = str(trip.get("destination") or "").strip()
            if destination:
                travel_cities.add(destination)
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
            "travel_summary": {
                "total_trips": len(data["trip_history"]),
                "total_days": travel_days,
                "cities": len(travel_cities),
                "active": len(active_trips),
                "upcoming": len(upcoming_trips),
            },
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
        store = _open_long_term(user_id)
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
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=100),
        archived: bool = Query(default=False),
    ) -> dict[str, Any]:
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
    def export_sessions(user_id: str = Query(..., pattern=_ID_PATTERN)) -> Response:
        """导出全部会话为单个 Markdown 文件（按最近更新排序，含归档会话）。

        必须声明在 ``/api/sessions/{session_id}`` 之前，否则会被动态路由捕获。
        """
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
        markdown, count = render_all_sessions_markdown(user_id, messages, titles)
        filename = f"aligo_sessions_{_safe_filename(user_id, 'user')}.md"
        logger.info("Exported %s sessions for %s", count, user_id)
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/sessions/{session_id}")
    def get_session_messages(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
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
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """把一个会话导出为 Markdown（便于保存或分享行程）。"""
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

        title = str(titles.get(session_id) or "")
        if not title:
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
    async def delete_session(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """删除会话的全部消息与元数据；等待进行中的对话结束后再删除，避免写回。"""
        async with state.lock_for(user_id):
            def _exists() -> bool:
                store = _open_long_term(user_id)
                try:
                    return bool(store.get_chat_history(session_id=session_id))
                finally:
                    _close_long_term(store)

            # 先落墓碑再查存在性：从请求开始其它在途对话就会被拒绝，
            # 不会在存在性检查期间把已删除的会话写回。重复删除（之前已有
            # 墓碑）也要重新写一次：上一次的共享墓碑可能因 Redis 故障没写
            # 成功，这里顺带修复；从未存在的 id 在确认后撤销本地墓碑。
            already_marked = await asyncio.to_thread(
                state.is_recently_deleted, user_id, session_id
            )
            await asyncio.to_thread(state.mark_deleted, user_id, session_id)

            try:
                existed = await asyncio.to_thread(_exists)
            except Exception as exc:  # noqa: BLE001 - 读失败统一提示
                if not already_marked and not state.shared_tombstones_active():
                    # 进程内墓碑可以撤销（允许该 id 继续使用）；共享墓碑可能
                    # 被其它实例的同一删除依赖，读失败时保守保留
                    await asyncio.to_thread(
                        state.unmark_deleted, user_id, session_id
                    )
                logger.warning("检查会话是否存在失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc

            def _delete() -> int:
                store = _open_long_term(user_id)
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
                            state.unmark_deleted, user_id, session_id
                        )
                    elif (
                        still_exists is False
                        and not existed
                        and not state.shared_tombstones_active()
                    ):
                        # 读到了「确实不存在」且删除前也不存在（从未存在的 id）：
                        # 撤销进程内墓碑，允许该 id 新开对话
                        await asyncio.to_thread(
                            state.unmark_deleted, user_id, session_id
                        )
                logger.warning("删除会话失败: %s", exc)
                raise HTTPException(status_code=503, detail=f"删除失败：{exc}") from exc
            if (
                removed == 0
                and not existed
                and not already_marked
                and not state.shared_tombstones_active()
            ):
                # 从未存在的 id：只有进程内墓碑时才撤销，允许用该 id 新开
                # 对话；共享墓碑可能属于其它实例的同一次删除，不能代为删除
                await asyncio.to_thread(state.unmark_deleted, user_id, session_id)
            await asyncio.to_thread(state.drop_session, user_id, session_id)
            await asyncio.to_thread(clear_short_term_session, session_id, user_id)
        return {"session_id": session_id, "deleted": removed}

    @app.delete("/api/sessions/{session_id}/last-turn")
    async def delete_last_turn(
        session_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
        expect: str | None = Query(default=None, max_length=6000),
    ) -> dict[str, Any]:
        """删除会话最后一轮对话（供「编辑上一条提问」后重新发送使用）。

        ``expect`` 为待编辑提问的原文：与最后一轮用户提问不一致时不删除任何
        内容（例如这条提问在意图识别阶段就失败、并未写入历史），避免误删上一轮。
        """
        async with state.lock_for(user_id):
            def _remove_last_turn() -> int:
                store = _open_long_term(user_id)
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

    @app.post("/api/chat")
    async def chat(
        request: Request, payload: ChatRequest, response: Response
    ) -> dict[str, Any]:
        rate_headers = _enforce_rate_limit(state, payload.user_id, request)
        async with state.lock_for(payload.user_id):
            if await asyncio.to_thread(
                state.is_recently_deleted, payload.user_id, payload.session_id
            ):
                raise HTTPException(
                    status_code=404, detail="会话已删除，请开始新对话"
                )
            # 构造会话会同步连接 Redis / PostgreSQL（最长数秒），放到线程池
            session = await asyncio.to_thread(
                state.get_session, payload.user_id, payload.session_id
            )
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
        response.headers.update(rate_headers)
        return {"session_id": session.session_id, **result}

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
        """SSE 流式对话：先推送执行进度事件，最后推送完整结果或错误。"""
        rate_headers = _enforce_rate_limit(state, payload.user_id, request)
        rid = _request_id(request)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def event_stream() -> Any:
            # 在推送第一个事件前拿到用户锁：删除会话会等待本轮结束。
            # 若像以前那样在 get_session 之后才加锁，流已返回、run 尚未开始时
            # 到达的删除会让本轮结束时把已删除的会话重新写回。
            lock = state.lock_for(payload.user_id)
            await lock.acquire()
            task: asyncio.Task[None] | None = None
            heartbeat: asyncio.Task[None] | None = None
            try:
                if await asyncio.to_thread(
                    state.is_recently_deleted, payload.user_id, payload.session_id
                ):
                    yield (
                        "data: "
                        + json.dumps(
                            {"type": "error", "message": "会话已删除，请开始新对话"},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    return
                session = await asyncio.to_thread(
                    state.get_session, payload.user_id, payload.session_id
                )

                async def run() -> None:
                    try:
                        result = await session.runtime.ask(
                            payload.message, on_event=queue.put_nowait
                        )
                        await queue.put(
                            {"type": "done", "session_id": session.session_id, **result}
                        )
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
                            {
                                "type": "error",
                                "message": f"处理失败：{exc}（请求 ID：{rid}）",
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
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event["type"] in ("done", "error"):
                        break
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                lock.release()

        return StreamingResponse(
            event_stream(),
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
        user_id: str = Query(..., pattern=_ID_PATTERN),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        """列出历史行程（默认记录顺序，支持分页；附带日期重叠信息）。

        ``order=desc`` 返回从最新记录开始的顺序，供记忆面板展示「最近 N 条」。
        """
        store = _open_long_term(user_id)
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("列出历史行程失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"读取行程失败：{exc}") from exc
        finally:
            _close_long_term(store)
        total = len(trips)
        ordered = trips[::-1] if order == "desc" else trips
        page = ordered[offset : offset + limit]
        return {
            "trips": _trips_with_conflicts(page, trips),
            "total": total,
            "offset": offset,
            "limit": limit,
            "order": order,
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
            conflicts = _trip_conflicts(trips, start, end or start, exclude_id=trip_id)
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

            start = end = None
            try:
                if merged.get("start_date"):
                    start = datetime.strptime(str(merged["start_date"]), "%Y-%m-%d")
                if merged.get("end_date"):
                    end = datetime.strptime(str(merged["end_date"]), "%Y-%m-%d")
            except ValueError:
                start = end = None
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
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出全部历史行程为一个 iCalendar 文件。"""
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
            headers={"Content-Disposition": 'attachment; filename="aligo_trips.ics"'},
        )

    @app.get("/api/trips/export")
    def export_trips_csv(
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出全部历史行程为 CSV（UTF-8 BOM，便于 Excel 直接打开）。"""
        store = _open_long_term(user_id)
        try:
            trips = store.get_trip_history(limit=None)
        except Exception as exc:  # noqa: BLE001 - 读取失败统一提示
            logger.warning("导出行程 CSV 失败: %s", exc)
            raise HTTPException(status_code=503, detail=f"导出失败：{exc}") from exc
        finally:
            _close_long_term(store)
        return Response(
            content=render_trips_csv(trips),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="aligo_trips.csv"'},
        )

    @app.get("/api/trips/{trip_id}/calendar")
    def export_trip_calendar(
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> Response:
        """导出一条行程为 iCalendar（.ics）全天事件。"""
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
                )
            },
        )

    @app.delete("/api/trips/{trip_id}")
    def delete_trip(
        trip_id: str = ApiPath(..., pattern=_ID_PATTERN),
        user_id: str = Query(..., pattern=_ID_PATTERN),
    ) -> dict[str, Any]:
        """删除一条历史行程（清理误记录）。"""
        store = _open_long_term(user_id)
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
        # h11 默认 16 KB 的请求行上限放不下长中文提问的 expect 查询串
        # （4000 字 × 9 字节 percent-encoding），编辑 / 重发会被协议层拒绝
        h11_max_incomplete_event_size=64 * 1024,
        # 自带访问日志不记录查询串（避免把编辑原文等用户内容写入日志），
        # /api/ 请求由 RequestContextMiddleware 结构化记录
        access_log=False,
    )


if __name__ == "__main__":
    main()
