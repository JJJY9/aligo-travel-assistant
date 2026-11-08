"""会话删除墓碑（跨进程 / 跨实例共享）。

Web 实例与 CLI 共用同一 Redis 键约定（``aligo:deleted:<user>:<session>``，
TTL 与 Web 本地墓碑一致）：删除会话后写短时墓碑，其它实例 / 进程的新请求
据此拒绝在途对话把已删除内容写回。

未配置 ``REDIS_URL`` 或 Redis 不可用时返回 False：不同进程之间没有共享
通道，调用方（CLI）应在提示中说明「旧标签页请刷新」。
"""
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DELETED_KEY_PREFIX = "aligo:deleted"
DEFAULT_TOMBSTONE_TTL_SEC = 300.0

# 操作级失败冷却：客户端已缓存后 Redis 中途变成黑洞（丢包不复位）时，
# 每次 EXISTS / SET 都要等满 socket 超时；CLI 的备份恢复逐会话调用读取
# 接口，数千会话会把恢复拖成小时级并刷屏日志。冷却期内直接按「未删除 /
# 写入失败」处理（与 context/storage、限流桶的同一约定）。
_FAILURE_COOLDOWN_SEC = 60.0
_failure_until = 0.0


def reset_failure_cache() -> None:
    """清空失败冷却（测试隔离 / 配置热更新后立即重试）。"""
    global _failure_until
    _failure_until = 0.0


def _shared_client() -> "Any | None":
    """取共享 Redis 客户端；冷却期内或获取失败返回 None。"""
    global _failure_until
    if time.monotonic() < _failure_until:
        return None
    from context.storage import get_shared_redis_client

    try:
        return get_shared_redis_client()
    except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("读取共享 Redis 失败，跳过删除墓碑操作：%s", exc)
        return None



def deleted_key(user_id: str, session_id: str) -> str:
    """与 webapp 的 ``AligoWebApp._deleted_key`` 保持同一约定。"""
    return f"{DELETED_KEY_PREFIX}:{user_id}:{session_id}"


def mark_session_deleted(
    user_id: str,
    session_id: str,
    ttl_sec: float = DEFAULT_TOMBSTONE_TTL_SEC,
    source: str = "cli",
) -> bool:
    """写入共享删除墓碑；Redis 不可用 / 写入失败时返回 False（不抛异常）。

    ``SET NX``：不覆盖其它实例已有的墓碑（它们的删除先发生，以先写者为准）。
    """
    global _failure_until
    client = _shared_client()
    if client is None:
        return False
    try:
        client.set(
            deleted_key(user_id, session_id),
            source,
            nx=True,
            ex=max(int(ttl_sec), 1),
        )
    except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("写入共享删除墓碑失败：%s", exc)
        return False
    return True


def is_session_deleted(user_id: str, session_id: str) -> bool:
    """共享墓碑是否仍在有效期内；Redis 不可用 / 读取失败按「未删除」处理。

    CLI 的备份恢复用它避免把刚被删除的会话写回（墓碑 TTL 与 Web 一致，
    只覆盖删除后的短时间窗）。未配置 ``REDIS_URL`` 时进程之间没有共享通道，
    返回 False——调用方应在提示里说明。
    """
    global _failure_until
    client = _shared_client()
    if client is None:
        return False
    try:
        return bool(client.exists(deleted_key(user_id, session_id)))
    except Exception as exc:  # noqa: BLE001 - 墓碑读取失败按未删除处理
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("读取共享删除墓碑失败：%s", exc)
        return False


def unmark_session_deleted(
    user_id: str, session_id: str, source: str = ""
) -> bool:
    """撤销本进程写入的墓碑（仅当值等于 ``source`` 时删除）。

    与 :func:`mark_session_deleted` 的 SET NX 配对：删除失败（会话仍存在）
    时恢复可写，但不撤销其它实例先写的墓碑（它们代表先发生的删除；Web 端
    的墓碑值是请求 token，CLI 是 "cli"，互不误删）。
    """
    global _failure_until
    client = _shared_client()
    if client is None:
        return False
    key = deleted_key(user_id, session_id)
    try:
        current = client.get(key)
        if current is None:
            return False
        if source:
            value = current.decode() if isinstance(current, bytes) else str(current)
            if value != source:
                return False
        client.delete(key)
        return True
    except Exception as exc:  # noqa: BLE001 - 撤销失败只影响可写性一会儿
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("撤销共享删除墓碑失败：%s", exc)
        return False

