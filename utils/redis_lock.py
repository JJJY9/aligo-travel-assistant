"""跨实例互斥锁（Redis SET NX + TTL），带进程内互斥兜底。

用于「同一时间只允许一个知识库评测」这类不希望并发执行的短任务：
- 进程内先互斥（``threading.Lock``），再尝试 Redis：即使 Redis 抖动回退，
  也不会与同进程里已持有 Redis 锁的请求并发；
- 配置 Redis 时多个 Web 实例共享同一把锁，TTL 防止进程崩溃后永久占用；
  持有期间按 TTL/3 续期，模型下载等长任务不会中途失去锁；
- Redis 调用失败时只回退本次加锁，保留客户端并在下次 acquire 重试，
  不会因为一次异常永久禁用共享锁；
- 释放时用 token 比对删除，避免误删其它实例重新获得的锁；SET 响应超时
  但可能已在服务端生效时保留 token，释放阶段尽力清理。
"""
import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_LOCAL_TOKEN = "__local__"


class DistributedLock:
    """Redis 互斥锁；Redis 不可用时自动回退进程内锁（本次加锁）。"""

    def __init__(
        self,
        client: Any,
        key: str,
        ttl_sec: float = 300.0,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._key = key
        self._ttl_sec = max(1, int(ttl_sec))
        self._local = threading.Lock()
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_lock = threading.Lock()
        # 最近一次 acquire 是否因 Redis 不可用回退到进程内锁
        self.degraded = False
        # 持有期间是否在续期时发现锁已失效（过期后被其它实例拿走）：
        # 此时互斥已不成立，长任务不能再假定自己是唯一执行者
        self.lost = False

    def _get_client(self) -> Any:
        """返回 Redis 客户端；启动时不可用则通过工厂重试获取。"""
        if self._client is None and self._client_factory is not None:
            try:
                self._client = self._client_factory()
            except Exception as exc:  # noqa: BLE001 - 工厂自身也可能失败
                logger.warning("获取 Redis 互斥锁客户端失败：%s", exc)
                self._client = None
        return self._client

    def acquire(self) -> str | None:
        """尝试加锁；成功返回 token（释放时传回），已被占用返回 None。"""
        # 先拿进程内锁：本进程任一时刻最多一个持有者，再谈跨实例
        if not self._local.acquire(blocking=False):
            return None
        # 每次取得本地互斥后重置上一次持有的 lost：Redis 持续故障 / 回退
        # 路径不会清零它，残留值会把没拿到共享锁的调用误标为「锁失效」
        self.lost = False
        client = self._get_client()
        if client is None:
            self.degraded = True
            return _LOCAL_TOKEN
        token = uuid.uuid4().hex
        try:
            acquired = client.set(self._key, token, nx=True, ex=self._ttl_sec)
        except Exception as exc:  # noqa: BLE001 - 锁后端故障不能影响请求
            # SET 可能已在服务端生效（响应超时），保留 token 让 release 尽力清理；
            # 保留客户端，下次 acquire 会再次尝试 Redis
            logger.warning("Redis 互斥锁不可用（%s），回退进程内锁", exc)
            self.degraded = True
            return token
        if not acquired:
            self._local.release()
            self.degraded = False
            return None
        self.degraded = False
        self._start_heartbeat(token)
        return token

    def release(self, token: str | None) -> None:
        """释放锁；token 不匹配（锁已过期并被他人获取）时不动 Redis 键。"""
        if token is None:
            return
        if token == _LOCAL_TOKEN:
            self._local.release()
            return
        self._stop_heartbeat()
        client = self._get_client()
        if client is not None:
            try:
                client.eval(_RELEASE_SCRIPT, 1, self._key, token)
            except Exception as exc:  # noqa: BLE001 - 释放失败时等待 TTL 自动过期
                logger.warning("释放 Redis 互斥锁失败：%s", exc)
        # Redis 释放失败也要放开进程内锁，否则本进程会一直卡住
        if self._local.locked():
            self._local.release()

    def _start_heartbeat(self, token: str) -> None:
        """持有期间按 TTL/3 续期，避免长任务在 TTL 到期后失去锁。"""
        interval = max(1.0, self._ttl_sec / 3)
        stop = threading.Event()

        def _renew() -> None:
            last_ok = time.monotonic()

            def _mark_lost_if_expired() -> bool:
                """连续续期失败达到一个 TTL：键可能已过期被其它实例取得。

                Redis 不可达时无法用「续期返回 0」发现锁已易主，只能按时间
                保守判断：最后一次成功续期后已过整个 TTL，键必然已过期。
                """
                if stop.is_set() or time.monotonic() - last_ok < self._ttl_sec:
                    return False
                self.lost = True
                logger.warning(
                    "Redis 互斥锁连续续期失败超过 TTL（%s），互斥保护可能已丢失",
                    self._key,
                )
                stop.set()
                return True

            while not stop.wait(interval):
                client = self._get_client()
                if client is None:
                    if _mark_lost_if_expired():
                        return
                    continue
                try:
                    renewed = client.eval(
                        _RENEW_SCRIPT, 1, self._key, token, self._ttl_sec
                    )
                except Exception as exc:  # noqa: BLE001 - 续期失败下次再试
                    logger.warning("续期 Redis 互斥锁失败：%s", exc)
                    if _mark_lost_if_expired():
                        return
                    continue
                # EXPIRE 返回 0：键已过期（被其它实例重新获取）或 token 已不匹配。
                # 此时本进程已不是唯一持有者，继续执行会与另一个实例并发。
                if not renewed:
                    if stop.is_set():
                        # 释放 / 重新加锁后才返回的旧续期：不能把新的持有点
                        # 标记为已丢失
                        return
                    self.lost = True
                    logger.warning(
                        "Redis 互斥锁在持有期间失效（%s），互斥保护已丢失", self._key
                    )
                    stop.set()
                    return
                last_ok = time.monotonic()

        thread = threading.Thread(
            target=_renew, name=f"aligo-lock-{self._key}", daemon=True
        )
        with self._heartbeat_lock:
            self._heartbeat_stop = stop
            self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        with self._heartbeat_lock:
            stop, thread = self._heartbeat_stop, self._heartbeat_thread
            self._heartbeat_stop = None
            self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
