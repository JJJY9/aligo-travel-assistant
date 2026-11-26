"""
LLM 连接与可用性：重试退避、可重试错误判断、健康检查
"""
import asyncio
import errno
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .access_log import MAX_LOG_TEXT, _safe_log_text
from .circuit_breaker import CircuitOpenError
from .errors import MemoryPersistenceError
from .log_redact import redact_secret

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 第三方 SDK 的连接类异常不是内置 OSError / ConnectionError 的子类，
# 且消息可能只有 "Connection error."，按类名识别（避免 import 可选依赖）
_RETRIABLE_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "RemoteProtocolError",
        "NetworkError",
    }
)


def _status_code(exc: BaseException) -> int | None:
    """尽量取出异常携带的 HTTP 状态码。"""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """读取上游 Retry-After（秒数或 HTTP 日期）；不可用时返回 None。

    429/503 的限流窗口常是 30–60 秒，按默认指数退避（1s/2s/4s）会在
    窗口内耗尽全部重试。解析必须全部容错：headers 不是映射、值非法、
    负数、时区缺失都不能让重试循环抛新异常。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - 非映射 headers 按无头处理
        raw = None
    if raw is None:
        try:
            for key, value in headers.items():
                if str(key).lower() == "retry-after":
                    raw = value
                    break
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt

        when = parsedate_to_datetime(text)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


# 本地存储类 OSError 的 errno（磁盘满 / 只读 / I/O 错误 / 配额 / 占名）：
# 与缺文件 / 权限 / 目录错误同口径，重试不会自愈，技能回复里的本地 I/O
# 失败也不能被当成上游 LLM 故障重跑整轮并计入全局熔断。errno 为 None 的
# 裸 OSError（socket 层 / 网关包装的网络错误）仍按可重试处理。
_LOCAL_IO_ERRNOS = frozenset(
    code
    for code in (
        errno.ENOSPC,
        getattr(errno, "EROFS", None),
        errno.EIO,
        getattr(errno, "EDQUOT", None),
        errno.EEXIST,
        errno.ENOTEMPTY,
        getattr(errno, "ENAMETOOLONG", None),
        getattr(errno, "ELOOP", None),
    )
    if code is not None
)

# 配额耗尽以 429 返回（OpenAI 兼容网关的常见形态）：状态码分支会把它当成
# 可重试的限流，重试只会反复失败。正文带这些标记时按永久故障处理，交给
# 熔断器记账。
_QUOTA_EXHAUSTED_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "billing_hard_limit_reached",
)


def is_retriable_error(exc: BaseException) -> bool:
    """
    判断是否为可重试错误（网络/超时/限流/5xx）。
    """
    # 写盘失败不是网络故障：重试整轮对话只会重复调用大模型，不会让磁盘恢复
    if isinstance(exc, MemoryPersistenceError):
        return False
    # 本地文件 / 模型加载类的 OSError（缺文件、权限、目录错误）重试不会
    # 自愈，还会把一轮编排连同其它智能体重复计费
    if isinstance(
        exc,
        (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError),
    ):
        return False
    # 其余本地存储类 errno（磁盘满 / 只读 / I/O / 配额）同口径不重试
    if isinstance(exc, OSError) and exc.errno in _LOCAL_IO_ERRNOS:
        return False
    text = str(exc).lower()
    if any(marker in text for marker in _QUOTA_EXHAUSTED_MARKERS):
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    if any(
        cls.__name__ in _RETRIABLE_EXCEPTION_NAMES for cls in type(exc).__mro__
    ):
        return True
    status = _status_code(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600
    msg = str(exc).lower()
    # 只把带 HTTP 标记的数字当作状态码：裸 "500" 会命中报错位置 / token 数，
    # 把「模型输出无法解析」这类永久错误误判为可重试
    if re.search(r"(?:status|code|http)\D{0,12}(?:429|5\d{2})\b", msg):
        return True
    if "rate limit" in msg or "too many requests" in msg:
        return True
    return "timeout" in msg or "timed out" in msg or "deadline exceeded" in msg


# 明确的「上游模型不可用」状态码：密钥失效 / 余额耗尽 / 模型不存在
_PERMANENT_LLM_STATUSES = frozenset({401, 402, 403, 404})
# 网关把鉴权错误包成 400 时正文会带这些关键词：同样按永久故障处理
_PERMANENT_LLM_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "authentication",
    "unauthorized",
    "insufficient_quota",
    "insufficient quota",
    "billing_hard_limit_reached",
    "model_not_found",
)


def is_permanent_llm_error(exc: BaseException) -> bool:
    """上游模型不可用的不可重试失败（401/402/403/404，或带鉴权标记的 4xx）。

    这类错误意味着模型服务整体不可用，调用方必须上抛让熔断器记账，不能
    吞成默认意图 / 空结果后由外层把本轮记为成功——否则 LLM 彻底不可用时
    熔断器永远不会打开。注意请求级 4xx（400 context_length_exceeded /
    408 / 409 / 413 / 422）**不在此列**：模型服务本身健康，任意用户连续
    超长请求不能打开全站熔断；它们按「确定性失败」处理（不重试、不记账）。
    """
    if is_retriable_error(exc):
        return False
    status = _status_code(exc)
    if status is None:
        return False
    if status in _PERMANENT_LLM_STATUSES:
        return True
    if 400 <= status < 500:
        text = str(exc).lower()
        return any(marker in text for marker in _PERMANENT_LLM_MARKERS)
    return False


async def retry_with_backoff(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    jitter: bool = True,
    deadline_monotonic: float | None = None,
) -> T:
    """
    对异步调用进行重试，使用指数退避（可选抖动）。

    Args:
        coro_factory: 无参可调用，每次返回新的协程（避免重复使用已 consumed 的 generator）
        max_retries: 最大重试次数（不含首次），即最多调用 1 + max_retries 次
        base_delay_sec: 首次退避基数（秒）
        max_delay_sec: 退避上限（秒）
        jitter: 是否在退避时间上加随机抖动
        deadline_monotonic: 可选的总预算截止时刻（``time.monotonic()``）。
            外层「整轮重试」用它约束失败放大的总时长：超过截止时刻不再发起
            新的尝试，直接抛出最后一次异常。内层单次调用的重试不传此参数。

    Returns:
        协程的返回值

    Raises:
        最后一次尝试的异常（若全部失败）
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            # 整轮重试预算耗尽：保留最后一次真实异常（而不是超时异常），
            # 让外层按原错误分类记账
            logger.warning(
                "整轮重试预算已耗尽（已尝试 %d 次），停止重试", attempt
            )
            if last_exc is not None:
                raise last_exc
            raise TimeoutError("重试预算已耗尽")
        try:
            return await coro_factory()
        except CircuitOpenError:
            raise
        except Exception as e:
            last_exc = e
            if attempt == max_retries or not is_retriable_error(e):
                raise
            delay = base_delay_sec * (2 ** attempt)
            if jitter:
                import random

                # 抖动在截断之前应用：先乘后截断保证实际退避不超过
                # max_delay_sec（此前截断后再乘最多到声明上限的 1.5 倍）
                delay = delay * (0.5 + random.random())
            delay = min(delay, max_delay_sec)
            # 上游明确指示等待时长时，取两者较大值（仍受 max_delay_sec
            # 约束）：限流窗口内按默认退避耗尽重试毫无意义
            retry_after = _retry_after_seconds(e)
            if retry_after is not None:
                delay = min(max(delay, retry_after), max_delay_sec)
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise
                # 退避睡眠同样受预算约束：睡完再判断会白白占用锁与额度
                delay = min(delay, remaining)
            logger.warning(
                "LLM call failed (attempt %d/%d), retry in %.1fs: %s",
                attempt + 1,
                max_retries + 1,
                delay,
                # 上游响应体常回显 API Key / 内部地址：日志与终端同口径脱敏；
                # 非 JSON 错误体（反代 502 HTML）还可能是多行，单行化避免
                # 伪造日志行，并截断防止超长刷屏
                _safe_log_text(redact_secret(str(e)))[:MAX_LOG_TEXT],
            )
            await asyncio.sleep(delay)
    if last_exc is None:
        raise RuntimeError("retry loop ended without an exception")
    raise last_exc


def _response_text(reply: Any) -> str:
    """尽量从 ChatResponse / dict / list / str 中取出文本。"""
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", item)))
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


async def run_health_check(
    base_url: str,
    api_key: str,
    model_name: str,
    timeout_sec: float = 10.0,
) -> tuple[bool, str]:
    """对 LLM 服务做一次最小化健康检查（发一条极简请求）。

    Returns:
        (success, message)
    """
    try:
        from config_agentscope import create_chat_model
    except ImportError:
        return False, "AgentScope not installed"

    try:
        model = create_chat_model(
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            timeout=timeout_sec,
            temperature=0,
            max_tokens=8,
            # 显式关闭 SDK 内建重试：timeout_sec 是本次健康检查的整体预算，
            # 任何重试都会让实际耗时超出预算（工厂默认已是 0，显式传入防止
            # 默认值回退）
            max_retries=0,
        )
        # 最小请求：必须返回非空文本才算可用。部分兼容网关以 HTTP 200
        # 返回错误对象，SDK 不抛异常、choices 为空，只看“未抛错”会误报健康
        reply = await asyncio.wait_for(
            model([{"role": "user", "content": "1"}]), timeout=timeout_sec
        )
        if not _response_text(reply).strip():
            return False, "模型返回空响应（请检查密钥 / 网关日志）"
        return True, "ok"
    except asyncio.TimeoutError:
        return False, f"健康检查超时（{timeout_sec:.1f}s）"
    except Exception as e:
        # 上游异常原文可能含完整 API Key（网关常见「Incorrect API key
        # provided: sk-…」）：回显给 /api/doctor、cli health 与日志前遮蔽
        return False, redact_secret(str(e))
