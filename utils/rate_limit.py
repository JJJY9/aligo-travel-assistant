"""
滑动窗口限流

用于 Web 对话接口的简单防滥用（按 客户端 + 用户、以及纯客户端 IP 两个维度）。

默认使用进程内实现；配置 ``REDIS_URL`` 后由 :class:`RedisRateLimiter` 把计数
写入 Redis 有序集合，多实例共享同一份额度（窗口时间取自 Redis 服务器时钟）。
Redis 运行中不可用时自动回退进程内实现（60 秒后重试），限流故障不影响请求。

key 由请求方提供（用户 ID 可被轮换），因此调用方应同时用客户端 IP 作为独立
限流维度，避免换一个用户 ID 就绕过限流。空 key 会定期清理，且 key 总数有上限，
防止大量随机 key 造成内存无限增长。
"""
import logging
import time
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# key 数量上限：超过后清理过期 key；仍超限时按最旧事件淘汰，保证内存有界。
MAX_KEYS = 10000

# Redis 限流键前缀（与其它 aligo: 键一致），name 区分用户桶 / IP 桶。
REDIS_KEY_PREFIX = "aligo:ratelimit"
# Redis 故障后的回退冷却时间：期间直接用进程内限流，避免每个请求都等超时。
REDIS_FAILURE_COOLDOWN_SEC = 60.0

# 以下 Lua 脚本用 Redis 服务器时间（TIME），多实例时钟不一致也不会算错窗口。
# 返回值：放行 -1；触发限流时为 Retry-After 秒数字符串（Lua 数字返回会被
# Redis 截断为整数，因此小数秒用 tostring 保留精度）。

_PEEK_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  return '-1'
end
local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return tostring(tonumber(first[2]) + window - now)
"""

_CHECK_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return tostring(tonumber(first[2]) + window - now)
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
return '-1'
"""

_STATUS_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count == 0 then
  return {limit, '0'}
end
local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local reset = tonumber(first[2]) + window - now
if reset < 0 then
  reset = 0
end
return {limit - count, tostring(reset)}
"""


class RateLimiter:
    """滑动窗口限流器：窗口内最多允许 ``limit`` 次请求。"""

    backend = "memory"

    def __init__(self, limit: int, window_sec: float, max_keys: int = MAX_KEYS) -> None:
        self.limit = max(1, int(limit))
        self.window_sec = max(0.01, float(window_sec))
        self.max_keys = max(1, int(max_keys))
        self._events: dict[str, deque[float]] = {}

    def __len__(self) -> int:
        return len(self._events)

    def check(self, key: str) -> float | None:
        """记录一次请求。

        Returns:
            None 表示放行；超限时返回建议的 Retry-After 秒数（保留 1 位小数）。
        """
        now = time.monotonic()
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.max_keys:
                # 为新 key 预留一个位置，确保插入后总数不超过上限
                self._sweep(now, force=True, reserve=1)
            events = deque()
            self._events[key] = events

        while events and now - events[0] >= self.window_sec:
            events.popleft()

        if len(events) >= self.limit:
            return round(max(0.1, self.window_sec - (now - events[0])), 1)

        events.append(now)
        return None

    def peek(self, key: str) -> float | None:
        """不记录请求，返回现在发起请求会被限流的 Retry-After（否则 None）。

        多桶限流时先用 peek 检查所有桶，全部有额度再 check 记录，
        避免一个桶已经拒绝、另一个桶却仍被消耗配额。
        """
        now = time.monotonic()
        events = self._events.get(key)
        if events is None:
            return None
        while events and now - events[0] >= self.window_sec:
            events.popleft()
        if len(events) >= self.limit:
            return round(max(0.1, self.window_sec - (now - events[0])), 1)
        return None

    def status(self, key: str) -> tuple[int, float]:
        """返回 (剩余次数, 窗口重置剩余秒数)，不记录新请求。"""
        now = time.monotonic()
        events = self._events.get(key)
        if events is None:
            return self.limit, 0.0
        while events and now - events[0] >= self.window_sec:
            events.popleft()
        if not events:
            return self.limit, 0.0
        reset = max(0.0, self.window_sec - (now - events[0]))
        return max(0, self.limit - len(events)), reset

    def _sweep(self, now: float, force: bool = False, reserve: int = 0) -> None:
        """清理已过期 / 空 key；force 时若仍超上限，淘汰事件最旧的 key。

        ``reserve`` 为调用方即将新增的 key 数量，淘汰后保证还能放下。
        """
        for key in list(self._events):
            events = self._events[key]
            while events and now - events[0] >= self.window_sec:
                events.popleft()
            if not events:
                del self._events[key]

        overflow = len(self._events) - (self.max_keys - reserve)
        if force and overflow > 0:
            oldest = sorted(self._events, key=lambda k: self._events[k][0])[:overflow]
            for key in oldest:
                del self._events[key]

    def reset(self) -> None:
        """清空所有限流状态（测试或运维需要时使用）。"""
        self._events.clear()


class RedisRateLimiter:
    """基于 Redis 有序集合的滑动窗口限流（多实例共享计数）。

    接口与 :class:`RateLimiter` 一致。Redis 调用失败时回退到进程内实现并
    进入冷却（``REDIS_FAILURE_COOLDOWN_SEC``），冷却结束后重新尝试 Redis。
    ``name`` 用于区分不同的桶（如 ``user`` / ``ip``），同名实例共享计数。
    """

    backend = "redis"

    def __init__(self, client: Any, name: str, limit: int, window_sec: float) -> None:
        self._client = client
        self.name = name
        self.limit = max(1, int(limit))
        self.window_sec = max(0.01, float(window_sec))
        self._fallback = RateLimiter(limit=self.limit, window_sec=self.window_sec)
        self._degraded_until = 0.0

    def _key(self, key: str) -> str:
        return f"{REDIS_KEY_PREFIX}:{self.name}:{key}"

    def _eval(self, script: str, key: str, *args: Any) -> Any:
        """执行 Lua 脚本；Redis 不可用时返回 None 表示应使用回退实现。"""
        if time.monotonic() < self._degraded_until:
            return None
        try:
            return self._client.eval(script, 1, self._key(key), *args)
        except Exception as exc:  # noqa: BLE001 - 限流后端故障不能影响请求
            self._degraded_until = time.monotonic() + REDIS_FAILURE_COOLDOWN_SEC
            logger.warning(
                "Redis 限流不可用（%s），%d 秒内回退为进程内限流",
                exc,
                REDIS_FAILURE_COOLDOWN_SEC,
            )
            return None

    @staticmethod
    def _retry_after(result: Any) -> float | None:
        retry = float(result)
        if retry < 0:
            return None
        return round(max(0.1, retry), 1)

    def peek(self, key: str) -> float | None:
        """不记录请求，返回现在发起请求会被限流的 Retry-After（否则 None）。"""
        result = self._eval(_PEEK_SCRIPT, key, self.window_sec, self.limit)
        if result is None:
            return self._fallback.peek(key)
        return self._retry_after(result)

    def check(self, key: str) -> float | None:
        """记录一次请求；放行返回 None，超限返回建议的 Retry-After 秒数。"""
        result = self._eval(
            _CHECK_SCRIPT, key, self.window_sec, self.limit, uuid.uuid4().hex
        )
        if result is None:
            return self._fallback.check(key)
        return self._retry_after(result)

    def status(self, key: str) -> tuple[int, float]:
        """返回 (剩余次数, 窗口重置剩余秒数)，不记录新请求。"""
        result = self._eval(_STATUS_SCRIPT, key, self.window_sec, self.limit)
        if result is None:
            return self._fallback.status(key)
        remaining, reset = result
        return max(0, int(remaining)), max(0.0, float(reset))

    def reset(self) -> None:
        """清空该实例的进程内回退状态，并删除 Redis 中同名桶的全部键。"""
        self._fallback.reset()
        self._degraded_until = 0.0
        try:
            keys = list(
                self._client.scan_iter(
                    match=f"{REDIS_KEY_PREFIX}:{self.name}:*", count=500
                )
            )
            if keys:
                self._client.delete(*keys)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响服务
            logger.warning("清理 Redis 限流键失败：%s", exc)


def create_rate_limiter(
    name: str,
    limit: int,
    window_sec: float,
    redis_client: Any = None,
) -> RateLimiter | RedisRateLimiter:
    """优先创建 Redis 共享限流器；未提供客户端时使用进程内实现。"""
    if redis_client is not None:
        return RedisRateLimiter(redis_client, name, limit, window_sec)
    return RateLimiter(limit=limit, window_sec=window_sec)
