"""用户数据备份恢复（``user-export`` / ``/api/export`` JSON 的逆操作）。

恢复偏好、行程、聊天消息与会话元数据（标题 / 置顶 / 归档）。统计字段由
存储层在写入时自行累计，不直接覆盖；非法条目收集为错误而不是中断整个
文件。重复恢复幂等：行程按存储层「同路线 + 同起止日期」去重，聊天消息按
（会话, 角色, 内容）多重集去重（重复导入同一备份不会追加重复消息）。
"""
import json
from collections import Counter
from datetime import datetime
from typing import Any

from utils.trip_import import _strict_date

_MAX_PREFERENCES = 200
_MAX_MESSAGES = 200000


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_iso_timestamp(value: Any) -> str | None:
    """合法 ISO 时间戳原样保留；非法 / 缺失返回 None（按恢复时间落库）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def parse_user_export(text: str) -> tuple[dict[str, Any], list[str]]:
    """解析并校验 user-export JSON，返回 ``(可恢复数据, 错误列表)``。

    可恢复数据键：preferences / trips / messages / titles / pinned / archived。
    没有任何可恢复数据时返回空 dict（错误列表说明原因）。
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        return {}, [f"JSON 解析失败：{exc}"]
    if not isinstance(data, dict):
        return {}, ["文件内容不是 JSON 对象"]

    errors: list[str] = []
    sections: dict[str, Any] = {}

    preferences = _as_dict(data.get("preferences"))
    cleaned_preferences: dict[str, Any] = {}
    for index, (key, value) in enumerate(preferences.items(), start=1):
        name = str(key or "").strip()
        if not name or len(name) > 64:
            errors.append(f"跳过第 {index} 个偏好：键名非法")
            continue
        if index > _MAX_PREFERENCES:
            errors.append(f"偏好超过 {_MAX_PREFERENCES} 个，多余条目已跳过")
            break
        cleaned_preferences[name] = value
    sections["preferences"] = cleaned_preferences

    trips: list[dict[str, Any]] = []
    for index, row in enumerate(_as_list(data.get("trip_history")), start=1):
        if not isinstance(row, dict):
            errors.append(f"跳过第 {index} 条行程：不是对象")
            continue
        destination = str(row.get("destination") or "").strip()
        start_date = str(row.get("start_date") or "").strip()
        end_date = str(row.get("end_date") or "").strip()
        if not destination:
            errors.append(f"跳过第 {index} 条行程：目的地为空")
            continue
        start_dt = _strict_date(start_date)
        if start_dt is None:
            errors.append(f"跳过第 {index} 条行程：出发日期格式应为 YYYY-MM-DD")
            continue
        end_dt = None
        if end_date:
            end_dt = _strict_date(end_date)
            if end_dt is None:
                errors.append(f"跳过第 {index} 条行程：返程日期格式应为 YYYY-MM-DD")
                continue
            if end_dt < start_dt:
                errors.append(f"跳过第 {index} 条行程：结束日期不能早于出发日期")
                continue
        session_id = str(row.get("session_id") or "").strip() or None
        trips.append(
            {
                "origin": str(row.get("origin") or "").strip(),
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date if end_dt else "",
                "purpose": str(row.get("purpose") or "").strip() or "出差",
                "session_id": session_id,
            }
        )
    sections["trips"] = trips

    messages: list[dict[str, Any]] = []
    raw_messages = _as_list(data.get("chat_history"))
    if len(raw_messages) > _MAX_MESSAGES:
        errors.append(f"聊天消息超过 {_MAX_MESSAGES} 条，多余条目已跳过")
        raw_messages = raw_messages[:_MAX_MESSAGES]
    for index, row in enumerate(raw_messages, start=1):
        if not isinstance(row, dict):
            errors.append(f"跳过第 {index} 条消息：不是对象")
            continue
        role = str(row.get("role") or "").strip()
        content = row.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            errors.append(f"跳过第 {index} 条消息：角色或内容非法")
            continue
        session_id = str(row.get("session_id") or "").strip() or None
        messages.append(
            {
                "role": role,
                "content": content,
                "session_id": session_id,
                "timestamp": _clean_iso_timestamp(row.get("timestamp")),
            }
        )
    sections["messages"] = messages
    session_ids = {m["session_id"] for m in messages if m["session_id"]}

    titles: dict[str, str] = {}
    for key, value in _as_dict(data.get("session_titles")).items():
        session_id = str(key or "").strip()
        title = str(value or "").strip()
        if not session_id or not title:
            continue
        if session_id not in session_ids:
            # 悬空的会话元数据（导出源自身也会忽略）不阻断恢复
            continue
        titles[session_id] = title[:60]
    sections["titles"] = titles

    def _session_list(name: str) -> list[str]:
        result: list[str] = []
        for item in _as_list(data.get(name)):
            session_id = str(item or "").strip()
            if session_id and session_id in session_ids and session_id not in result:
                result.append(session_id)
        return result

    sections["pinned"] = _session_list("pinned_sessions")
    sections["archived"] = _session_list("archived_sessions")

    recoverable = any(
        sections[key]
        for key in ("preferences", "trips", "messages")
    )
    if not recoverable:
        errors.append(
            "没有可恢复的数据（preferences / trip_history / chat_history 均为空或非法）"
        )
        return {}, errors
    return sections, errors


def import_user_data(store: Any, sections: dict[str, Any]) -> dict[str, Any]:
    """把解析后的数据写入长期记忆；返回各类计数（供 CLI 输出）。"""
    result: dict[str, Any] = {
        "preferences": 0,
        "trips_created": 0,
        "trips_updated": 0,
        "messages_added": 0,
        "messages_skipped": 0,
        "titles": 0,
        "pinned": 0,
        "archived": 0,
    }

    for key, value in sections.get("preferences", {}).items():
        store.save_preference(key, value)
        result["preferences"] += 1

    trips = sections.get("trips", [])
    if trips:
        before = len(store.get_trip_history(limit=None))
        for row in trips:
            store.save_trip_history(row)
        after = len(store.get_trip_history(limit=None))
        created = max(0, after - before)
        result["trips_created"] = created
        result["trips_updated"] = len(trips) - created

    messages = sections.get("messages", [])
    if messages:
        # （会话, 角色, 内容）多重集去重：同一备份重复导入幂等，同一会话里
        # 重复出现的相同提问仍按出现次数补回
        existing = Counter(
            (
                message.get("session_id"),
                message.get("role"),
                str(message.get("content") or ""),
            )
            for message in store.get_chat_history(limit=None)
        )
        for message in messages:
            key = (message["session_id"], message["role"], message["content"])
            if existing[key] > 0:
                existing[key] -= 1
                result["messages_skipped"] += 1
                continue
            store.add_chat_message(
                message["role"],
                message["content"],
                message["session_id"],
                timestamp=message["timestamp"],
            )
            result["messages_added"] += 1

    for session_id, title in sections.get("titles", {}).items():
        if store.set_session_title(session_id, title):
            result["titles"] += 1
    for session_id in sections.get("pinned", []):
        if store.set_session_pinned(session_id, True):
            result["pinned"] += 1
    for session_id in sections.get("archived", []):
        if store.set_session_archived(session_id, True):
            result["archived"] += 1
    return result
