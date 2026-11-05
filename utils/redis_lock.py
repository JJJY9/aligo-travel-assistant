"""跨实例互斥锁（Redis SET NX + TTL），带进程内回退。

用于「同一时间只允许一个知识库评测」这类不希望并发执行的短任务：
- 配置 Redis 时，多个 Web 实例共享同一把锁（key 固定、TTL 防止进程崩溃后永久占用）；
- 未配置或 Redis 调用失败时回退到进程内 ``threading.Lock``；
- 释放时用 token 比对删除，避免 TTL 过期后误删其它实例重新获得的锁。
"""
import logging
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_LOCAL_TOKEN = "__local__"


class DistributedLock:
    """Redis 互斥锁；Redis 不可用时自动回退进程内锁。"""

    def __init__(
        self,
        client: Any,
        key: str,
        ttl_sec: float = 300.0,
    ) -> None:
        self._client = client
        self._key = key
        self._ttl_sec = max(1, int(ttl_sec))
        self._local = threading.Lock()

    def acquire(self) -> str | None:
        """尝试加锁；成功返回 token（释放时传回），已被占用返回 None。"""
        if self._client is None:
            return _LOCAL_TOKEN if self._local.acquire(blocking=False) else None
        token = uuid.uuid4().hex
        try:
            acquired = self._client.set(
                self._key, token, nx=True, ex=self._ttl_sec
            )
        except Exception as exc:  # noqa: BLE001 - 锁后端故障不能影响请求
            logger.warning("Redis 互斥锁不可用（%s），回退进程内锁", exc)
            self._client = None
            return _LOCAL_TOKEN if self._local.acquire(blocking=False) else None
        return token if acquired else None

    def release(self, token: str | None) -> None:
        """释放锁；token 不匹配（锁已过期并被他人获取）时不动它。"""
        if token is None:
            return
        if token == _LOCAL_TOKEN:
            self._local.release()
            return
        try:
            self._client.eval(_RELEASE_SCRIPT, 1, self._key, token)
        except Exception as exc:  # noqa: BLE001 - 释放失败时等待 TTL 自动过期
            logger.warning("释放 Redis 互斥锁失败：%s", exc)
