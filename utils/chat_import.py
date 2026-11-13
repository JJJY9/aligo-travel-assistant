"""备份恢复的批量消息去重与时间归位。

备份恢复按（会话, 角色, 内容）多重集去重，但只对单条消息生效：用户导出
备份后又对同一问题编辑重发 / 重新生成过，再恢复旧备份时，提问命中去重被
跳过，回答内容不同却被当成新消息追加——会话末尾凭空多出一条没有对应
提问的回答，前端还会把它配到最后一条提问下，编辑 / 重新生成（删除最后一
轮）从此失配。

这里在多重集去重的基础上按会话跟踪「提问已被跳过」的轮次：提问已在目标
库中时，该轮次随后的回答（重复或备份里的其它版本）一律跳过——目标库自己
的回答保留。这是有意的本地优先策略：把备份里的旧 / 另一版本回答追加进
会话会破坏「提问→回答」配对与「删除最后一轮」的认定，而回答本身只是同一
提问的备选输出，不涉及提问 / 行程 / 偏好等用户数据的丢失。无会话归属
（session_id 为 None）的遗留消息无法可靠配对轮次，只按单条去重。

缺失 / 非法时间戳的恢复消息用 :func:`prepare_import_messages` 补齐：
沿用同会话相邻消息的时间（优先较早的一侧），都没有时按恢复时间。落库
时间与合并位置使用同一口径，JSON / PostgreSQL 两个后端读出的会话顺序
一致，不会出现回答排在提问之前的乱序。

预览（dry-run）、恢复上限预演与两个存储后端共用本模块，避免多处去重与
排序语义漂移。
"""
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from utils.timestamps import chronological_key

_MESSAGE_ROLES = frozenset({"user", "assistant"})

# （会话键, 角色, 内容）多重集
MessageKey = tuple[str | None, Any, str]


def session_dedupe_key(value: Any) -> str | None:
    """消息去重用的会话键：旧数据里的整数 id 字符串化，None 保持 None。"""
    return None if value is None else str(value)


def prepare_import_messages(
    messages: Iterable[dict[str, Any]], now: str | None = None
) -> list[dict[str, Any]]:
    """返回补齐时间戳后的消息（浅拷贝，不修改调用方数据）。

    缺失 / 非法时间戳（以及越界年份被解析为缺失）的消息：沿用同一会话
    相邻消息的时间——提问优先用后一条（自己的回答）的锚点，回答优先用
    前一条（自己的提问）的锚点，保证备份内顺序非时间升序时「提问在前、
    回答在后」；只有一侧时用该侧；都没有时按恢复时间。JSON 与
    PostgreSQL 写入的时间戳完全一致。
    """
    rows = list(messages)
    fallback = now or datetime.now().isoformat()
    keys: list[str] = []
    for message in rows:
        raw = message.get("timestamp")
        keys.append(chronological_key(raw) if raw else "")

    # 同一会话「后一条带时间戳消息」的时间键（从后往前）
    next_keys: list[str] = [""] * len(rows)
    last_seen: dict[str | None, str] = {}
    for index in range(len(rows) - 1, -1, -1):
        session_key = session_dedupe_key(rows[index].get("session_id"))
        next_keys[index] = last_seen.get(session_key, "")
        if keys[index]:
            last_seen[session_key] = keys[index]

    previous: dict[str | None, str] = {}
    prepared: list[dict[str, Any]] = []
    for index, message in enumerate(rows):
        session_key = session_dedupe_key(message.get("session_id"))
        if keys[index]:
            timestamp = message.get("timestamp")
            previous[session_key] = keys[index]
        else:
            previous_key = previous.get(session_key, "")
            next_key = next_keys[index]
            if previous_key and next_key:
                # 提问贴着自己的回答，回答贴着自己的提问：备份内顺序
                # 非时间升序时也不会把回答排到提问之前
                role = str(message.get("role") or "")
                timestamp = next_key if role == "user" else previous_key
            else:
                timestamp = previous_key or next_key or fallback
        prepared.append({**message, "timestamp": timestamp})
    return prepared


def filter_import_messages(
    messages: Iterable[dict[str, Any]],
    existing: Counter[MessageKey],
) -> list[dict[str, Any]]:
    """返回本次恢复真正要写入的消息（保持备份顺序）。

    ``existing`` 是目标库的（会话, 角色, 内容）多重集，命中时原地扣减：
    与原先存储层的去重语义一致（备份自身内部的重复消息仍全部保留）。
    角色非法的消息直接丢弃。

    提问已在目标库中（命中去重）时，该会话随后的回答一律跳过，直到下一条
    提问：避免恢复出没有对应提问的尾部回答、破坏前端问答配对与
    「删除最后一轮」。无会话归属的消息不做轮次配对，只按单条去重。
    """
    kept: list[dict[str, Any]] = []
    skipped_turns: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if role not in _MESSAGE_ROLES:
            continue
        session_key = session_dedupe_key(message.get("session_id"))
        key: MessageKey = (
            session_key,
            role,
            str(message.get("content") or ""),
        )
        duplicate = existing[key] > 0
        if role == "user":
            if duplicate:
                existing[key] -= 1
                if session_key is not None:
                    skipped_turns.add(session_key)
                continue
            # 新的提问：该会话回到正常轮次
            if session_key is not None:
                skipped_turns.discard(session_key)
        elif session_key is not None and session_key in skipped_turns:
            if duplicate:
                existing[key] -= 1
            continue
        if duplicate:
            existing[key] -= 1
            continue
        kept.append(message)
    return kept
