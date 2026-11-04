"""
进程内滑动窗口限流

用于 Web 对话接口的简单防滥用（按 客户端 + 用户、以及纯客户端 IP 两个维度）。
单机内存实现，不适用于多进程 / 多实例部署；生产环境可替换为 Redis 令牌桶。

key 由请求方提供（用户 ID 可被轮换），因此调用方应同时用客户端 IP 作为独立
限流维度，避免换一个用户 ID 就绕过限流。空 key 会定期清理，且 key 总数有上限，
防止大量随机 key 造成内存无限增长。
"""
import time
from collections import deque

# key 数量上限：超过后清理过期 key；仍超限时按最旧事件淘汰，保证内存有界。
MAX_KEYS = 10000


class RateLimiter:
    """滑动窗口限流器：窗口内最多允许 ``limit`` 次请求。"""

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
