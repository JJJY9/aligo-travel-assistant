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

from utils.identifiers import is_valid_web_id
from utils.timestamps import localize_naive, parse_iso_datetime
from utils.trip_import import _strict_date, trip_identity
import os

_MAX_PREFERENCES = 200
_MAX_TRIPS = 5000
_MAX_MESSAGES = 200000
_MAX_KNOWLEDGE = 50000
# 分片数硬上限（与 knowledge_admin._GROUP_PARTS_MAX 同一口径）：parts 声明
# 来自备份元数据，无上限时 set(range(1, parts + 1)) 会被 1e9 声明直接撑爆
# 内存（百字节备份即可触发 OOM）
_MAX_KNOWLEDGE_PARTS = 100000
# 单个备份文件的体积上限：整份读入内存之前先拒绝明显异常的输入
MAX_BACKUP_BYTES = 50 * 1024 * 1024

_USAGE_NUMBER_FIELDS = ("calls", "input_tokens", "output_tokens", "total_tokens")

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 时间戳年份范围：PostgreSQL 的 timestamptz 读写器拒绝公元 1 年之前 /
# 10000 年之后（带时区偏移的 0001 / 9999 换算后越界），会留下整用户不可读
# 的消息行；JSON 后端在 CLI 展示时也会 OverflowError。超出范围的按缺失处理。
_MIN_TIMESTAMP_YEAR = 1900
_MAX_TIMESTAMP_YEAR = 2100


def _normalize_timestamp(value: Any) -> str | None:
    """合法 ISO 时间戳归一成带时区形式；非法 / 缺失返回 None（按恢复时间落库）。"""
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    try:
        if parsed.tzinfo is None:
            parsed = localize_naive(parsed)
    except (OverflowError, OSError, ValueError):
        return None
    if not (_MIN_TIMESTAMP_YEAR <= parsed.year <= _MAX_TIMESTAMP_YEAR):
        return None
    return parsed.isoformat()


def _clean_session_id(value: Any) -> tuple[str | None, bool]:
    """返回 ``(session_id, 是否合法)``；缺失 / 空白按无会话处理。"""
    if value is None:
        return None, True
    text = str(value).strip()
    if not text:
        return None, True
    if not is_valid_web_id(text):
        # 与 Web 路径参数（pydantic-core 的 Rust 正则 ^[\w-]{1,64}$）同一
        # 字符集：Web 可以创建并导出含零宽连接符 / 组合符的会话，导入不能
        # 把它们丢掉；反过来也不能恢复出 Web 端点 422 打不开的 ID
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


def _json_text_problem(value: Any, depth: int = 0) -> str | None:
    """递归检查已解析 JSON 的键与字符串值中的 NUL / 孤立代理项。

    assistant 消息的原文门禁看到的是转义写法（``\\ud800`` 是若干 ASCII
    字符，UTF-8 可编码），解析后才是真正的孤立代理项：渲染 / 导出 / 总结
    路径对该消息每次编码都失败（会话查看永久 500）。转义形态必须在导入时
    按解析结果再查一遍。
    """
    if depth > _MAX_JSON_DEPTH:
        return "消息内容嵌套过深"
    if isinstance(value, str):
        return _text_problem(value)
    if isinstance(value, list):
        for item in value:
            problem = _json_text_problem(item, depth + 1)
            if problem:
                return problem
    elif isinstance(value, dict):
        for key, item in value.items():
            problem = _text_problem(str(key)) or _json_text_problem(item, depth + 1)
            if problem:
                return problem
    return None


def _bad_text_in_value(value: Any, depth: int = 0) -> str | None:
    """递归检查偏好值里的字符串（列表 / 对象内的字符串同样会落库）。

    带深度上限：偏好值只应为标量或一层列表，近乎递归上限的嵌套结构会让
    这里的递归先于 json.loads 的护栏抛出 RecursionError（CLI user-import
    直接崩溃、--json 也没有结构化输出），按非法值拒绝。
    """
    if depth > 64:
        return "嵌套过深"
    if isinstance(value, float):
        # 非有限数写进 JSONB 会被 PostgreSQL 确定性拒绝（整份恢复 503），
        # JSON 后端则落盘成非标准写法；PUT /api/preferences 已拒绝同类值
        if not math.isfinite(value):
            return "包含非有限数（NaN / Infinity）"
        return None
    if isinstance(value, str):
        return _text_problem(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            problem = _bad_text_in_value(item, depth + 1)
            if problem:
                return problem
    elif isinstance(value, dict):
        for key, item in value.items():
            problem = _text_problem(str(key)) or _bad_text_in_value(item, depth + 1)
            if problem:
                return problem
    return None


def _non_finite_in_value(value: Any, depth: int = 0) -> str | None:
    """递归检查任意值里的非有限浮点数（NaN / Infinity）。

    ``_meta`` 会原样返回给前端（会话详情），其中任意键（含 usage.time、
    schedule 字段）是非有限数时，响应序列化（allow_nan=False）会失败，
    该会话每次打开都 500。
    """
    if depth > 32:
        return "消息元数据嵌套过深"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "消息元数据含非有限数（NaN / Infinity）"
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            problem = _non_finite_in_value(item, depth + 1)
            if problem:
                return problem
    elif isinstance(value, dict):
        for item in value.values():
            problem = _non_finite_in_value(item, depth + 1)
            if problem:
                return problem
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
    except json.JSONDecodeError:
        # 不是合法 JSON 的普通文本：按旧格式原样保留
        return None
    except ValueError:
        # 超 4300 位整数字面量等在解析阶段抛普通 ValueError（不是
        # JSONDecodeError）：落库后 /api/status、会话查看 / 导出 / 总结
        # 对该消息永久失败，必须拒绝
        return "消息内容含超长数字字面量"
    # 转义形态（"\ud800" / "\u0000"）的 NUL / 孤立代理项在原文门禁里是
    # 可编码的 ASCII，只有解析后才暴露：渲染 / 导出 / 总结都会编码失败
    problem = _json_text_problem(data)
    if problem:
        return problem
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
            # 面板渲染对 null / 数字取 agent.name 会 TypeError，会话永久打不开
            return "消息智能体列表包含非法条目"
        duration = item.get("duration")
        if duration is None:
            continue
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return "智能体耗时不是数字"
        if isinstance(duration, float) and not math.isfinite(duration):
            return "智能体耗时不是有限数"
    schedule = meta.get("schedule")
    if schedule is not None and not isinstance(schedule, list):
        return "消息执行路线不是数组"
    for item in schedule or []:
        if not isinstance(item, dict):
            return "消息执行路线包含非法条目"
    elapsed = meta.get("elapsed")
    if elapsed is not None:
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            return "消息耗时不是数字"
        # 1e999 解析为 inf：/api/status 的 avg_elapsed 变 inf，JSON 序列化
        # （allow_nan=False）直接 500，且故障随消息永久存在
        if isinstance(elapsed, float) and not math.isfinite(elapsed):
            return "消息耗时不是有限数"
    usage = meta.get("usage")
    if usage is not None:
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
    # 已知字段之外的任意键（usage.time、schedule 数值等）同样会原样返回给
    # 前端：任何位置的 NaN / Infinity 都让会话详情响应序列化失败，最后兜底。
    # 无 usage 的消息同样要走这一步（此前 usage 缺失时提前 return，任意键的
    # NaN 漏检，落库后该会话每次打开 500）
    return _non_finite_in_value(meta)


def backup_limit_problem(backup: dict[str, Any]) -> str | None:
    """导出的备份是否超出导入侧上限（超出的备份永远无法恢复）。

    口径与 ``parse_user_export`` / ``import_user_data`` 的硬上限一致：
    条数与备份文件字节上限。返回具体原因或 None。
    """
    if not isinstance(backup, dict):
        return None
    messages = backup.get("chat_history")
    if isinstance(messages, list) and len(messages) > _MAX_MESSAGES:
        return f"消息 {len(messages)} 条超过导入上限 {_MAX_MESSAGES}"
    trips = backup.get("trip_history")
    if isinstance(trips, list) and len(trips) > _MAX_TRIPS:
        return f"行程 {len(trips)} 条超过导入上限 {_MAX_TRIPS}"
    preferences = backup.get("preferences")
    if isinstance(preferences, (dict, list)) and len(preferences) > _MAX_PREFERENCES:
        return f"偏好 {len(preferences)} 条超过导入上限 {_MAX_PREFERENCES}"
    knowledge = backup.get("knowledge")
    if isinstance(knowledge, list) and len(knowledge) > _MAX_KNOWLEDGE:
        return f"知识片段 {len(knowledge)} 条超过导入上限 {_MAX_KNOWLEDGE}"
    # 与导出实际写出的格式对齐：孤立代理项先转义（否则 encode 直接抛
    # UnicodeEncodeError），体积按 indent=2（以及 Windows 换行翻译）估算——
    # 紧凑 JSON 的估算会让 1.3-1.6 倍体积的备份通过自检、写盘后却被导入侧
    # 「超过 50 MB」拒绝，导出器产出自己声明可恢复、实际无法导入的备份
    from utils.json_safe import sanitize_json

    serialized = json.dumps(
        sanitize_json(backup), ensure_ascii=False, indent=2
    )
    size = len(serialized.encode("utf-8"))
    if os.linesep != "\n":
        size += serialized.count("\n")
    if size > MAX_BACKUP_BYTES:
        return (
            f"约 {size / (1024 * 1024):.1f} MB 超过导入上限 "
            f"{MAX_BACKUP_BYTES // (1024 * 1024)} MB"
        )
    return None


def parse_user_export(text: str) -> tuple[dict[str, Any], list[str]]:
    """解析并校验 user-export JSON 文本，返回 ``(可恢复数据, 错误列表)``。"""
    try:
        data = json.loads(text)
    except RecursionError:
        # 结构性深层嵌套（如 '['*3000）不是 ValueError：不捕获会让 CLI
        # --json 模式打印 traceback 而不是结构化错误
        return {}, ["JSON 嵌套过深，无法解析"]
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
        # 与 Web /api/preferences/{type} 的 ^[^/]{1,64}$ 约束一致：含 "/"
        # 的键落库后面板（路径段被拆开）与 CLI（字符集校验）都无法编辑或
        # 删除，只能重新导入覆盖值
        if not name or len(name) > 64 or "/" in name:
            errors.append(f"跳过第 {index} 个偏好：键名非法")
            continue
        if index > _MAX_PREFERENCES:
            errors.append(f"偏好超过 {_MAX_PREFERENCES} 个，多余条目已跳过")
            break
        problem = _text_problem(name) or _bad_text_in_value(value)
        if problem:
            errors.append(f"跳过第 {index} 个偏好：{problem}")
            continue
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
        start_raw = str(row.get("start_date") or "").strip()
        end_raw = str(row.get("end_date") or "").strip()
        date_problem = _text_problem(start_raw) or _text_problem(end_raw)
        if date_problem:
            errors.append(f"跳过第 {index} 条行程：{date_problem}")
            continue
        start_date = _normalize_trip_date(start_raw)
        if not start_date:
            errors.append(f"跳过第 {index} 条行程：出发日期为空")
            continue
        end_date = _normalize_trip_date(end_raw)
        start_dt = _strict_date(start_date)
        end_dt = _strict_date(end_date) if end_date else None
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            # 备份恢复以保真为先：规划路径可能存下颠倒的日期（历史数据，
            # Web PUT 也会拒绝编辑这类记录），整条丢弃等于备份静默丢行程；
            # 按原样保留并提示，恢复后可用导出 / 删除处理
            errors.append(
                f"第 {index} 条行程：结束日期早于出发日期（按原样保留）"
            )
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
        trip_record: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "start_date": start_date,
            "end_date": end_date,
            "purpose": purpose,
        }
        # 只在备份确实带有关联时写入 session_id：drop_sessions 显式置 None
        # 表示「清除关联」，与「备份本来没有关联」必须区分（否则恢复会
        # 清掉目标库里既有行程的会话链接）
        if ok and session_id:
            trip_record["session_id"] = session_id
        # 保留备份里的 trip_id / timestamp（导出格式两种后端都有）：恢复后
        # 编号与创建时间与备份一致，而不是全部重新编号为 trip_N / 恢复时刻
        trip_id_raw = str(row.get("trip_id") or "").strip()
        if re.fullmatch(r"trip_\d+", trip_id_raw):
            trip_record["trip_id"] = trip_id_raw
        record_timestamp = _normalize_timestamp(row.get("timestamp"))
        if record_timestamp:
            trip_record["timestamp"] = record_timestamp
        trips.append(trip_record)
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
    # 保持备份里的消息顺序：备份由导出按存储顺序写出，会话内提问 / 回答
    # 有序；全局按时间重排时无时间戳的旧消息会被排到末尾（恢复后会话里
    # 先显示回答再显示提问）。逐会话顺序由 add_chat_messages 原样追加
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
            cleaned_meta: dict[str, str] = {}
            meta_problem: str | None = ""
            for key, value in list(metadata.items())[:_MAX_PREFERENCES]:
                key_text = str(key)[:32]
                value_text = str(value)[:200]
                meta_problem = (
                    meta_problem
                    or _text_problem(key_text)
                    or _text_problem(value_text)
                )
                cleaned_meta[key_text] = value_text
            if meta_problem:
                errors.append(f"跳过第 {index} 个知识片段：{meta_problem}")
                if group:
                    invalid_groups.add(group)
                continue
            knowledge.append(
                {"content": content, "metadata": cleaned_meta}
            )
        if len(raw_knowledge) > _MAX_KNOWLEDGE:
            errors.append(f"知识片段超过 {_MAX_KNOWLEDGE} 条，多余条目已跳过")
            # 截断点可能落在多分片 group 中间：残缺组会让 import_documents
            # 的组完整性校验拒绝整份恢复——把被截断的组整组丢弃
            kept_counts: Counter[str] = Counter()
            declared: dict[str, int] = {}
            for entry in knowledge:
                meta = entry["metadata"]
                group = str(meta.get("group") or "")
                if not group:
                    continue
                kept_counts[group] += 1
                try:
                    declared[group] = max(
                        declared.get(group, 1), int(meta.get("parts") or 1)
                    )
                except (TypeError, ValueError):
                    declared[group] = declared.get(group, 1)
            truncated_groups = {
                group
                for group, count in kept_counts.items()
                if declared.get(group, count) > count
            }
            if truncated_groups:
                knowledge = [
                    entry
                    for entry in knowledge
                    if str(entry["metadata"].get("group") or "")
                    not in truncated_groups
                ]
                errors.append(
                    f"截断导致 {len(truncated_groups)} 个知识分片组不完整，已整组跳过"
                )
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
    # 结构校验：part 编号重复 / 缺号 / 与 parts 声明不符的组整组丢弃。
    # 否则 import_documents 的分组完整性校验会抛 ValueError，整份知识段
    # （包括无关的合法文档）都恢复不了。
    broken_groups: set[str] = set()
    oversized_groups: set[str] = set()
    group_declared: dict[str, int] = {}
    group_parts: dict[str, list[int]] = {}
    for entry in knowledge:
        meta = entry["metadata"]
        group = str(meta.get("group") or "")
        if not group:
            continue
        try:
            parts = int(meta.get("parts") or 1)
        except (TypeError, ValueError, OverflowError):
            parts = 1
        if parts <= 1:
            continue
        group_declared[group] = max(group_declared.get(group, 1), parts)
        try:
            part = int(meta.get("part") or 0)
        except (TypeError, ValueError, OverflowError):
            part = 0
        group_parts.setdefault(group, []).append(part)
    for group, declared_parts in group_declared.items():
        if declared_parts > _MAX_KNOWLEDGE_PARTS:
            # 声明数异常（损坏 / 恶意备份）：不能先构造 range 再判断，
            # 1e9 的 parts 会让 set(range(...)) 分配数十 GB 内存
            oversized_groups.add(group)
            broken_groups.add(group)
            continue
        seen = group_parts.get(group, [])
        if len(seen) != len(set(seen)) or set(seen) != set(
            range(1, declared_parts + 1)
        ):
            broken_groups.add(group)
    if broken_groups:
        knowledge = [
            entry
            for entry in knowledge
            if str(entry["metadata"].get("group") or "") not in broken_groups
        ]
        duplicate_or_missing = len(broken_groups) - len(oversized_groups)
        if duplicate_or_missing:
            errors.append(
                f"跳过 {duplicate_or_missing} 个知识分片组：分片编号重复或缺失"
            )
        if oversized_groups:
            errors.append(
                f"跳过 {len(oversized_groups)} 个知识分片组：分片数声明异常"
                f"（超过 {_MAX_KNOWLEDGE_PARTS}）"
            )

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


def drop_sessions(sections: dict[str, Any], session_ids: set[str]) -> None:
    """从备份 sections 中剔除给定会话（原位修改）。

    消息 / 标题 / 置顶 / 归档一并删除，行程只清空关联的 ``session_id``
    （行程本身是独立数据，保留）。Web 的 /api/import 与 CLI 的 user-import
    用同一实现处理墓碑期已删除与保留字会话。
    """
    if not session_ids:
        return
    sections["messages"] = [
        message
        for message in sections.get("messages") or []
        if message.get("session_id") not in session_ids
    ]
    sections["titles"] = {
        session_id: title
        for session_id, title in (sections.get("titles") or {}).items()
        if session_id not in session_ids
    }
    sections["pinned"] = [
        session_id
        for session_id in sections.get("pinned") or []
        if session_id not in session_ids
    ]
    sections["archived"] = [
        session_id
        for session_id in sections.get("archived") or []
        if session_id not in session_ids
    ]
    for trip in sections.get("trips") or []:
        if trip.get("session_id") in session_ids:
            trip["session_id"] = None


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