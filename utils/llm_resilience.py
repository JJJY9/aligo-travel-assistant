"""
LLM 连接与可用性：重试退避、可重试错误判断、健康检查
"""
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .circuit_breaker import CircuitOpenError
from .errors import MemoryPersistenceError

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


def is_retriable_error(exc: BaseException) -> bool:
    """
    判断是否为可重试错误（网络/超时/限流/5xx）。
    """
    # 写盘失败不是网络故障：重试整轮对话只会重复调用大模型，不会让磁盘恢复
    if isinstance(exc, MemoryPersistenceError):
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


def is_permanent_llm_error(exc: BaseException) -> bool:
    """不可重试的模型调用失败（401/402/403/404 等 4xx）。

    这类错误意味着上游模型完全不可用（密钥失效 / 余额耗尽 / 模型不存在），
    不是「模型输出无法解析」：调用方必须上抛让熔断器记账，不能吞成默认
    意图 / 空结果后由外层把本轮记为成功——否则 LLM 彻底不可用时熔断器
    永远不会打开，健康检查一直报 closed。
    """
    if is_retriable_error(exc):
        return False
    status = _status_code(exc)
    return status is not None and 400 <= status < 500


async def retry_with_backoff(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    jitter: bool = True,
) -> T:
    """
    对异步调用进行重试，使用指数退避（可选抖动）。

    Args:
        coro_factory: 无参可调用，每次返回新的协程（避免重复使用已 consumed 的 generator）
        max_retries: 最大重试次数（不含首次），即最多调用 1 + max_retries 次
        base_delay_sec: 首次退避基数（秒）
        max_delay_sec: 退避上限（秒）
        jitter: 是否在退避时间上加随机抖动

    Returns:
        协程的返回值

    Raises:
        最后一次尝试的异常（若全部失败）
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
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
            logger.warning(
                "LLM call failed (attempt %d/%d), retry in %.1fs: %s",
                attempt + 1, max_retries + 1, delay, e,
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
        return False, str(e)
