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
# 会话纪元键：每次删除单调递增，不被墓碑撤销回退。墓碑只剩 1 小时记忆，
# 而删除前已在途的长回合（多智能体串行 + 重试可达数分钟）与「从未存在」
# 删除都会撤销墓碑——落库守卫依靠纪元在删除前后是否变化来判断「这一轮
# 是不是删除前启动的」，不依赖墓碑是否还在。TTL 比墓碑长得多，覆盖长
# 回合与故障重放窗口。
EPOCH_KEY_PREFIX = "aligo:session-epoch"
EPOCH_TTL_SEC = 86400.0
# 与 Web 的 AligoWebApp.tombstone_ttl_sec 同值（1 小时）：墓碑必须覆盖
# 「删除发生时仍在途的回合」的最长生命周期，CLI 删除的会话才能挡住多实例 /
# 长回合的迟到落库（旧值 300s 在多智能体重试超过 5 分钟时失效）
DEFAULT_TOMBSTONE_TTL_SEC = 3600.0

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


def _shared_client(force: bool = False) -> "Any | None":
    """取共享 Redis 客户端；冷却期内或获取失败返回 None。

    ``force=True``（删除 / 故障重放前的检查等低频关键路径）跳过冷却与
    工厂冷却，真实尝试一次——冷却只该节省高频枚举的等待时间，不能把
    「是否已删除」的保护静默关掉。
    """
    global _failure_until
    if not force and time.monotonic() < _failure_until:
        return None
    from context.storage import get_shared_redis_client

    try:
        return get_shared_redis_client(force=force)
    except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("读取共享 Redis 失败，跳过删除墓碑操作：%s", exc)
        return None



def deleted_key(user_id: str, session_id: str) -> str:
    """与 webapp 的 ``AligoWebApp._deleted_key`` 保持同一约定。"""
    return f"{DELETED_KEY_PREFIX}:{user_id}:{session_id}"


def epoch_key(user_id: str, session_id: str) -> str:
    """会话纪元键；Web 端写入使用同一键约定。"""
    return f"{EPOCH_KEY_PREFIX}:{user_id}:{session_id}"


def bump_session_epoch(
    user_id: str,
    session_id: str,
    ttl_sec: float = EPOCH_TTL_SEC,
    force: bool = False,
) -> None:
    """删除会话时递增会话纪元（单调，撤销墓碑也不会回退）。

    落库守卫在回合开始时记下纪元、提交前比对：值不同说明这一轮开始之后
    会话被删除过，即使是「从未存在 → 撤销墓碑」或并发删除后墓碑被撤销的
    路径也不能把已删除会话写回。
    """
    global _failure_until
    client = _shared_client(force=force)
    if client is None:
        return
    key = epoch_key(user_id, session_id)
    try:
        client.incr(key)
        client.expire(key, max(int(ttl_sec), 1))
    except Exception as exc:  # noqa: BLE001 - 纪元失败由墓碑兜底
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("写入会话纪元失败：%s", exc)


def get_session_epoch(user_id: str, session_id: str) -> str | None:
    """读取会话纪元；无纪元（从未删除）/ Redis 不可用时返回 None。"""
    global _failure_until
    client = _shared_client()
    if client is None:
        return None
    try:
        value = client.get(epoch_key(user_id, session_id))
    except Exception as exc:  # noqa: BLE001 - 读取失败按「无纪元」处理
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("读取会话纪元失败：%s", exc)
        return None
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def mark_session_deleted(
    user_id: str,
    session_id: str,
    ttl_sec: float = DEFAULT_TOMBSTONE_TTL_SEC,
    source: str = "cli",
    force: bool = False,
) -> bool:
    """写入共享删除墓碑；返回本次调用是否成为墓碑的所有者。

    Redis 不可用 / 写入失败时返回 False（不抛异常）。``force=True`` 跳过
    失败冷却真实尝试一次（删除是低频关键路径，冷却期静默跳过会让其它
    实例看不到这次删除）。``SET NX`` 不覆盖其它进程已有的墓碑——未写入
    时只有值恰好等于 ``source``（本进程先前的写入 / 带唯一 token 的幂等
    重试）才算自己拥有，否则返回 False，调用方回滚时不能删除别人的保护。
    无论写入归属如何，删除都会递增会话纪元。
    """
    global _failure_until
    client = _shared_client(force=force)
    if client is None:
        return False
    key = deleted_key(user_id, session_id)
    try:
        written = client.set(
            key,
            source,
            nx=True,
            ex=max(int(ttl_sec), 1),
        )
    except Exception as exc:  # noqa: BLE001 - 墓碑失败不影响删除本身
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("写入共享删除墓碑失败：%s", exc)
        return False
    bump_session_epoch(user_id, session_id, force=force)
    if written:
        return True
    try:
        current = client.get(key)
    except Exception:  # noqa: BLE001 - 读失败按非所有者处理
        return False
    if current is None:
        return False
    value = current.decode() if isinstance(current, bytes) else str(current)
    return value == source


def is_session_deleted(
    user_id: str, session_id: str, force: bool = False
) -> bool:
    """共享墓碑是否仍在有效期内；Redis 不可用 / 读取失败按「未删除」处理。

    CLI 的备份恢复用它避免把刚被删除的会话写回（墓碑 TTL 与 Web 一致，
    只覆盖删除后的短时间窗）。未配置 ``REDIS_URL`` 时进程之间没有共享通道，
    返回 False——调用方应在提示里说明。``force=True`` 供故障重放前的
    安全检查等关键路径跳过失败冷却，真实读取一次。
    """
    global _failure_until
    client = _shared_client(force=force)
    if client is None:
        return False
    try:
        return bool(client.exists(deleted_key(user_id, session_id)))
    except Exception as exc:  # noqa: BLE001 - 墓碑读取失败按未删除处理
        _failure_until = time.monotonic() + _FAILURE_COOLDOWN_SEC
        logger.warning("读取共享删除墓碑失败：%s", exc)
        return False


def unmark_session_deleted(
    user_id: str, session_id: str, source: str = "", force: bool = False
) -> bool:
    """撤销本进程写入的墓碑（仅当值等于 ``source`` 时删除）。

    与 :func:`mark_session_deleted` 的 SET NX 配对：删除失败（会话仍存在）
    时恢复可写，但不撤销其它实例先写的墓碑（它们代表先发生的删除；Web 端
    的墓碑值是请求 token，CLI 是 "cli"，互不误删）。
    """
    global _failure_until
    client = _shared_client(force=force)
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

