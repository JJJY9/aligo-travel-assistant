"""
熔断器 (Circuit Breaker)
在连续失败后暂停调用 LLM，避免雪崩；一段时间后半开试探恢复。
"""
import logging
import asyncio
import threading
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常调用
    OPEN = "open"          # 拒绝调用，直接降级
    HALF_OPEN = "half_open"  # 试探性放行少量请求


class CircuitOpenError(Exception):
    """熔断器打开时抛出的异常，表示当前不执行 LLM 调用"""


def _owner_identity() -> object:
    """半开试探的归属标识。

    Web 端所有请求都跑在同一个事件循环线程里，只用线程 ident 认属主会让
    任意迟到请求被当成试探持有者；有运行中的 asyncio task 时用 task 对象
    本身（身份唯一、随请求隔离），否则退回线程 ident（CLI / 同步代码）。
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return task
    return ("thread", threading.get_ident())


class CircuitBreaker:
    """
    熔断器：连续失败 N 次后打开，拒绝一段时间后进入半开，半开成功 M 次后关闭。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 60.0,
        half_open_successes: int = 2,
    ):
        """
        Args:
            failure_threshold: 连续失败多少次后打开熔断
            recovery_timeout_sec: 打开状态持续多少秒后进入半开
            half_open_successes: 半开状态下连续成功多少次后关闭熔断
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self.half_open_successes = half_open_successes

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0
        self._last_failure_time: float | None = None
        self._opened_at: float | None = None
        # 半开状态下同一时刻只放行一个试探请求（避免恢复瞬间全部放量），
        # 记录发起时间以防调用被取消后试探名额永久占用
        self._half_open_probe_at: float | None = None
        # 试探名额租约：必须覆盖一次 LLM 调用（含 SDK 自带重试）的最长执行
        # 时间。复用 recovery_timeout_sec（默认 60s）时，慢试探仍在跑第二个
        # 请求就会覆盖归属，第一个的真实结果被静默丢弃（单飞失效、失败不
        # 能重开熔断）。取恢复期的 5 倍且不少于 300s；超租约才视为卡死回收。
        self._probe_lease_sec = max(recovery_timeout_sec * 5.0, 300.0)
        # 试探名额的持有线程：只有它能把结果计入半开 / 归还名额，
        # 熔断打开前放行的慢请求迟到返回时不能清掉别人的名额
        self._probe_owner: object | None = None
        # /metrics 等线程池端点会并发读取状态与迁移 OPEN→HALF_OPEN：
        # 所有状态读写都在锁内，避免清空别的线程刚占用的试探名额
        self._lock = threading.RLock()

    def _maybe_half_open(self) -> None:
        """OPEN 超过恢复期后迁移到 HALF_OPEN（调用方必须已持有本对象的锁）。"""
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.recovery_timeout_sec:
                logger.info("Circuit breaker: OPEN -> HALF_OPEN (recovery timeout)")
                self._state = CircuitState.HALF_OPEN
                self._half_open_success_count = 0
                self._half_open_probe_at = None
                self._probe_owner = None

    @property
    def state(self) -> CircuitState:
        """当前状态；可能从 OPEN 自动变为 HALF_OPEN"""
        with self._lock:
            self._maybe_half_open()
            return self._state

    def allow_call(self) -> bool:
        """
        是否允许本次调用。
        - CLOSED: 允许
        - OPEN: 不允许
        - HALF_OPEN: 单飞试探（同一时刻只放行一个请求）
        """
        with self._lock:
            self._maybe_half_open()
            s = self._state
            if s == CircuitState.CLOSED:
                return True
            if s == CircuitState.OPEN:
                return False
            now = time.monotonic()
            if (
                self._half_open_probe_at is not None
                and now - self._half_open_probe_at < self._probe_lease_sec
            ):
                return False
            self._half_open_probe_at = now
            self._probe_owner = _owner_identity()
            return True

    def record_success(self) -> None:
        """记录一次成功调用"""
        with self._lock:
            self._maybe_half_open()
            state = self._state
            if state == CircuitState.HALF_OPEN:
                # 只有持试探名额的调用能把成功计入半开：熔断打开前放行的
                # 慢请求迟到返回时不能清掉别人的名额、更不能凑够关闭计数
                # （两个迟到成功会在没有任何真实试探完成时关闭熔断器）
                if self._probe_owner != _owner_identity():
                    return
                self._half_open_probe_at = None
                self._probe_owner = None
                self._half_open_success_count += 1
                if self._half_open_success_count >= self.half_open_successes:
                    logger.info("Circuit breaker: HALF_OPEN -> CLOSED (recovered)")
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._opened_at = None
            elif state == CircuitState.CLOSED:
                self._failure_count = 0

    def release_probe(self) -> None:
        """归还半开试探名额：调用在真正发出 LLM 请求之前就失败或被取消时。

        半开状态只放行一个试探请求；若该请求被取消（用户点停止 / 断网）或
        在调用 LLM 前抛错，成功 / 失败都没记录，名额会一直占用到
        ``recovery_timeout_sec`` 之后，期间其它用户的请求全部被拒绝。
        归还后下一个请求可以立即试探；已记录成功 / 失败（名额已消费）时
        本方法不产生任何效果。
        """
        with self._lock:
            self._maybe_half_open()
            # 只有名额持有者能归还：任何未记录成功 / 失败的请求来清空的话，
            # 会把别的线程正在使用的名额释放掉（恢复期直接放量）
            if (
                self._state == CircuitState.HALF_OPEN
                and self._probe_owner == _owner_identity()
            ):
                self._half_open_probe_at = None
                self._probe_owner = None

    def record_failure(self) -> None:
        """记录一次失败调用"""
        with self._lock:
            self._last_failure_time = time.monotonic()
            self._maybe_half_open()
            state = self._state

            if state == CircuitState.HALF_OPEN:
                # 有其它调用持着试探名额时，熔断打开前放行的慢请求迟到失败
                # 不能把半开重开、清掉当前试探（否则真正试探的成功被丢弃、
                # 恢复再被推迟一个 recovery_timeout）；名额未占用 / 本调用
                # 就是持试探者时，失败仍按原语义重开熔断
                owner = self._probe_owner
                if owner is not None and owner != _owner_identity():
                    return
                logger.warning(
                    "Circuit breaker: HALF_OPEN -> OPEN (failure in half-open)"
                )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_probe_at = None
                self._probe_owner = None
                # 冻结在阈值：/metrics 的 HELP 文案承诺 open/half_open 期间
                # 失败数停在阈值，清零会让熔断期间的失败数误显示为 0
                self._failure_count = max(
                    self._failure_count, self.failure_threshold
                )
                return

            if state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    logger.warning(
                        "Circuit breaker: CLOSED -> OPEN "
                        "(failure_threshold=%d reached)",
                        self.failure_threshold,
                    )
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()

    def raise_if_open(self) -> None:
        """若熔断器打开则抛出 CircuitOpenError，供上层直接降级"""
        if not self.allow_call():
            raise CircuitOpenError("服务暂时不可用，请稍后再试")

    def get_status(self) -> dict:
        """当前状态摘要，便于日志或 status 命令"""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "last_failure_time": self._last_failure_time,
            "opened_at": self._opened_at,
        }
