"""
存储后端工厂

按配置选择记忆后端（未配置或连接失败时自动回退，保证本地开箱即用）：

- 短期记忆：配置 ``REDIS_URL`` → RedisShortTermMemory；否则进程内 ShortTermMemory
- 长期记忆：配置 ``DATABASE_URL`` → PostgresLongTermMemory；否则 JSON 文件 LongTermMemory

连接失败会记录一次警告并进入 60 秒冷却，避免每个会话都等待连接超时。
"""
import logging
import re
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
    queue_clear,
    redis_short_term_degraded,
    session_key,
)

logger = logging.getLogger(__name__)

def _redact_url(url: str) -> str:
    """隐藏 Redis URL 中的密码，避免日志泄露。

    用户信息段可能在密码中包含未转义的 ``@``，按最后一个 ``@`` 切分
    （与 ``urlsplit`` / redis-py 的解析一致），不能停在第一个 ``@``。
    """
    return re.sub(r"://[^/]*@", "://***@", url)

ShortTermStore = ShortTermMemory | RedisShortTermMemory
LongTermStore = LongTermMemory | PostgresLongTermMemory

_FAILED_UNTIL: dict[str, float] = {}
_FAILURE_COOLDOWN_SEC = 60.0
_redis_client: Any = None


def _failed_recently(key: str) -> bool:
    return _FAILED_UNTIL.get(key, 0.0) > time.monotonic()


def _mark_failed(key: str) -> None:
    _FAILED_UNTIL[key] = time.monotonic() + _FAILURE_COOLDOWN_SEC


def _get_redis_client(url: str):
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if _failed_recently(f"redis:{url}"):
        return None
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
        _redis_client = client
        logger.info("Redis 短期记忆已启用: %s", _redact_url(url))
        return client
    except Exception as exc:  # noqa: BLE001 - 连接失败回退
        _mark_failed(f"redis:{url}")
        logger.warning("Redis 不可用（%s），短期记忆回退为进程内实现", exc)
        return None


def get_shared_redis_client() -> Any | None:
    """返回共享 Redis 客户端（供限流等模块复用连接池）。

    未配置 ``REDIS_URL``、Redis 不可用或处于失败冷却期时返回 None。
    """
    url = STORAGE_CONFIG.get("redis_url") or ""
    if not url:
        return None
    return _get_redis_client(url)


def create_short_term_memory(
    session_id: str, max_turns: int = 10, user_id: str = ""
) -> ShortTermStore:
    """创建短期记忆：优先 Redis（如已配置且可用），否则进程内滑动窗口。"""
    url = STORAGE_CONFIG.get("redis_url") or ""
    if url:
        client = _get_redis_client(url)
        ttl = int(STORAGE_CONFIG.get("redis_ttl_sec", 3600))
        # 连接暂不可用（启动失败 / 工厂 60 秒冷却）时仍返回 Redis 实现：
        # 调用会走故障路径（影子缓存 + 操作日志），恢复后由后续实例重放。
        # 回退纯内存实现会让故障期的删除 / 清空彻底丢失（与
        # clear_short_term_session 的 queue_clear 兜底不对称）
        return RedisShortTermMemory(
            client,
            session_id,
            max_turns=max_turns,
            ttl_sec=ttl,
            user_id=user_id,
        )
    return ShortTermMemory(max_turns=max_turns)


def create_long_term_memory(user_id: str, storage_path: str = "data/memory") -> LongTermStore:
    """创建长期记忆：优先 PostgreSQL（如已配置且可用），否则 JSON 文件。"""
    dsn = STORAGE_CONFIG.get("postgres_dsn") or ""
    if dsn and not _failed_recently(f"postgres:{dsn}"):
        try:
            memory = PostgresLongTermMemory(user_id, dsn)
            logger.info("PostgreSQL 长期记忆已启用")
            return memory
        except Exception as exc:  # noqa: BLE001 - 连接失败回退
            _mark_failed(f"postgres:{dsn}")
            logger.warning("PostgreSQL 不可用（%s），长期记忆回退为 JSON 文件", exc)
    return LongTermMemory(user_id, storage_path)


def reset_failure_cache() -> None:
    """清空失败冷却与缓存的 Redis 客户端（测试或配置热更新时使用）。"""
    global _redis_client
    _FAILED_UNTIL.clear()
    _redis_client = None


def describe_backends() -> dict[str, Any]:
    """返回当前实际使用的后端名称（供 /api/health 与界面展示）。

    ``*_configured`` 表示是否配置了对应后端：配置了但当前回退本地时，
    界面可提示「已配置后端暂不可用」。
    """
    url = STORAGE_CONFIG.get("redis_url") or ""
    dsn = STORAGE_CONFIG.get("postgres_dsn") or ""
    short_term = "redis" if url and _get_redis_client(url) is not None else "memory"
    long_term = "postgres" if dsn and not _failed_recently(f"postgres:{dsn}") else "json"
    return {
        "short_term": short_term,
        "long_term": long_term,
        "short_term_configured": bool(url),
        "long_term_configured": bool(dsn),
        # 配置了 Redis 但运行中故障、短期记忆已回退进程内缓存
        "short_term_degraded": bool(url)
        and short_term == "redis"
        and redis_short_term_degraded(),
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
        # 删除失败时登记整键删除：墓碑（300 秒）过期后复用同一
        # session_id 时，下一次访问会先重放它，不会读到旧对话
        queue_clear(replay_key)
