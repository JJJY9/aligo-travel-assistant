"""
存储后端工厂

按配置选择记忆后端（未配置或连接失败时自动回退，保证本地开箱即用）：

- 短期记忆：配置 ``REDIS_URL`` → RedisShortTermMemory；否则进程内 ShortTermMemory
- 长期记忆：配置 ``DATABASE_URL`` → PostgresLongTermMemory；否则 JSON 文件 LongTermMemory

连接失败会记录一次警告并进入 60 秒冷却，避免每个会话都等待连接超时。
"""
import logging
import time
from typing import Any

from config import STORAGE_CONFIG
from context.long_term_memory import LongTermMemory
from context.postgres_long_term_memory import PostgresLongTermMemory
from context.short_term_memory import RedisShortTermMemory, ShortTermMemory

logger = logging.getLogger(__name__)

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
        logger.info("Redis 短期记忆已启用: %s", url)
        return client
    except Exception as exc:  # noqa: BLE001 - 连接失败回退
        _mark_failed(f"redis:{url}")
        logger.warning("Redis 不可用（%s），短期记忆回退为进程内实现", exc)
        return None


def create_short_term_memory(session_id: str, max_turns: int = 10) -> ShortTermStore:
    """创建短期记忆：优先 Redis（如已配置且可用），否则进程内滑动窗口。"""
    url = STORAGE_CONFIG.get("redis_url") or ""
    if url:
        client = _get_redis_client(url)
        if client is not None:
            ttl = int(STORAGE_CONFIG.get("redis_ttl_sec", 3600))
            return RedisShortTermMemory(client, session_id, max_turns=max_turns, ttl_sec=ttl)
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
