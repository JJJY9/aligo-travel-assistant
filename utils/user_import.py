"""用户数据备份恢复（``user-export`` / ``/api/export`` JSON 的逆操作）。

恢复偏好、行程、聊天消息与会话元数据（标题 / 置顶 / 归档）。统计字段由
存储层在写入时自行累计，不直接覆盖；非法条目收集为错误而不是中断整个
文件。重复恢复幂等：行程按存储层「同路线 + 同起止日期」去重，聊天消息按
（会话, 角色, 内容）多重集去重（重复导入同一备份不会追加重复消息）。

时间戳统一归一成带时区 ISO（naive 按本机时区解释），写入 TIMESTAMPTZ 不会
发生时区偏移；消息按时间升序批量写入（单次存储层写锁 / 单事务），导入
到已存在的会话时旧消息追加在当前转录末尾（存储层按追加顺序保存）。
"""
import json
import math
import re
from collections import Counter
from typing import Any

from utils.timestamps import chronological_key, localize_naive, parse_iso_datetime
from utils.trip_import import _strict_date, trip_identity

_MAX_PREFERENCES = 200
_MAX_TRIPS = 5000
_MAX_MESSAGES = 200000
_MAX_KNOWLEDGE = 50000
# 单个备份文件的体积上限：整份读入内存之前先拒绝明显异常的输入
MAX_BACKUP_BYTES = 50 * 1024 * 1024

_SESSION_ID_RE = re.compile(r"^[\w-]{1,64}$")
_USAGE_NUMBER_FIELDS = ("calls", "input_tokens", "output_tokens", "total_tokens")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_timestamp(value: Any) -> str | None:
    """合法 ISO 时间戳归一成带时区形式；非法 / 缺失返回 None（按恢复时间落库）。"""
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = localize_naive(parsed)
    return parsed.isoformat()


def _clean_session_id(value: Any) -> tuple[str | None, bool]:
    """返回 ``(session_id, 是否合法)``；缺失 / 空白按无会话处理。"""
    if value is None:
        return None, True
    text = str(value).strip()
    if not text:
        return None, True
    if text.startswith("-") or not _SESSION_ID_RE.fullmatch(text):
        return None, False
    return text, True


def _normalize_trip_date(value: str) -> str:
    """补零 YYYY-M-D 形态；其它格式原样保留（备份保真优先于格式严格）。"""
    if _strict_date(value) is not None:
        return value
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if not match:
        return value
    month = int(match.group(2))
    day = int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return value
    return f"{match.group(1)}-{month:02d}-{day:02d}"


_MAX_JSON_DEPTH = 256


def _json_nesting_too_deep(text: str, limit: int = _MAX_JSON_DEPTH) -> bool:
    """括号/对象嵌套是否超过上限（字符串内的括号不计）。

    深度嵌套内容会让展示 / 摘要路径的 json.loads 抛 RecursionError，而
    C 扫描器的递归预算随解释器与编译选项而异（同一 5000 层数组在部分
    3.12 构建上直接解析成功）——按文本深度预检，与解释器无关。
    合法历史消息的嵌套只有个位数层，256 层上限没有误伤面。
    """
    depth = 0
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False

def _text_problem(value: str) -> str | None:
    """备份文本不能含 NUL 或孤立代理项：两后端行为分叉且都不可恢复。

    PostgreSQL 的 text 列拒绝 0x00（psycopg DataError，不在重试错误元组
    里，整份恢复 503）；孤立代理项在本机 UTF-8 写盘 / 编码时抛
    UnicodeEncodeError（JSON 后端 json.dump 同样走 UTF-8）。
    """
    if "\x00" in value:
        return "包含 NUL 字符"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "包含无法编码的字符（孤立代理项）"
    return None


def _assistant_content_problem(content: str) -> str | None:
    """assistant 消息里的 _meta.usage 若存在则必须是数字字段。

    非数字会让 /api/status 与导出的 int() 解析在之后每次请求时报 500，
    且数据持久存在——在导入时拒绝比事后修复便宜。
    """
    text = content.strip()
    if not text.startswith(("{", "[")):
        return None
    if _json_nesting_too_deep(text):
        return "消息内容嵌套过深"
    try:
        data = json.loads(text)
    except RecursionError:
        # 深度嵌套（如 5000 层数组）会让展示 / 摘要路径的 json.loads 抛
        # RecursionError，而那些路径只捕获解析错误——落库后每次请求 500
        return "消息内容嵌套过深"
    except ValueError:
        return None
    if not isinstance(data, dict) or "_meta" not in data:
        return None
    meta = data.get("_meta")
    if not isinstance(meta, dict):
        return "消息元数据不是对象"
    agents = meta.get("agents")
    if agents is not None and not isinstance(agents, list):
        # 汇总接口会迭代该字段：非列表（5 / true）落库后 /api/status 永久 500
        return "消息智能体列表不是数组"
    for item in agents or []:
        if not isinstance(item, dict):
            continue
        duration = item.get("duration")
        if duration is None:
            continue
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return "智能体耗时不是数字"
        if isinstance(duration, float) and not math.isfinite(duration):
            return "智能体耗时不是有限数"
    elapsed = meta.get("elapsed")
    if elapsed is not None:
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            return "消息耗时不是数字"
        # 1e999 解析为 inf：/api/status 的 avg_elapsed 变 inf，JSON 序列化
        # （allow_nan=False）直接 500，且故障随消息永久存在
        if isinstance(elapsed, float) and not math.isfinite(elapsed):
            return "消息耗时不是有限数"
    usage = meta.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        return "消息用量不是对象"
    for key in _USAGE_NUMBER_FIELDS:
        value = usage.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "消息用量字段不是数字"
        # NaN / Infinity 也是 float：落库后 /api/status 的 int() 会 ValueError /
        # OverflowError，且此后每次汇总都 500，必须在导入时拒绝
        if isinstance(value, float) and not math.isfinite(value):
            return "消息用量字段不是有限数"
    return None


def parse_user_export(text: str) -> tuple[dict[str, Any], list[str]]:
    """解析并校验 user-export JSON 文本，返回 ``(可恢复数据, 错误列表)``。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        return {}, [f"JSON 解析失败：{exc}"]
    return parse_user_export_data(data)


def parse_user_export_data(data: Any) -> tuple[dict[str, Any], list[str]]:
    """校验已解析的 user-export JSON 对象（CLI 文本路径与 Web API 共用）。

    可恢复数据键：preferences / trips / messages / titles / pinned / archived。
    没有任何可恢复数据时返回空 dict（错误列表说明原因）。
    """
    if not isinstance(data, dict):
        return {}, ["文件内容不是 JSON 对象"]

    errors: list[str] = []
    sections: dict[str, Any] = {}

    raw_preferences = data.get("preferences")
    if raw_preferences is not None and not isinstance(raw_preferences, dict):
        errors.append("preferences 段不是对象，已忽略")
    preferences = raw_preferences if isinstance(raw_preferences, dict) else {}
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
    raw_trips = _as_list(data.get("trip_history"))
    if len(raw_trips) > _MAX_TRIPS:
        errors.append(f"行程超过 {_MAX_TRIPS} 条，多余条目已跳过")
        raw_trips = raw_trips[:_MAX_TRIPS]
    for index, row in enumerate(raw_trips, start=1):
        if not isinstance(row, dict):
            errors.append(f"跳过第 {index} 条行程：不是对象")
            continue
        destination = str(row.get("destination") or "").strip()
        if not destination:
            errors.append(f"跳过第 {index} 条行程：目的地为空")
            continue
        start_date = _normalize_trip_date(str(row.get("start_date") or "").strip())
        if not start_date:
            errors.append(f"跳过第 {index} 条行程：出发日期为空")
            continue
        end_date = _normalize_trip_date(str(row.get("end_date") or "").strip())
        start_dt = _strict_date(start_date)
        end_dt = _strict_date(end_date) if end_date else None
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            errors.append(f"跳过第 {index} 条行程：结束日期不能早于出发日期")
            continue
        session_id, ok = _clean_session_id(row.get("session_id"))
        if not ok:
            errors.append(f"第 {index} 条行程：会话 ID 非法，已忽略会话关联")
        origin = str(row.get("origin") or "").strip()
        purpose = str(row.get("purpose") or "").strip() or "出差"
        problem = (
            _text_problem(origin)
            or _text_problem(destination)
            or _text_problem(purpose)
        )
        if problem:
            errors.append(f"跳过第 {index} 条行程：{problem}")
            continue
        trips.append(
            {
                "origin": origin,
                "destination": destination,
                "start_date": start_date,
                "end_date": end_date,
                "purpose": purpose,
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
        session_id, ok = _clean_session_id(row.get("session_id"))
        if not ok:
            errors.append(f"跳过第 {index} 条消息：会话 ID 非法")
            continue
        problem = _text_problem(content)
        if problem:
            errors.append(f"跳过第 {index} 条消息：{problem}")
            continue
        if role == "assistant":
            problem = _assistant_content_problem(content)
            if problem:
                errors.append(f"跳过第 {index} 条消息：{problem}")
                continue
        messages.append(
            {
                "role": role,
                "content": content,
                "session_id": session_id,
                "timestamp": _normalize_timestamp(row.get("timestamp")),
            }
        )
    # 按时间升序写入（无时间戳的排最后）：多会话恢复后转录顺序与会话
    # updated_at 都保持与备份一致
    messages.sort(
        key=lambda message: (
            message["timestamp"] is None,
            chronological_key(message["timestamp"]),
        )
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
        problem = _text_problem(title)
        if problem:
            errors.append(f"忽略会话 {session_id} 的标题：{problem}")
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

    knowledge: list[dict[str, Any]] = []
    raw_knowledge = data.get("knowledge")
    invalid_groups: set[str] = set()
    if raw_knowledge is not None and not isinstance(raw_knowledge, list):
        errors.append("knowledge 段不是数组，已忽略")
    elif isinstance(raw_knowledge, list):
        for index, item in enumerate(raw_knowledge[:_MAX_KNOWLEDGE], start=1):
            if not isinstance(item, dict):
                errors.append(f"跳过第 {index} 个知识片段：不是对象")
                continue
            content = item.get("content")
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            group = str(metadata.get("group") or "")
            if not isinstance(content, str) or not content.strip():
                errors.append(f"跳过第 {index} 个知识片段：内容为空或非法")
                if group:
                    invalid_groups.add(group)
                continue
            problem = _text_problem(content)
            if problem:
                errors.append(f"跳过第 {index} 个知识片段：{problem}")
                if group:
                    invalid_groups.add(group)
                continue
            knowledge.append(
                {
                    "content": content,
                    "metadata": {
                        str(key)[:32]: str(value)[:200]
                        for key, value in list(metadata.items())[:_MAX_PREFERENCES]
                    },
                }
            )
        if len(raw_knowledge) > _MAX_KNOWLEDGE:
            errors.append(f"知识片段超过 {_MAX_KNOWLEDGE} 条，多余条目已跳过")
    if invalid_groups:
        # 同组的其它分片一并丢弃：只删坏分片会让 import_documents 的分组
        # 完整性校验看到「缺片」而拒绝整份知识恢复，连无关的合法文档也
        # 一起导入失败（坏组整组跳过，其余文档照常恢复）
        remaining = [
            entry
            for entry in knowledge
            if str(entry["metadata"].get("group") or "") not in invalid_groups
        ]
        if len(remaining) != len(knowledge):
            errors.append(
                f"跳过 {len(knowledge) - len(remaining)} 个知识分片："
                f"同组含无效分片（{', '.join(sorted(invalid_groups)[:3])}）"
            )
        knowledge = remaining
    sections["knowledge"] = knowledge

    recoverable = any(
        sections[key]
        for key in ("preferences", "trips", "messages", "knowledge")
    )
    if not recoverable:
        errors.append(
            "没有可恢复的数据（preferences / trip_history / chat_history 均为空或非法）"
        )
        return {}, errors
    return sections, errors


def preview_user_import(store: Any, sections: dict[str, Any]) -> dict[str, Any]:
    """预览恢复结果（只读）：返回与 import_user_data 相同键的计数，不写库。"""
    result: dict[str, Any] = {
        "preferences": len(sections.get("preferences", {})),
        "knowledge": len(sections.get("knowledge", [])),
        "trips_created": 0,
        "trips_updated": 0,
        "messages_added": 0,
        "messages_skipped": 0,
        "titles": len(sections.get("titles", {})),
        "pinned": len(sections.get("pinned", [])),
        "archived": len(sections.get("archived", [])),
    }

    pending_keys = {
        trip_identity(trip) for trip in store.get_trip_history(limit=None)
    }
    for row in sections.get("trips", []):
        key = trip_identity(row)
        if key in pending_keys:
            result["trips_updated"] += 1
        else:
            result["trips_created"] += 1
            pending_keys.add(key)

    messages = sections.get("messages", [])
    if messages:
        existing = Counter(
            (
                message.get("session_id"),
                message.get("role"),
                str(message.get("content") or ""),
            )
            for message in store.get_chat_history(limit=None)
        )
        for message in messages:
            message_key = (
                message["session_id"],
                message["role"],
                message["content"],
            )
            if existing[message_key] > 0:
                existing[message_key] -= 1
                result["messages_skipped"] += 1
            else:
                result["messages_added"] += 1
    return result


def import_user_data(store: Any, sections: dict[str, Any]) -> dict[str, Any]:
    """把解析后的数据写入长期记忆；返回各类计数（供 CLI / Web 输出）。"""
    result: dict[str, Any] = {
        "preferences": 0,
        "trips_created": 0,
        "trips_updated": 0,
        "messages_added": 0,
        "messages_skipped": 0,
        "titles": 0,
        "pinned": 0,
        "archived": 0,
        "knowledge_imported": 0,
        "knowledge_skipped": 0,
    }

    for key, value in sections.get("preferences", {}).items():
        store.save_preference(key, value)
        result["preferences"] += 1

    trips = sections.get("trips", [])
    if trips:
        # 批量保存：JSON 后端一次重载 + 一次原子写回（逐条写是 O(n²)，
        # 几千条行程会把恢复拖成分钟级）
        created, updated = store.save_trip_histories(trips)
        result["trips_created"] = int(created)
        result["trips_updated"] = int(updated)

    messages = sections.get("messages", [])
    if messages:
        # 批量写入 + 存储层写锁内去重：JSON 后端一次重载一次原子写回
        # （逐条写是 O(n²)，大备份会跑几分钟到数小时）；去重放在写锁内
        # 使并发导入也不会因锁外快照过期而重复插入
        added = store.add_chat_messages(messages, dedupe=True)
        added = int(added if added is not None else len(messages))
        result["messages_added"] = added
        result["messages_skipped"] = len(messages) - added

    titles = sections.get("titles", {})
    if titles:
        result["titles"] = int(store.set_session_titles(titles))
    pinned = sections.get("pinned", [])
    if pinned:
        result["pinned"] = int(store.set_sessions_pinned(pinned, True))
    archived = sections.get("archived", [])
    if archived:
        result["archived"] = int(store.set_sessions_archived(archived, True))
    knowledge = sections.get("knowledge", [])
    if knowledge:
        # 知识片段是全局知识库（不随用户隔离）：失败时用户数据已恢复，
        # 记录错误由调用方决定退出码 / 提示，重跑可补齐（导入幂等）
        try:
            from utils.knowledge_admin import import_documents

            kb_result = import_documents(knowledge)
            result["knowledge_imported"] = int(kb_result.get("imported", 0))
            result["knowledge_skipped"] = int(kb_result.get("skipped", 0))
        except FileNotFoundError:
            result["knowledge_error"] = (
                "知识库未初始化（先运行初始化脚本再恢复知识片段）"
            )
        except Exception as exc:  # noqa: BLE001 - 知识库失败不回滚用户数据
            result["knowledge_error"] = f"知识片段恢复失败：{exc}"
    return result
