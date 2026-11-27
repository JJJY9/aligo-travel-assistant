"""请求级幂等缓存（v2.0 持久化执行的第一步）。

用途：同一用户对同一会话用同一个幂等键重复提交时，直接返回上次完成的
响应，不再重复跑 Agent（也就不会重复写偏好 / 行程 / 聊天记录）。

实现边界：
- 进程内 TTL + 最大条目数；键为 ``(user_id, session_id, key)``；
- 超限淘汰按用户公平：只淘汰条目最多的用户自己最旧的一条，任一用户的
  流量都不能把另一用户仍在 TTL 内的条目（尤其断连回合的
  ``__incomplete__`` 标记）挤出缓存；
- 只缓存成功完成的响应；失败不入缓存，允许客户端重试；
- 读出时深拷贝，调用方修改返回对象不会污染缓存；
- 多实例部署可把同一接口替换为 Redis 实现（键与 TTL 语义不变）。
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

IdempotencyKey = tuple[str, str, str, str]


def make_key(
    user_id: str,
    session_id: str,
    idempotency_key: str,
    *,
    endpoint: str = "",
    message: str = "",
) -> IdempotencyKey:
    """归一化幂等键：去空白、限制长度，并绑定端点与请求正文。

    只按 (user, session, key) 复用会让客户端复用键但换了提问时静默回放
    上一问的答案：新消息既不执行也不落库。请求指纹用 SHA-256 前 32 位，
    不把完整长消息放进缓存键。
    """
    fingerprint = hashlib.sha256(
        f"{endpoint}\x00{message}".encode("utf-8")
    ).hexdigest()[:32]
    return (
        str(user_id or "").strip(),
        str(session_id or "").strip(),
        str(idempotency_key or "").strip()[:200],
        fingerprint,
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

    def _evict_fair_locked(self) -> None:
        """超上限时先清过期、再按用户公平淘汰（调用方已持有 ``self._lock``）。

        过期条目不会再被回放却仍占名额：直接按条目数选 victim 会把活跃
        用户仍在 TTL 内的新鲜条目（可能是断连回合的 ``__incomplete__``
        标记）挤出，公平性承诺在过期条目堆积时不成立。先弹一条已过期
        的最旧条目；调用方在仍超限时会再次进入，循环必然收敛。
        """
        if self.ttl_sec:
            now = time.monotonic()
            for key, (created_at, _) in self._entries.items():
                if now - created_at > self.ttl_sec:
                    self._entries.pop(key)
                    return
        counts: dict[str, int] = {}
        for key in self._entries:
            counts[key[0]] = counts.get(key[0], 0) + 1
        victim_user = max(counts, key=lambda user: counts[user])
        for key in self._entries:
            if key[0] == victim_user:
                self._entries.pop(key)
                return

    def put(self, key: IdempotencyKey, payload: dict[str, Any]) -> None:
        """写入完成响应；超上限时按用户公平淘汰最久未使用的条目。"""
        with self._lock:
            self._entries[key] = (time.monotonic(), copy.deepcopy(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._evict_fair_locked()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def discard_session(self, user_id: str, session_id: str) -> None:
        """清除某用户某会话的全部缓存条目（会话删除 / 删除最后一轮后调用）。

        键里的 session_id 覆盖带会话 id 的请求；首条消息（无 session_id）
        写在空串键下，载荷里带着服务端首次执行生成的会话 id，需一并清除。
        否则删除会话 / 删除最后一轮后用同键重试会回放已删除的答案，客户端
        以为已落库并继续使用一个已删除的会话。
        """
        uid = str(user_id or "").strip()
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            doomed = [
                key
                for key, (_, payload) in self._entries.items()
                if key[0] == uid
                and (
                    key[1] == sid
                    or str(payload.get("session_id") or "").strip() == sid
                )
            ]
            for key in doomed:
                self._entries.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
