"""
进程内滑动窗口限流

用于 Web 对话接口的简单防滥用（按 客户端 + 用户 维度）。单机内存实现，
不适用于多进程 / 多实例部署；生产环境可替换为 Redis 令牌桶。
"""
import time
from collections import defaultdict, deque


class RateLimiter:
    """滑动窗口限流器：窗口内最多允许 ``limit`` 次请求。"""

    def __init__(self, limit: int, window_sec: float) -> None:
        self.limit = max(1, int(limit))
        self.window_sec = max(0.01, float(window_sec))
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        """记录一次请求。

        Returns:
            None 表示放行；超限时返回建议的 Retry-After 秒数（保留 1 位小数）。
        """
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] >= self.window_sec:
            events.popleft()

        if len(events) >= self.limit:
            return round(max(0.1, self.window_sec - (now - events[0])), 1)

        events.append(now)
        return None

    def reset(self) -> None:
        """清空所有限流状态（测试或运维需要时使用）。"""
        self._events.clear()
