"""请求级幂等缓存（v2.0 持久化执行的第一步）。

用途：同一用户对同一会话用同一个幂等键重复提交时，直接返回上次完成的
响应，不再重复跑 Agent（也就不会重复写偏好 / 行程 / 聊天记录）。

实现边界：
- 进程内 TTL + 最大条目数（LRU），键为 ``(user_id, session_id, key)``；
- 只缓存成功完成的响应；失败不入缓存，允许客户端重试；
- 读出时深拷贝，调用方修改返回对象不会污染缓存；
- 多实例部署可把同一接口替换为 Redis 实现（键与 TTL 语义不变）。
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any

IdempotencyKey = tuple[str, str, str]


def make_key(user_id: str, session_id: str, idempotency_key: str) -> IdempotencyKey:
    """归一化幂等键：去空白、限制长度，空幂等键由调用方决定是否使用。"""
    return (
        str(user_id or "").strip(),
        str(session_id or "").strip(),
        str(idempotency_key or "").strip()[:200],
    )


class IdempotencyCache:
    """线程安全的 TTL + LRU 响应缓存。"""

    def __init__(self, max_entries: int = 256, ttl_sec: float = 600.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl_sec = max(0.0, float(ttl_sec))
        self._lock = threading.Lock()
        self._entries: OrderedDict[IdempotencyKey, tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )

    def get(self, key: IdempotencyKey) -> dict[str, Any] | None:
        """命中返回深拷贝；过期 / 不存在返回 None。"""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            created_at, payload = entry
            if self.ttl_sec and (now - created_at) > self.ttl_sec:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(payload)

    def put(self, key: IdempotencyKey, payload: dict[str, Any]) -> None:
        """写入完成响应；超上限时淘汰最久未使用的条目。"""
        with self._lock:
            self._entries[key] = (time.monotonic(), copy.deepcopy(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
