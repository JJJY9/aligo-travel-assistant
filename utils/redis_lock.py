"""跨实例互斥锁（Redis SET NX + TTL），带进程内互斥兜底。

用于「同一时间只允许一个知识库评测」这类不希望并发执行的短任务：
- 进程内先互斥（``threading.Lock``），再尝试 Redis：即使 Redis 抖动回退，
  也不会与同进程里已持有 Redis 锁的请求并发；
- 配置 Redis 时多个 Web 实例共享同一把锁，TTL 防止进程崩溃后永久占用；
  持有期间按 TTL/3 续期，模型下载等长任务不会中途失去锁；
- Redis 调用失败时只回退本次加锁，保留客户端并在下次 acquire 重试，
  不会因为一次异常永久禁用共享锁；
- 释放时用 token 比对删除，避免误删其它实例重新获得的锁；SET 响应超时
  但可能已在服务端生效时保留 token，释放阶段尽力清理；释放失败时记住
  token，下次加锁被自己的残留键挡住时先按 token 校验删除再重试；
- :meth:`DistributedLock.verify_owned` 供长任务收尾前只读复核归属，
  补上「心跳间隔内锁已失效」的检测空窗。
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

# 只读归属复核：不刷新 TTL，只回答「键现在是否仍归本 token」
_VERIFY_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return 1
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
        # 本实例当前持有的 token（acquire 设置、release 清除）：收尾复核用它
        # 区分「自己的旧 token」与「重新加锁后的新持有点」
        self._held_token: str | None = None
        # 释放失败（CAS DEL 抛异常）时保留的 token：下次 acquire 被自己的
        # 残留键挡住时先按 token 校验删除再重试一次 SET NX，避免本实例在
        # 键 TTL（最长约 5 分钟）内一直误报「已有任务在运行」
        self._stale_release_token: str | None = None

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
            self._held_token = _LOCAL_TOKEN
            return _LOCAL_TOKEN
        token = uuid.uuid4().hex
        try:
            acquired = client.set(self._key, token, nx=True, ex=self._ttl_sec)
        except Exception as exc:  # noqa: BLE001 - 锁后端故障不能影响请求
            # SET 可能已在服务端生效（响应超时），保留 token 让 release 尽力清理；
            # 保留客户端，下次 acquire 会再次尝试 Redis
            logger.warning("Redis 互斥锁不可用（%s），回退进程内锁", exc)
            # SET 可能已在服务端生效：启动心跳（续期脚本按 token 校验归属，
            # 未真正持有键时不会续期别人的锁），否则长任务超过 TTL 后共享
            # 锁静默过期且 lost 永远为 False
            return self._degraded_hold(token)
        if not acquired and self._stale_release_token is not None:
            # 上一次 release 的 CAS DEL 失败，键仍以本实例旧 token 残留：
            # 只删除自己的键（token 比对），成功后再试一次 SET NX
            stale = self._stale_release_token
            try:
                cleaned = client.eval(_RELEASE_SCRIPT, 1, self._key, stale)
            except Exception as exc:  # noqa: BLE001 - 清理失败等 TTL 自愈
                logger.warning("清理残留 Redis 互斥锁失败：%s", exc)
                cleaned = None
            if cleaned is not None:
                # 0 表示键已不存在或已被其它实例持有：都不再需要本实例清理
                self._stale_release_token = None
                if cleaned:
                    try:
                        acquired = client.set(
                            self._key, token, nx=True, ex=self._ttl_sec
                        )
                    except Exception as exc:  # noqa: BLE001 - 同 SET 超时口径
                        logger.warning(
                            "清理残留锁后重新加锁失败（%s），按降级处理", exc
                        )
                        return self._degraded_hold(token)
        if not acquired:
            self._local.release()
            self.degraded = False
            return None
        self._stale_release_token = None
        self.degraded = False
        self._held_token = token
        try:
            self._start_heartbeat(token)
        except BaseException:
            # 同上：start() 抛异常时回滚，否则本地锁永久被占，后续 acquire
            # 全部返回 None（评测端点恒定「已有评测在运行」直到重启）
            self.release(token)
            raise
        return token

    def _degraded_hold(self, token: str) -> str:
        """SET 结果不确定（异常 / 清理后重试仍超时）时按「可能已持锁」继续。"""
        self.degraded = True
        self._held_token = token
        try:
            self._start_heartbeat(token)
        except BaseException:
            # 心跳线程起不来（线程耗尽 / 解释器收尾）时不能让进程内锁
            # 处于「已持有但调用方拿不到 token」的半状态：复用 release
            # 归还本地锁并按 token 尽力清理可能已生效的键
            self.release(token)
            raise
        return token

    def release(self, token: str | None) -> None:
        """释放锁；token 不匹配（锁已过期并被他人获取）时不动 Redis 键。"""
        if token is None:
            return
        if self._held_token == token:
            self._held_token = None
        if token == _LOCAL_TOKEN:
            self._local.release()
            return
        self._stop_heartbeat()
        client = self._get_client()
        if client is not None:
            try:
                client.eval(_RELEASE_SCRIPT, 1, self._key, token)
                if self._stale_release_token == token:
                    self._stale_release_token = None
            except Exception as exc:  # noqa: BLE001 - 释放失败时等待 TTL 自动过期
                logger.warning(
                    "释放 Redis 互斥锁失败（键将在 TTL 后过期）：%s", exc
                )
                # 记住 token：残留键属于本实例，下次 acquire 被它挡住时
                # 可按 token 校验删除后重试，不必等 TTL 自然过期
                self._stale_release_token = token
        # Redis 释放失败也要放开进程内锁，否则本进程会一直卡住
        if self._local.locked():
            self._local.release()

    def verify_owned(self, token: str | None) -> bool | None:
        """只读复核 token 是否仍持有共享锁（不刷新 TTL，不改变键状态）。

        用于长任务收尾前检测「锁在运行期间已失效」（如主从切换丢失键）：
        心跳只在下一次续期时才发现，短任务可能整段窗口都漏检。

        Returns:
            True 仍持有；False 键已删除 / 被其它实例取得（并置 ``lost``）；
            None 本地锁或 Redis 不可达，无法判断（维持现有 lost 状态）。
        """
        if token is None:
            return None
        if token == _LOCAL_TOKEN:
            return True
        client = self._get_client()
        if client is None:
            return None
        try:
            owned = client.eval(_VERIFY_SCRIPT, 1, self._key, token)
        except Exception as exc:  # noqa: BLE001 - 复核失败不改变既有标记
            logger.warning("校验 Redis 互斥锁归属失败：%s", exc)
            return None
        if owned:
            return True
        if self._held_token == token:
            self.lost = True
            logger.warning(
                "Redis 互斥锁在收尾复核时已失效（%s），互斥保护已丢失",
                self._key,
            )
        return False

    def _start_heartbeat(self, token: str) -> None:
        """持有期间按 TTL/3 续期，避免长任务在 TTL 到期后失去锁。"""
        # 续期间隔取 TTL/3：不能用 1 秒下限——TTL ≤ 3 秒时首次续期会落在
        # 过期之后（短 TTL 的锁会静默丢失）
        interval = max(0.1, self._ttl_sec / 3)
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
                if (
                    self.degraded
                    and not stop.is_set()
                    and self._heartbeat_stop is stop
                ):
                    # 续期脚本按 token 比对返回 1：本实例确实持有共享键。
                    # 「SET 响应超时但已生效」的降级判定在此解除，避免评测
                    # 结果整轮被误标为可能并发；旧续期（已被新持有取代）
                    # 不得清掉新持有期 acquire 置位的 degraded
                    self.degraded = False

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
        if (
            thread is not None
            and thread.ident is not None
            and thread is not threading.current_thread()
        ):
            # ident 为 None 表示 start() 抛异常后的半状态：join 未启动的
            # 线程会再抛 RuntimeError，掩盖 acquire 的回滚异常
            thread.join(timeout=2.0)
