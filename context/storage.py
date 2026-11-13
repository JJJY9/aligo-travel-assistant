"""
存储后端工厂

按配置选择记忆后端（未配置或连接失败时自动回退，保证本地开箱即用）：

- 短期记忆：配置 ``REDIS_URL`` → RedisShortTermMemory；否则进程内 ShortTermMemory
- 长期记忆：配置 ``DATABASE_URL`` → PostgresLongTermMemory；否则 JSON 文件 LongTermMemory

连接失败会记录一次警告并进入 60 秒冷却，避免每个会话都等待连接超时。
"""
import logging
import re
import threading
import time
from typing import Any

from config import STORAGE_CONFIG
from context.long_term_memory import LongTermMemory
from context.postgres_long_term_memory import PostgresLongTermMemory
from context.short_term_memory import (
    RedisShortTermMemory,
    ShortTermMemory,
    applied_key,
    discard_pending_ops,
    note_redis_operation_failure,
    queue_clear,
    redis_short_term_available,
    redis_short_term_degraded,
    reset_redis_degraded,
    session_key,
)
from utils.errors import MemoryPersistenceError
from utils.log_redact import redact_secret

logger = logging.getLogger(__name__)

def _redact_url(url: str) -> str:
    """隐藏 Redis URL 中的密码，避免日志泄露。

    用户信息段可能在密码中包含未转义的 ``@``，按最后一个 ``@`` 切分
    （与 ``urlsplit`` / redis-py 的解析一致），不能停在第一个 ``@``；
    redis-py 同样接受查询串里的 ``password=`` / ``pwd=`` 参数，也要脱敏。
    """
    url = re.sub(r"://[^/]*@", "://***@", url)
    return re.sub(
        r"([?&](?:password|passwd|pwd|auth)=)[^&#]*",
        r"\1***",
        url,
        flags=re.IGNORECASE,
    )

ShortTermStore = ShortTermMemory | RedisShortTermMemory
LongTermStore = LongTermMemory | PostgresLongTermMemory

_FAILED_UNTIL: dict[str, float] = {}
_FAILURE_COOLDOWN_SEC = 60.0
# 惰性创建客户端必须互斥：冷却到期后的第一波并发请求各自 from_url + ping
# 会在 Redis 仍不可达时同时排 1.5 秒超时（放大成连接风暴），恢复时又会
# 创建多个连接池而只有一个进缓存、其余被各会话长期持有
_redis_lock = threading.Lock()
# 按 URL 缓存客户端：配置热更新（redis_url 改变 + reset_failure_cache）后
# 旧 URL 的客户端不会再被新配置的调用复用；reset 递增代数，在途创建完成时
# 代数已变则丢弃结果、不写回缓存
_redis_clients: dict[str, Any] = {}
# 单飞事件：同一 URL 的并发创建者等待 leader 完成，而不是各自排队 ping；
# 锁只在读写缓存 / 事件时持有，from_url + ping 的网络等待不占全局锁
_redis_creating: dict[str, threading.Event] = {}
_redis_generation = 0
_CREATE_WAIT_SEC = 5.0
# PostgreSQL 创建单飞：失败冷却到期后的第一波并发请求不能各自发起
# psycopg.connect（connect_timeout=3s）——数据库仍不可用时会把工作线程
# 整批钉在连接超时上，60 秒边界反复制造连接风暴。leader 失败后其余等待者
# 直接按降级处理（strict 抛持久化错误），不再逐个重试。
_pg_lock = threading.Lock()
_pg_creating: dict[str, threading.Event] = {}
_PG_CREATE_WAIT_SEC = 5.0
# 最近一次实际返回的后端（describe_backends 用真实状态而不是仅凭时间戳
# 推断：冷却过期后、下一次成功创建之前，实际仍在使用回退后端）
_ACTUAL_BACKEND: dict[str, str] = {}


def _failed_recently(key: str) -> bool:
    return _FAILED_UNTIL.get(key, 0.0) > time.monotonic()


def _mark_failed(key: str) -> None:
    _FAILED_UNTIL[key] = time.monotonic() + _FAILURE_COOLDOWN_SEC


def _get_redis_client(url: str, force: bool = False):
    """返回共享 Redis 客户端；``force=True`` 时跳过失败冷却强制尝试一次。

    删除会话 / 故障重放前的墓碑检查这类低频关键路径不能因冷却期返回
    None 而静默跳过共享保护：冷却只该节省高频枚举的等待时间。
    创建 / ping 全程持模块锁（双重检查）：并发波次只允许一个真实创建者，
    其余等待后命中缓存或冷却，避免连接风暴与多连接池泄漏。
    """
    global _redis_generation
    with _redis_lock:
        client = _redis_clients.get(url)
        if client is not None:
            return client
        if not force and _failed_recently(f"redis:{url}"):
            return None
        existing = _redis_creating.get(url)
        if existing is None:
            leader = True
            event = threading.Event()
            _redis_creating[url] = event
            generation = _redis_generation
        else:
            leader = False
            event = existing
    if not leader:
        # 从_url + ping 的网络等待不占全局锁：等待同一 URL 的 leader 完成
        event.wait(_CREATE_WAIT_SEC)
        with _redis_lock:
            return _redis_clients.get(url)
    try:
        try:
            import redis
        except ImportError:
            logger.warning("redis 未安装，短期记忆回退为进程内实现")
            return None
        try:
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            client.ping()
        except Exception as exc:  # noqa: BLE001 - 连接失败回退
            _mark_failed(f"redis:{url}")
            logger.warning(
                "Redis 不可用（%s），短期记忆回退为进程内实现",
                redact_secret(exc),
            )
            return None
        with _redis_lock:
            if generation == _redis_generation:
                _redis_clients[url] = client
            else:
                # reset 发生在创建期间：丢弃旧配置的客户端，不写回缓存
                client = _redis_clients.get(url)
        if client is not None:
            logger.info("Redis 短期记忆已启用: %s", _redact_url(url))
        return client
    finally:
        with _redis_lock:
            if _redis_creating.get(url) is event:
                _redis_creating.pop(url, None)
        event.set()


def get_shared_redis_client(force: bool = False) -> Any | None:
    """返回共享 Redis 客户端（供限流等模块复用连接池）。

    未配置 ``REDIS_URL``、Redis 不可用或处于失败冷却期时返回 None；
    ``force=True`` 跳过失败冷却（供删除 / 重放等关键路径重试一次）。
    """
    url = STORAGE_CONFIG.get("redis_url") or ""
    if not url:
        return None
    return _get_redis_client(url, force=force)


def shared_redis_configured() -> bool:
    """是否配置了共享 Redis（区分「未配置」与「配置但暂不可用」）。"""
    return bool(STORAGE_CONFIG.get("redis_url") or "")


def create_short_term_memory(
    session_id: str, max_turns: int = 10, user_id: str = ""
) -> ShortTermStore:
    """创建短期记忆：优先 Redis（如已配置且可用），否则进程内滑动窗口。"""
    url = STORAGE_CONFIG.get("redis_url") or ""
    if url:
        client = _get_redis_client(url)
        ttl = int(STORAGE_CONFIG.get("redis_ttl_sec", 3600))
        if ttl < 1:
            # 常见误解「0 = 永不过期」：Redis 的 EXPIRE 收到非正数会直接
            # 删键，短期记忆会静默全空。这里显式告警并按 1 秒生效。
            logger.warning(
                "REDIS_TTL_SEC=%s 非法（必须 >= 1），已按 1 秒处理", ttl
            )
            ttl = 1
        # 连接暂不可用（启动失败 / 工厂 60 秒冷却）时仍返回 Redis 实现：
        # 调用会走故障路径（影子缓存 + 操作日志），恢复后由后续实例重放。
        # 回退纯内存实现会让故障期的删除 / 清空彻底丢失（与
        # clear_short_term_session 的 queue_clear 兜底不对称）。
        # 保留客户端工厂：实例启动时拿不到客户端也可以在工厂恢复后自动
        # 重新接入，而不是永久停留在影子缓存。
        _ACTUAL_BACKEND["short_term"] = "redis"
        return RedisShortTermMemory(
            client,
            session_id,
            max_turns=max_turns,
            ttl_sec=ttl,
            user_id=user_id,
            client_factory=get_shared_redis_client,
        )
    _ACTUAL_BACKEND["short_term"] = "memory"
    return ShortTermMemory(max_turns=max_turns)


def create_long_term_memory(
    user_id: str, storage_path: str = "data/memory", strict: bool = False
) -> LongTermStore:
    """创建长期记忆：优先 PostgreSQL（如已配置且可用），否则 JSON 文件。

    ``strict=True`` 用于删除等破坏性操作：配置了 PostgreSQL 但当前不可用
    （连接失败 / 失败冷却期）时抛 ``MemoryPersistenceError``，而不是静默
    回退到 JSON——在回退库上「删除成功」会留下 PostgreSQL 里的数据，冷却
    结束后会话 / 消息重新出现。
    """
    dsn = STORAGE_CONFIG.get("postgres_dsn") or ""
    if dsn and not _failed_recently(f"postgres:{dsn}"):
        # 冷却到期后的第一波并发只让一个 leader 真正连接；其余等待其
        # 结果——leader 失败会重新进入冷却，等待者直接降级而不是各自再排
        # 3 秒连接超时（PG 仍不可用时的连接风暴）
        with _pg_lock:
            existing = _pg_creating.get(dsn)
            if existing is None:
                leader = True
                gate = threading.Event()
                _pg_creating[dsn] = gate
            else:
                leader = False
                gate = existing
        if not leader:
            gate.wait(_PG_CREATE_WAIT_SEC)
            if _failed_recently(f"postgres:{dsn}"):
                _ACTUAL_BACKEND["long_term"] = "json"
                if strict:
                    raise MemoryPersistenceError(
                        "PostgreSQL 处于故障冷却期，操作暂不可用，请稍后再试"
                    )
                return LongTermMemory(user_id, storage_path)
        try:
            memory = PostgresLongTermMemory(user_id, dsn)
            _ACTUAL_BACKEND["long_term"] = "postgres"
            logger.info("PostgreSQL 长期记忆已启用")
            return memory
        except Exception as exc:  # noqa: BLE001 - 连接失败回退
            _mark_failed(f"postgres:{dsn}")
            logger.warning(
                "PostgreSQL 不可用（%s），长期记忆回退为 JSON 文件",
                redact_secret(exc),
            )
            if strict:
                _ACTUAL_BACKEND["long_term"] = "json"
                raise MemoryPersistenceError(
                    f"PostgreSQL 不可用：{redact_secret(exc)}"
                ) from exc
        finally:
            if leader:
                with _pg_lock:
                    _pg_creating.pop(dsn, None)
                gate.set()
    elif dsn and strict:
        # 健康检查 / 指标按真实后端上报：冷却期内即使只有 strict 请求，
        # 也不能继续显示 postgres（此前 _ACTUAL_BACKEND 停留旧值）
        _ACTUAL_BACKEND["long_term"] = "json"
        raise MemoryPersistenceError(
            "PostgreSQL 处于故障冷却期，删除操作暂不可用，请稍后再试"
        )
    _ACTUAL_BACKEND["long_term"] = "json"
    return LongTermMemory(user_id, storage_path)


def reset_failure_cache() -> None:
    """清空失败冷却、缓存的 Redis 客户端与最近后端记录（测试 / 配置热更新）。

    持模块锁清空：与在途的 ``_get_redis_client`` 创建互斥，旧 URL 的客户端
    不会被 reset 之后完成的创建写回缓存。已经直接缓存客户端的消费方
    （限流器实例、已建会话的短期记忆、Web 状态）仍持有旧客户端，热更新后
    需重启进程才能全部切到新实例。
    """
    global _redis_generation
    with _redis_lock:
        _FAILED_UNTIL.clear()
        _redis_clients.clear()
        _ACTUAL_BACKEND.clear()
        # 在途创建完成时按代数丢弃结果，reset 之后旧客户端不会写回缓存
        _redis_generation += 1
    reset_redis_degraded()


def describe_backends() -> dict[str, Any]:
    """返回当前实际使用的后端名称（供 /api/health 与界面展示）。

    ``*_configured`` 表示是否配置了对应后端：配置了但当前回退本地时，
    界面可提示「已配置后端暂不可用」。
    """
    url = STORAGE_CONFIG.get("redis_url") or ""
    dsn = STORAGE_CONFIG.get("postgres_dsn") or ""
    short_term = "redis" if url and _get_redis_client(url) is not None else "memory"
    long_term = _ACTUAL_BACKEND.get("long_term")
    if long_term is None:
        # 本进程还没创建过长期记忆：只能按配置与失败冷却推断
        long_term = (
            "postgres"
            if dsn and not _failed_recently(f"postgres:{dsn}")
            else "json"
        )
    return {
        "short_term": short_term,
        "long_term": long_term,
        "short_term_configured": bool(url),
        "long_term_configured": bool(dsn),
        # 配置了 Redis 但当前用不上（启动时不可达 / 运行中故障回退进程内）。
        # 三条判据缺一不可：degraded() 只看冷却窗口（到期后即使从未成功过
        # 也返回 False）、available() 看最近一次 Redis 操作是否成功
        "short_term_degraded": bool(url)
        and (
            short_term != "redis"
            or redis_short_term_degraded()
            or not redis_short_term_available()
        ),
        # 配置了 PostgreSQL 但连接失败、长期记忆已回退本地 JSON
        "long_term_degraded": bool(dsn) and long_term == "json",
    }


def clear_short_term_session(session_id: str, user_id: str = "") -> None:
    """删除某个会话的 Redis 短期记忆（未启用 Redis 时为空操作）。

    同时清理旧格式键（``aligo:session:{id}:messages``），便于升级后回收遗留
    数据；并丢弃本进程内该键排队的故障期操作——删除会话后同一 session_id
    再被使用时，旧会话排队的消息 / 删除动作不能重放进新会话。
    """
    keys = {session_key(session_id)}
    # 排队操作只登记给「后续实例会使用」的键：带 user_id 时实例一律使用
    # 用户维度键，旧格式键没有任何重放者，登记只会占用注册表槽位
    replay_key = session_key(session_id, user_id) if user_id else session_key(session_id)
    if user_id:
        keys.add(replay_key)
    for key in keys:
        discard_pending_ops(key)
    url = STORAGE_CONFIG.get("redis_url") or ""
    if not url:
        return
    client = _get_redis_client(url)
    if client is None:
        # 启动 / 重连失败时客户端尚不可用（工厂 60 秒冷却）：同样要登记
        # 整键删除，否则冷却结束、会话 ID 被复用后会读到已删除的旧对话
        logger.warning("Redis 客户端不可用，登记会话清理（%s）", session_id)
        queue_clear(replay_key)
        return
    try:
        client.delete(*keys, *[applied_key(key) for key in keys])
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
        logger.warning("清理 Redis 会话失败（%s）: %s", session_id, exc)
        # 这是一次真实 Redis 操作失败：健康接口的可用性标志必须跟着变
        # （连接级故障进入冷却），不能只登记重放让状态显示正常
        note_redis_operation_failure(exc)
        # 删除失败时登记整键删除：墓碑（300 秒）过期后复用同一
        # session_id 时，下一次访问会先重放它，不会读到旧对话
        queue_clear(replay_key)
