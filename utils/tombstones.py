"""会话删除墓碑（跨进程 / 跨实例共享）。

Web 实例与 CLI 共用同一 Redis 键约定（``aligo:deleted:<user>:<session>``，
TTL 与 Web 本地墓碑一致）：删除会话后写短时墓碑，其它实例 / 进程的新请求
据此拒绝在途对话把已删除内容写回。

未配置 ``REDIS_URL`` 或 Redis 不可用时返回 False：不同进程之间没有共享
通道，调用方（CLI）应在提示中说明「旧标签页请刷新」。
"""
import logging

logger = logging.getLogger(__name__)

DELETED_KEY_PREFIX = "aligo:deleted"
DEFAULT_TOMBSTONE_TTL_SEC = 300.0


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
    from context.storage import get_shared_redis_client

    try:
        client = get_shared_redis_client()
    except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
        logger.warning("读取共享 Redis 失败，跳过删除墓碑：%s", exc)
        return False
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
        logger.warning("写入共享删除墓碑失败：%s", exc)
        return False
    return True
