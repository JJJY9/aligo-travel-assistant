"""用户数据备份恢复（``user-export`` / ``/api/export`` JSON 的逆操作）。

恢复偏好、行程、聊天消息与会话元数据（标题 / 置顶 / 归档）。统计字段由
存储层在写入时自行累计，不直接覆盖；非法条目收集为错误而不是中断整个
文件。重复恢复幂等：行程按存储层「同路线 + 同起止日期」去重，聊天消息按
（会话, 角色, 内容）多重集去重（重复导入同一备份不会追加重复消息）。

时间戳统一归一成带时区 ISO（naive 按本机时区解释），写入 TIMESTAMPTZ 不会
发生时区偏移；缺失 / 非法时间戳的消息沿用同会话相邻消息的时间。消息批量
写入（单次存储层写锁 / 单事务）并按时间戳并入全局历史位置；提问已在目标
库中的轮次跳过备份里的回答（本地优先，见 :mod:`utils.chat_import`）。
"""
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime
from typing import Any

from utils.chat_import import filter_import_messages, prepare_import_messages
from utils.errors import MemoryPersistenceError
from utils.identifiers import is_valid_web_session_id
from utils.json_safe import MAX_JSON_DEPTH
from utils.timestamps import localize_naive, parse_iso_datetime
from utils.trip_import import (
    _strict_date,
    normalize_identity_date,
    trip_identity,
)
import os

logger = logging.getLogger(__name__)

_MAX_PREFERENCES = 200
_MAX_TRIPS = 5000
_MAX_MESSAGES = 200000
_MAX_KNOWLEDGE = 50000
# 备份 trip_id 的数字上限：PostgreSQL 编号计数器是 BIGINT，JSON 后端用
# Python int 换算——19 位以上（> 2**63-1）的编号会让编号水位永久溢出，
# 该用户之后新增行程全部写不进去；超过 18 位的一律丢弃编号、按新编号恢复
_MAX_TRIP_ID_DIGITS = 18
# 分片数硬上限（与 knowledge_admin._GROUP_PARTS_MAX 同一口径）：parts 声明
# 来自备份元数据，无上限时 set(range(1, parts + 1)) 会被 1e9 声明直接撑爆
# 内存（百字节备份即可触发 OOM）
_MAX_KNOWLEDGE_PARTS = 100000
# 单个备份文件的体积上限：整份读入内存之前先拒绝明显异常的输入
MAX_BACKUP_BYTES = 50 * 1024 * 1024

_USAGE_NUMBER_FIELDS = ("calls", "input_tokens", "output_tokens", "total_tokens")

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_session_key(value: Any) -> str | None:
    """消息去重键的会话维度：与存储层 ``_session_dedupe_key`` 同口径。

    旧 JSON 数据里 session_id 可能是整数（123），解析后的备份是字符串
    （"123"）：不归一会让 dry-run 预览与实际导入的 messages_added 不一致。
    """
    return None if value is None else str(value)


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
    if not is_valid_web_session_id(text):
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


def _content_beyond_export_depth(value: Any, depth: int = 0) -> bool:
    """解析后的非空内容是否落进导出截断区（绝对深度 ≥ ``MAX_JSON_DEPTH``）。

    导出 / 状态渲染用 ``sanitize_json``：绝对深度到 ``MAX_JSON_DEPTH`` 的
    节点一律替换成 None。导入若接受更深的**非空**内容，下一次导出会把
    它们静默截掉——备份链丢数据，而用户无法感知。已导出的备份在该深度
    只剩 null（上一轮截断的产物），继续可导入，保证导出 → 恢复闭环。
    ``depth`` 是 value 根在导出树中的绝对深度（assistant JSON 根为 0；
    偏好值位于 ``{"preferences": {name: value}}`` 下为 2）。
    """
    if value is None:
        return False
    if depth >= MAX_JSON_DEPTH:
        return True
    if isinstance(value, list):
        return any(
            _content_beyond_export_depth(item, depth + 1) for item in value
        )
    if isinstance(value, dict):
        return any(
            _content_beyond_export_depth(item, depth + 1)
            for item in value.values()
        )
    return False


# 偏好值在导出树中的绝对根深度：user_data["preferences"][name] 的 value
_PREFERENCE_EXPORT_DEPTH = 2


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
    if _content_beyond_export_depth(data):
        return "消息内容嵌套过深（超过导出保存深度）"
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


def _count_limit_problem(backup: dict[str, Any]) -> str | None:
    """条数轴上限（与导入侧硬上限完全一致）；体积轴另有口径。"""
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
    return None


def _backup_payload_size(backup: dict[str, Any]) -> int:
    """按导出实际写出的格式估算体积（indent=2，Windows 换行翻译）。"""
    # 孤立代理项先转义（否则 encode 直接抛 UnicodeEncodeError）——
    # 紧凑 JSON 的估算会让 1.3-1.6 倍体积的备份通过自检、写盘后却被导入侧
    # 「超过 50 MB」拒绝，导出器产出自己声明可恢复、实际无法导入的备份
    from utils.exporter import sanitize_backup_data

    serialized = json.dumps(
        sanitize_backup_data(backup), ensure_ascii=False, indent=2
    )
    size = len(serialized.encode("utf-8"))
    if os.linesep != "\n":
        size += serialized.count("\n")
    return size


def backup_limit_problem(backup: dict[str, Any]) -> str | None:
    """导出的备份是否超出导入侧上限（超出的备份永远无法恢复）。

    口径与 ``parse_user_export`` / ``import_user_data`` 的硬上限一致：
    条数与备份文件字节上限。返回具体原因或 None。
    """
    if not isinstance(backup, dict):
        return None
    problem = _count_limit_problem(backup)
    if problem is not None:
        return problem
    size = _backup_payload_size(backup)
    if size > MAX_BACKUP_BYTES:
        return (
            f"约 {size / (1024 * 1024):.1f} MB 超过导入上限 "
            f"{MAX_BACKUP_BYTES // (1024 * 1024)} MB"
        )
    return None


def merged_backup_limit_problem(
    store: Any, sections: dict[str, Any]
) -> str | None:
    """恢复后「账号现有数据 + 备份」按导出格式是否仍可能超限。

    ``backup_limit_problem`` 只看备份自身：小备份恢复到接近上限的账号后，
    账号仍会因导出体积永久 413。这里把现有数据与备份合并后再复核。

    合并按导入层的去重语义预演：备份里与现有数据重复的消息 / 行程会被
    跳过或更新，不进入合并结果——直接用「现有 + 备份」拼接会让重导自己
    备份的账号组合体积约为两倍，接近上限时被误判为恢复后超限（幂等恢复
    失效、--dry-run 与真实执行结论相反）。
    """
    if not isinstance(sections, dict):
        return None
    try:
        if callable(getattr(store, "export_snapshot", None)):
            existing = store.export_snapshot() or {}
        else:
            existing = {
                "preferences": store.get_preference() or {},
                "trip_history": store.get_trip_history(limit=None) or [],
                "chat_history": store.get_chat_history() or [],
                "session_titles": store.get_session_titles() or {},
                "pinned_sessions": store.get_pinned_sessions() or [],
                "archived_sessions": store.get_archived_sessions() or [],
            }
    except Exception:  # noqa: BLE001 - 读不到现存数据时退回只查备份自身
        return sections_limit_problem(
            sections, pg_shape=not getattr(store, "snapshot_based", False)
        )
    merged_prefs = (
        dict(existing.get("preferences") or {})
        if isinstance(existing.get("preferences"), dict)
        else {}
    )
    incoming_prefs = sections.get("preferences")
    if isinstance(incoming_prefs, dict):
        merged_prefs.update(incoming_prefs)
    existing_titles = existing.get("session_titles")
    existing_titles = existing_titles if isinstance(existing_titles, dict) else {}
    incoming_titles = sections.get("titles")
    merged_titles = dict(existing_titles)
    if isinstance(incoming_titles, dict):
        merged_titles.update(incoming_titles)
    existing_messages = list(existing.get("chat_history") or [])
    existing_trip_rows = list(existing.get("trip_history") or [])
    pg_shape = not getattr(store, "snapshot_based", False)
    # 以现有快照为底，再覆盖为合并后的各段；真实导出端会在
    # export_snapshot 之外补 user_id / exported_at——这里放等长占位，
    # 体积口径与导出端一致
    payload = dict(existing)
    # 占位与导出端形状等长：user_id 允许 64 字符、时间戳带微秒、
    # CLI 导出恒写 session_id 空串
    payload.setdefault("user_id", "u" * 64)
    payload.setdefault("exported_at", "1970-01-01T00:00:00.000000")
    payload.setdefault("session_id", "")
    payload.update(
        {
            "chat_history": existing_messages
            + _new_message_rows(
                existing_messages,
                sections.get("messages") or [],
                pg_shape=not getattr(store, "snapshot_based", False),
            ),
            "trip_history": _merged_trip_rows(
                existing_trip_rows,
                sections.get("trips") or [],
                include_empty_keys=pg_shape,
                existing_last_number=_existing_trip_number_watermark(
                    store, existing, pg_shape=pg_shape
                ),
            ),
            "preferences": merged_prefs,
            "knowledge": _baseline_aware_knowledge_rows(existing, sections)
            or [],
            "session_titles": merged_titles,
            "pinned_sessions": sorted(
                {
                    str(item)
                    for item in list(existing.get("pinned_sessions") or [])
                    + list(sections.get("pinned") or [])
                }
            ),
            "archived_sessions": sorted(
                {
                    str(item)
                    for item in list(existing.get("archived_sessions") or [])
                    + list(sections.get("archived") or [])
                }
            ),
        }
    )
    # 重算 statistics：导入自身会增长 total_*、并为每个新目的地复制一份
    # 完整目的地文本进 frequent_destinations——沿用导入前快照会系统性
    # 低估恢复后的真实导出体积（接近上限时放行「导入后永久 413」的恢复）
    statistics = dict(payload.get("statistics") or {})
    merged_trips = payload.get("trip_history") or []
    merged_messages = payload.get("chat_history") or []
    statistics["total_trips"] = len(merged_trips)
    statistics["total_messages"] = len(merged_messages)
    frequent: dict[str, int] = {}
    for trip in merged_trips:
        if not isinstance(trip, dict):
            continue
        destination = trip.get("destination")
        if not destination:
            continue
        try:
            datetime.strptime(str(trip.get("start_date") or ""), "%Y-%m-%d")
        except ValueError:
            continue
        key = str(destination)
        frequent[key] = frequent.get(key, 0) + 1
    existing_frequent = statistics.get("frequent_destinations")
    merged_frequent: dict[str, int] = {}
    if isinstance(existing_frequent, dict):
        for key, value in existing_frequent.items():
            try:
                merged_frequent[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                merged_frequent[str(key)] = 0
    for key, value in frequent.items():
        merged_frequent[key] = max(merged_frequent.get(key, 0), value)
    # 真实导出恒含该键（空 dict 也序列化），同形状计入体积
    statistics["frequent_destinations"] = merged_frequent
    trip_numbers = [
        int(match.group(1))
        for match in (
            re.fullmatch(r"trip_(\d+)", str(trip.get("trip_id") or ""))
            for trip in merged_trips
            if isinstance(trip, dict)
        )
        if match
    ]
    created_trip_count = max(0, len(merged_trips) - len(existing_trip_rows))
    if merged_trips and not pg_shape:
        # 写入层对每个新建行至少推进一次水位；冲突改号行还能把水位推到
        # 18 位以上——取「现有水位 / 最大编号 / 新建行数」的保守上界。
        # PG 的 get_statistics 没有该字段，保持后端真实导出形状
        existing_last = _existing_last_trip_number(existing) or 0
        water = max([existing_last, *trip_numbers], default=existing_last)
        statistics["last_trip_number"] = water + created_trip_count
    payload["statistics"] = statistics
    problem = backup_limit_problem(payload)
    if problem is None:
        return None
    # 用户数据轴的导入前基线：存量账号自身（不含知识库）按导出格式已
    # 超限时——正常聊天积累到 50MB 以上的账号——任何恢复都无法让它更糟，
    # 体积轴不再作为拒绝理由（否则该账号连幂等重导自身备份都会被 413，
    # 完全无法换机 / 恢复）。条数轴照常生效：把账号推过条数上限的备份
    # （导入后永远无法导出）仍然拒绝。
    baseline = _export_shape_baseline(existing, [])
    if (
        backup_limit_problem(baseline) is not None
        and _count_limit_problem(payload) is None
    ):
        return None
    return problem


def _new_message_rows(
    existing_rows: list[Any],
    messages: list[Any],
    *,
    pg_shape: bool = False,
) -> list[Any]:
    """按写入层去重语义挑出备份里真正会新增的消息行。

    复用合并复核已经取得的现有快照（不再整表重读，避免后台两次读取
    不是同一快照）；快照读不到时调用方退回只查备份自身。

    缺时间戳且同会话无锚点的消息按落库后的导出形状占位：PostgreSQL
    落库经 ``_pg_timestamp`` 带时区偏移（32 字符），按 naive 26 字符
    计会低估导出体积（上限附近可放行「恢复后永久 413」的备份）。
    """
    if not messages:
        return []
    fallback = (
        datetime.now().astimezone().isoformat(timespec="microseconds")
        if pg_shape
        else datetime.now().isoformat(timespec="microseconds")
    )
    existing = Counter(
        (
            _message_session_key(message.get("session_id")),
            message.get("role"),
            str(message.get("content") or ""),
        )
        for message in existing_rows
        if isinstance(message, dict)
    )
    return list(
        filter_import_messages(
            prepare_import_messages(messages, now=fallback), existing
        )
    )


def _trip_merge_key(row: dict[str, Any]) -> Any:
    """与 ``save_trip_histories`` 的索引口径一致：缺目的地 / 出发日期的
    行不参与去重（返回 None，存储层每行都会新增）。"""
    destination = row.get("destination")
    start = normalize_identity_date(row.get("start_date"))
    if not destination or not start:
        return None
    return trip_identity(row)


def _existing_knowledge_rows() -> list[Any]:
    """知识库现有片段（合并体积复核用）；未初始化返回空列表。

    知识库未初始化不构成「恢复后无法导出」风险：导入时知识段会以
    ``knowledge_error`` 上报，用户数据照常恢复。
    """
    from utils.knowledge_admin import export_documents, knowledge_db_path

    try:
        if not knowledge_db_path().exists():
            return []
        return list(export_documents() or [])
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - 交给合并预检按知识轴跳过
        raise MemoryPersistenceError(
            f"读取知识库现有片段失败：{exc}"
        ) from exc


def _planned_knowledge_rows(sections: dict[str, Any]) -> list[Any] | None:
    """本次恢复真正会写入的知识分片（导入层 dry-run 规划）。

    与真实 ``import_documents`` 同一实现（分组还原 / 去重 / 切分后的
    最终形状）：预演与落库逐字节同源。返回 None 表示知识轴不可评估
    （规划失败）：调用方跳过知识体积轴、不阻断用户数据恢复（导入层会
    把知识段以 ``knowledge_error`` 上报）。
    """
    rows = sections.get("knowledge") or []
    if not rows:
        return []
    try:
        from utils.knowledge_admin import import_documents

        plan = import_documents(rows, dry_run=True, return_pieces=True)
    except Exception as exc:  # noqa: BLE001 - 知识失败不阻断用户数据恢复
        logger.warning("合并预检：知识库规划失败（%s），跳过知识体积轴", exc)
        return None
    return list(plan.get("pieces") or [])


def _export_shape_baseline(
    existing: dict[str, Any], knowledge_rows: list[Any]
) -> dict[str, Any]:
    """导入前的完整导出形状（现有存量 + 现有知识库）。

    知识轴是否允许作为拒绝理由，取决于「恢复前」账号本身是否仍可导出
    WITH-knowledge 备份：已超限的账号不可能被恢复推得更糟。
    """
    payload = dict(existing)
    payload.setdefault("user_id", "u" * 64)
    payload.setdefault("exported_at", "1970-01-01T00:00:00.000000")
    payload.setdefault("session_id", "")
    payload.update(
        {
            "chat_history": list(existing.get("chat_history") or []),
            "trip_history": list(existing.get("trip_history") or []),
            "preferences": existing.get("preferences") or {},
            "knowledge": list(knowledge_rows),
            "session_titles": existing.get("session_titles") or {},
            "pinned_sessions": list(existing.get("pinned_sessions") or []),
            "archived_sessions": list(
                existing.get("archived_sessions") or []
            ),
            "statistics": existing.get("statistics") or {},
        }
    )
    return payload


def _baseline_aware_knowledge_rows(
    existing: dict[str, Any], sections: dict[str, Any]
) -> list[Any] | None:
    """合并预检的知识段；None = 跳过知识轴（仅保留用户数据轴）。

    - 现有知识库不可读 / 本次规划失败：跳过知识轴（不阻断用户数据）；
    - 「现有存量 + 现有知识库」本已超过导出上限：跳过知识轴——任何恢复
      都无法让它更糟，不能据此拒绝用户数据（隔离账号的 user-import 是
      唯一产品内恢复路径）；用户数据轴仍由最终 payload 复核；
    - 否则计入「现有库 + 本次新增分片」，恢复把合规库推过上限时照常拒绝。
    """
    try:
        existing_kb = _existing_knowledge_rows()
    except MemoryPersistenceError as exc:
        logger.warning("合并预检：读取现有知识库失败（%s），跳过知识体积轴", exc)
        return None
    planned = _planned_knowledge_rows(sections)
    if planned is None:
        return None
    if not existing_kb and not planned:
        return []
    baseline_problem = backup_limit_problem(
        _export_shape_baseline(existing, existing_kb)
    )
    if baseline_problem is not None:
        logger.warning(
            "合并预检：导入前数据与知识库已超过导出上限，跳过知识体积轴"
        )
        return None
    return existing_kb + planned


def _existing_last_trip_number(existing: dict[str, Any]) -> int | None:
    """已有快照里的编号水位（statistics.last_trip_number）。

    缺省 / 非法返回 None（未知）：PG 的快照不带该字段，未知时调用方
    必须按最坏情况占位，不能当成 0。
    """
    statistics = existing.get("statistics")
    if not isinstance(statistics, dict):
        return None
    value = statistics.get("last_trip_number")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _existing_trip_number_watermark(
    store: Any, existing: dict[str, Any], *, pg_shape: bool
) -> int:
    """预演起算编号：JSON 统计水位 / PG 计数器 / 未知时最坏占位。

    PG 的计数器（aligo_trip_counters.last_number）不会随行程删除回落，
    快照 statistics 又不带 last_trip_number：读得到就用真实值，读不到
    按 BIGINT 上限（19 位数字，trip_ 前缀共 24 字符）占位——预演绝不
    低估真实落库后的 trip_id 长度。
    """
    watermark = _existing_last_trip_number(existing)
    getter = getattr(store, "get_trip_number_watermark", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - 读水位失败按未知处理
            value = None
        if value is not None:
            watermark = max(watermark or 0, int(value))
    if watermark is not None:
        return watermark
    # 未知：JSON 从 0 起号；PG 计数器可到 BIGINT 上限
    return 10**18 - 1 if pg_shape else 0


def _merged_trip_rows(
    existing_rows: list[Any],
    trips: list[Any],
    *,
    include_empty_keys: bool = False,
    existing_last_number: int | None = None,
) -> list[Any]:
    """恢复后行程表的形状：按 ``save_trip_histories`` 的索引语义预演。

    - 同身份只有第一条既有行被更新（存储层 ``index.setdefault``），更新
      purpose / session_id / 时间戳；其余同身份历史行保持原样；
    - 同批重复的新行先建后更新：后行的 purpose / session_id 覆盖前行，
      前行其余字段保留（新纪录是整行落库，只回填 purpose 会低估体积）；
    - 缺目的地 / 出发日期的行不参与去重，逐条新增。
    """
    fallback_timestamp = (
        datetime.now().astimezone().isoformat(timespec="microseconds")
        if include_empty_keys
        else datetime.now().isoformat(timespec="microseconds")
    )
    base = [dict(row) if isinstance(row, dict) else row for row in existing_rows]
    index: dict[Any, int] = {}
    used_trip_ids: set[str] = set()
    max_number = int(existing_last_number or 0)
    for idx, row in enumerate(base):
        if isinstance(row, dict):
            trip_id_text = str(row.get("trip_id") or "")
            if trip_id_text:
                used_trip_ids.add(trip_id_text)
            match = re.fullmatch(r"trip_(\d+)", trip_id_text)
            if match:
                max_number = max(max_number, int(match.group(1)))
            key = _trip_merge_key(row)
            if key is not None:
                index.setdefault(key, idx)
    created: list[dict[str, Any]] = []
    created_index: dict[Any, int] = {}
    for row in trips:
        if not isinstance(row, dict):
            continue
        key = _trip_merge_key(row)
        if key is not None and key in index:
            target = index[key]
            base[target] = _merge_trip_row(
                base[target], row, fallback_timestamp
            )
            continue
        if key is not None and key in created_index:
            target = created_index[key]
            created[target] = _merge_trip_row(
                created[target], row, fallback_timestamp
            )
            continue
        if key is not None:
            created_index[key] = len(created)
        record = dict(row)
        # drop_sessions 的内部标记不会落库；存储层为新建行按编号仲裁
        # 补写 trip_id——预演必须复刻同一规则（先自增水位，备份编号未被
        # 占用时可沿用并抬高水位，否则写成 trip_<水位>），否则冲突改号
        # 的行会按备份里的短编号低估导出体积
        record.pop(_DROPPED_SESSION_KEY, None)
        max_number += 1
        backup_id = str(record.get("trip_id") or "")
        match = re.fullmatch(r"trip_(\d+)", backup_id)
        if match and backup_id not in used_trip_ids:
            max_number = max(max_number, int(match.group(1)))
        else:
            record["trip_id"] = f"trip_{max_number}"
        used_trip_ids.add(str(record["trip_id"]))
        record.setdefault("timestamp", fallback_timestamp)
        if include_empty_keys:
            # PostgreSQL 后端的导出行带 purpose / session_id 键（空值也
            # 序列化）；JSON 后端没有这两个键
            record.setdefault("purpose", None)
            record.setdefault("session_id", None)
        created.append(record)
    return base + created


def _merge_trip_row(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    fallback_timestamp: str = "",
) -> dict[str, Any]:
    """导入层对同身份行程的更新语义（与 ``save_trip_histories`` 一致）：
    purpose 非 None 覆盖、session_id 键存在即覆盖（含显式 None）、时间戳
    无条件刷新（``row.get("timestamp") or now``），其余字段保留。"""
    merged = dict(existing)
    purpose = incoming.get("purpose")
    if purpose is not None:
        merged["purpose"] = purpose
    if "session_id" in incoming:
        merged["session_id"] = incoming.get("session_id")
    timestamp = incoming.get("timestamp")
    if timestamp:
        merged["timestamp"] = timestamp
    elif fallback_timestamp:
        # 存储层更新行无条件刷新时间戳：预演同样补占位，导出形状
        # （含时间戳键）才与落库后一致，避免体积低估
        merged["timestamp"] = fallback_timestamp
    return merged


def _export_shape_messages(
    messages: list[Any], *, pg_shape: bool = False
) -> list[Any]:
    """备份自身体积预检也按落库后的导出行形状计。

    缺 / 空时间戳的行落库后导出为时间戳（PG 32 字符带时区；JSON 26
    字符 naive），按 ``"timestamp": null``（4 字节）会低估导出体积。
    后端形状必须区分：对纯 JSON 部署按 PG 形状会把可恢复的备份每条
    多算 6 字节（20 万条约 1.2MB）误拒。
    """
    fallback = (
        datetime.now().astimezone().isoformat(timespec="microseconds")
        if pg_shape
        else datetime.now().isoformat(timespec="microseconds")
    )
    # 复用写入层同一时间戳模型：同会话有邻居的缺时间戳消息会拿邻居的
    # UTC 键（32 字符），与落库 / 导出逐字节一致；无锚点行走后端形状占位
    dict_rows = [message for message in messages if isinstance(message, dict)]
    prepared = iter(prepare_import_messages(dict_rows, now=fallback))
    rows: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            rows.append(message)
            continue
        row = next(prepared)
        timestamp = row.get("timestamp")
        if not pg_shape and isinstance(timestamp, str) and len(timestamp) > 26:
            # 解析把 naive 时间戳归一成带偏移的 32 字符，但 JSON 后端
            # 幂等重导时重复行不落库、真实导出仍是 26 字符：预检按 26
            # 估算（新建行走权威合并复核，不会被低估放行）
            row = {**row, "timestamp": timestamp[:26]}
        rows.append(row)
    return rows


def sections_limit_problem(
    sections: dict[str, Any], *, pg_shape: bool = False
) -> str | None:
    """恢复后账号是否仍能导出全量备份（按导出格式复核条数与体积）。

    CLI 导入此前只按原始文件字节做 50MB 预检：紧凑 JSON 备份可以通过并
    恢复成功，随后 user-export 按 indent=2 估算体积超限、永久拒绝导出——
    用户必须先删数据才能再备份。这里把可恢复段按导出形状重新组装后复用
    :func:`backup_limit_problem` 的同一口径（与 Web /api/import 一致）。
    """
    if not isinstance(sections, dict):
        return None
    # 与 merged_backup_limit_problem 同一导出形状（空存量）：行程走编号
    # 仲裁（同身份重复折叠、补 trip_id/timestamp、PG 空键），会话元数据
    # 与重算统计一并计入——手搭的窄字段 payload 会低估同内容的真实导出，
    # 让 CLI / Web 把实际可恢复的备份判成超限
    # 起号从 0 开始：独立预检没有 store、拿不到 PG 计数器，而后续的
    # 权威合并复核会用真实水位（读不到时按最坏占位 fail-closed）——
    # 预检按最坏占位会比权威复核更严，把实际可恢复的备份误拒
    trip_rows = _merged_trip_rows(
        [],
        sections.get("trips") or [],
        include_empty_keys=pg_shape,
        existing_last_number=0,
    )
    messages = _export_shape_messages(
        sections.get("messages") or [], pg_shape=pg_shape
    )
    payload: dict[str, Any] = {
        "user_id": "u" * 64,
        "exported_at": "1970-01-01T00:00:00.000000",
        "session_id": "",
        "chat_history": messages,
        "trip_history": trip_rows,
        "preferences": sections.get("preferences") or {},
        "knowledge": sections.get("knowledge") or [],
        "session_titles": sections.get("titles") or {},
        "pinned_sessions": sections.get("pinned") or [],
        "archived_sessions": sections.get("archived") or [],
    }
    statistics: dict[str, Any] = {
        "total_trips": len(trip_rows),
        "total_messages": len(messages),
        "total_queries": 0,
    }
    frequent: dict[str, int] = {}
    for trip in trip_rows:
        if not isinstance(trip, dict):
            continue
        destination = trip.get("destination")
        if not destination:
            continue
        try:
            datetime.strptime(str(trip.get("start_date") or ""), "%Y-%m-%d")
        except ValueError:
            continue
        key = str(destination)
        frequent[key] = frequent.get(key, 0) + 1
    statistics["frequent_destinations"] = frequent
    numbers = [
        int(match.group(1))
        for match in (
            re.fullmatch(r"trip_(\d+)", str(trip.get("trip_id") or ""))
            for trip in trip_rows
            if isinstance(trip, dict)
        )
        if match
    ]
    if not pg_shape:
        # JSON 后端的统计恒含该键（空账号也序列化）；PG 的 get_statistics
        # 没有此字段，保持后端真实形状
        statistics["last_trip_number"] = (
            max(numbers, default=0) + len(trip_rows)
            if trip_rows
            else 0
        )
    payload["statistics"] = statistics
    return backup_limit_problem(payload)


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
        # 上限先于任何 continue：此前非法键直接跳过、永远到不了 break，
        # 200 条上限可被任意数量的非法键完全绕过
        if index > _MAX_PREFERENCES:
            errors.append(f"偏好超过 {_MAX_PREFERENCES} 个，多余条目已跳过")
            break
        # 键名与 Web /api/preferences/{type} 的 ^[^/]{1,64}$ 同一口径，并
        # 排除控制字符 / 行分隔符（与 _PREF_TYPE_PATTERN 一致）：不做 strip
        # ——改名会让「 home」与「home」碰撞互相覆盖，含 ESC/NUL 的键导入
        # 后面板与 API 都无法编辑或删除
        name = str(key)
        if (
            not name
            or len(name) > 64
            or re.search(r"[\x00-\x1F\x7F-\x9F\u2028\u2029/]", name)
        ):
            errors.append(f"跳过第 {index} 个偏好：键名非法")
            continue
        problem = _text_problem(name) or _bad_text_in_value(value)
        if problem:
            errors.append(f"跳过第 {index} 个偏好：{problem}")
            continue
        if _content_beyond_export_depth(value, _PREFERENCE_EXPORT_DEPTH):
            errors.append(
                f"跳过第 {index} 个偏好：嵌套过深（超过导出保存深度）"
            )
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
        # 备份缺失 / 空的目的不能补默认值：存储层对既有行程按「非 None」
        # 覆盖 purpose，补默认「出差」会把用户编辑过的目的静默改掉。
        # 键缺失即保留原值（新行程由展示层按缺省处理）
        purpose = str(row.get("purpose") or "").strip()
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
        }
        if purpose:
            trip_record["purpose"] = purpose
        # 只在备份确实带有关联时写入 session_id：drop_sessions 显式置 None
        # 表示「清除关联」，与「备份本来没有关联」必须区分（否则恢复会
        # 清掉目标库里既有行程的会话链接）
        if ok and session_id:
            trip_record["session_id"] = session_id
        # 保留备份里的 trip_id / timestamp（导出格式两种后端都有）：恢复后
        # 编号与创建时间与备份一致，而不是全部重新编号为 trip_N / 恢复时刻。
        # 数字位数必须设上限：BIGINT 计数器 / int 换算会被超大编号永久毒化
        trip_id_raw = str(row.get("trip_id") or "").strip()
        if re.fullmatch(r"trip_\d{1,%d}" % _MAX_TRIP_ID_DIGITS, trip_id_raw):
            trip_record["trip_id"] = trip_id_raw
        elif trip_id_raw:
            errors.append(
                f"第 {index} 条行程的 trip_id 超出可恢复范围，已按新编号恢复"
            )
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
    # 先显示回答再显示提问）。存储层按时间戳把恢复消息并入全局历史
    # （缺失时间戳的沿用同会话相邻消息的时间），备份顺序决定同一时刻
    # 消息的先后。
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
        # 集合去重：恢复的置顶 / 归档列表可能很长（&~上万条），
        # list membership 会让解析变成 O(n²)、导入挂起
        seen: set[str] = set()
        for item in _as_list(data.get(name)):
            session_id = str(item or "").strip()
            if session_id and session_id in session_ids and session_id not in seen:
                seen.add(session_id)
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
        seen_set = set(seen)
        # 完整性判定不走 set(range(1, declared_parts+1))：大量小组合法声明
        # parts=上限值时，range 集合构造会累积成聚合型 DoS。distinct 值数量
        # 等于声明数、且最小值 1 / 最大值等于声明数，由鸽巢原理即恰好覆盖
        # 1..declared_parts
        complete = (
            len(seen) == declared_parts
            and len(seen_set) == declared_parts
            and min(seen_set) == 1
            and max(seen_set) == declared_parts
        )
        if len(seen) != len(seen_set) or not complete:
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
    # --dry-run 与真实执行同一上限口径：预览成功、真跑失败会让恢复脚本
    # 误判（此前上限只看备份条数，重复导入的大账号两边结论相反）
    _enforce_import_limits(store, sections)
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

    # 与写入层同一去重口径：缺目的地 / 出发日期的行不参与去重，逐条新增
    # （裸 trip_identity 会把它们折叠，--dry-run 与真实导入结论相反）
    try:
        history_trips = store.get_trip_history(limit=None)
    except MemoryPersistenceError:
        # 存储类失败必须落到 503 脱敏分支（ValueError 在 /api/import 会
        # 被当成用户可修正的请求问题回 400 并原样回显底层错误文本）
        raise
    except Exception as exc:  # noqa: BLE001 - dry-run 不能给乐观结论
        raise MemoryPersistenceError(f"读取既有行程失败：{exc}") from exc
    pending_keys = {
        key
        for key in (_trip_merge_key(trip) for trip in history_trips)
        if key is not None
    }
    for row in sections.get("trips", []):
        key = _trip_merge_key(row)
        if key is not None and key in pending_keys:
            result["trips_updated"] += 1
        else:
            result["trips_created"] += 1
            if key is not None:
                pending_keys.add(key)

    messages = sections.get("messages", [])
    if messages:
        try:
            history = store.get_chat_history(limit=None)
        except MemoryPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - dry-run 不能给乐观结论
            raise MemoryPersistenceError(f"读取既有消息失败：{exc}") from exc
        existing = Counter(
            (
                _message_session_key(message.get("session_id")),
                message.get("role"),
                str(message.get("content") or ""),
            )
            for message in history
        )
        # 与写入层同一实现（含缺失时间戳的补齐口径）：重复消息与
        # 「提问已存在轮次里的回答」都跳过，预览与真实执行的条数一致
        planned = filter_import_messages(
            prepare_import_messages(messages), existing
        )
        result["messages_added"] = len(planned)
        result["messages_skipped"] = len(messages) - len(planned)
    return result


# drop_sessions 在行程行上留下的内部标记（写库前由
# _resolve_dropped_trip_links 消费）：是否清除关联取决于目标库既有行程
# 实际指向的会话，标记本身绝不落库
_DROPPED_SESSION_KEY = "_dropped_session_id"


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
        session_id = trip.get("session_id")
        if session_id in session_ids:
            # 不能直接置 None：目标库里同一行程（同路线 + 同起止日期）可能
            # 关联着另一条仍存活的会话，无差别清除会把活链接清掉。这里只
            # 记下被丢弃的会话，写库前再按目标库的实际关联决定（见
            # _resolve_dropped_trip_links）。
            trip.pop("session_id", None)
            trip[_DROPPED_SESSION_KEY] = str(session_id)


def _resolve_dropped_trip_links(store: Any, trips: list[dict[str, Any]]) -> None:
    """把 drop_sessions 的行程关联标记落实为写库语义（原位修改）。

    - 目标库既有同一行程且其 ``session_id`` 正是被丢弃的会话：显式置 None
      （``save_trip_histories`` 对「键存在」按清除关联处理）；
    - 否则去掉标记、保持「键缺失」：存储层保留既有链接（可能是另一条存活
      会话），新建行程也不会产生指向已删除会话的链接。

    读取既有行程失败时按「保留既有链接」保守处理（宁可留一条死链，也不
    误清存活会话的关联）。标记本身绝不会进入存储。
    """
    marked = [row for row in trips if _DROPPED_SESSION_KEY in row]
    if not marked:
        return
    try:
        history = list(store.get_trip_history(limit=None) or [])
    except Exception as exc:  # noqa: BLE001 - 读不到既有行程时保守保留链接
        logger.warning("读取既有行程失败，跳过关联清理判定：%s", exc)
        history = []
    existing: dict[Any, dict[str, Any]] = {}
    for trip in history:
        existing.setdefault(trip_identity(trip), trip)
    for row in marked:
        dropped = str(row.pop(_DROPPED_SESSION_KEY) or "")
        match = existing.get(trip_identity(row))
        if match is not None and str(match.get("session_id") or "") == dropped:
            row["session_id"] = None


def _planned_new_counts(
    store: Any, sections: dict[str, Any]
) -> tuple[int | None, int | None]:
    """按写入层去重语义预演真正新增的行程 / 消息条数。

    返回 ``(新增行程, 新增消息)``；存储后端不提供读取接口时对应项为
    ``None``（调用方按「全部视为新增」保守处理）。恢复上限检查与
    ``preview_user_import`` 共用：备份里与现有数据重复的行会被去重
    更新 / 跳过，不计入「恢复后总数」——否则接近上限的账号连自己刚
    导出的备份都无法重放（幂等恢复失效），且 --dry-run 与真实执行
    结论相反。
    """
    new_trips: int | None = None
    history_getter = getattr(store, "get_trip_history", None)
    if callable(history_getter):
        pending_keys: set[tuple[str, str, str, str]] | None = None
        try:
            pending_keys = {
                key
                for key in (
                    _trip_merge_key(trip) for trip in history_getter(limit=None)
                )
                if key is not None
            }
        except MemoryPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读取失败不能伪造「全部新增」
            raise MemoryPersistenceError(
                f"读取既有行程失败：{exc}"
            ) from exc
        if pending_keys is not None:
            new_trips = 0
            for trip_row in sections.get("trips", []):
                trip_key = _trip_merge_key(trip_row)
                if trip_key is not None and trip_key in pending_keys:
                    continue
                new_trips += 1
                if trip_key is not None:
                    pending_keys.add(trip_key)

    new_messages: int | None = None
    messages = sections.get("messages", [])
    history_getter = getattr(store, "get_chat_history", None)
    if messages and callable(history_getter):
        existing: Counter[tuple[str | None, Any, str]] | None
        try:
            history = history_getter(limit=None)
            existing = Counter(
                (
                    _message_session_key(message.get("session_id")),
                    message.get("role"),
                    str(message.get("content") or ""),
                )
                for message in history
            )
        except MemoryPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读取失败不能伪造「全部新增」
            raise MemoryPersistenceError(
                f"读取既有消息失败：{exc}"
            ) from exc
        if existing is not None:
            # 与写入层 / 预览同一实现：重复消息与提问已存在轮次的回答不算新增
            new_messages = len(
                filter_import_messages(prepare_import_messages(messages), existing)
            )
    return new_trips, new_messages


def _enforce_import_limits(store: Any, sections: dict[str, Any]) -> None:
    """恢复前的总量上限预检（与导出上限一致，避免恢复后再也无法导出）。

    先用统计快速判断；只有「现有 + 备份条数」看起来超限时才按去重语义
    预演真实新增量：大账号重复导入自己的备份不会被当成新增而拒绝。
    """
    planned: tuple[int | None, int | None] | None = None

    def planned_counts() -> tuple[int | None, int | None]:
        nonlocal planned
        if planned is None:
            planned = _planned_new_counts(store, sections)
        return planned

    def existing_total(name: str) -> int | None:
        getter = getattr(store, "get_statistics", None)
        if not callable(getter):
            return None
        try:
            return int((getter() or {}).get(name) or 0)
        except Exception:  # noqa: BLE001 - 统计失败不阻断恢复
            return None

    messages = sections.get("messages", [])
    existing_messages = existing_total("total_messages")
    if (
        existing_messages is not None
        and messages
        and existing_messages + len(messages) > _MAX_MESSAGES
    ):
        _planned_trips, new_messages = planned_counts()
        if new_messages is None:
            new_messages = len(messages)
        if existing_messages + new_messages > _MAX_MESSAGES:
            raise ValueError(
                f"恢复后消息总数将超过上限 {_MAX_MESSAGES}"
                f"（现有 {existing_messages} + 新增 {new_messages}；"
                f"备份中 {len(messages) - new_messages} 条为重复或提问已存在轮次的回答），"
                "请先清理历史消息后重试"
            )

    trips = sections.get("trips", [])
    existing_trips = existing_total("total_trips")
    if (
        existing_trips is not None
        and trips
        and existing_trips + len(trips) > _MAX_TRIPS
    ):
        new_trips, _planned_messages = planned_counts()
        if new_trips is None:
            new_trips = len(trips)
        if existing_trips + new_trips > _MAX_TRIPS:
            raise ValueError(
                f"恢复后行程总数将超过上限 {_MAX_TRIPS}"
                f"（现有 {existing_trips} + 新增 {new_trips}；"
                f"备份中 {len(trips) - new_trips} 条与现有行程重复），"
                "请先清理历史行程后重试"
            )

    # 偏好按「类型并集」计：覆盖同名类型不增加总量，只有新类型才可能
    # 推过上限。此前 CLI 恢复完全没查偏好，恢复后 400 个类型会让
    # user-export / /api/export 永久拒绝导出全量备份（Web 侧已有同一检查）
    incoming_preferences = sections.get("preferences")
    if isinstance(incoming_preferences, dict) and incoming_preferences:
        getter = getattr(store, "get_preference", None)
        if callable(getter):
            try:
                existing_preferences = getter() or {}
            except Exception:  # noqa: BLE001 - 读取失败不阻断恢复
                existing_preferences = None
            if isinstance(existing_preferences, dict):
                merged = set(existing_preferences) | set(incoming_preferences)
                if len(merged) > _MAX_PREFERENCES:
                    raise ValueError(
                        f"恢复后偏好类型将超过上限 {_MAX_PREFERENCES}"
                        f"（现有 {len(existing_preferences)} + 备份 "
                        f"{len(incoming_preferences)}，合并后 {len(merged)}），"
                        "请先删除不用的偏好类型后重试"
                    )


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

    # 存量 + 本备份不能越过导出上限：否则恢复后 /api/export 与 CLI
    # user-export 永久 413 / 中止，该账号再也导不出备份（上限要避免的正是
    # 这种不可恢复状态）。重复行按去重语义不计入新增，见 _enforce_import_limits
    _enforce_import_limits(store, sections)

    for key, value in sections.get("preferences", {}).items():
        store.save_preference(key, value)
        result["preferences"] += 1
    trips = sections.get("trips", [])
    if trips:
        # 被删除 / 保留字会话的行程关联：只清除目标库确实指向该会话的记录，
        # 另一条存活会话的既有链接必须保留（见 _resolve_dropped_trip_links）
        _resolve_dropped_trip_links(store, trips)
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
            # 上游异常可能带本机路径 / URL：与其它知识库接口同一脱敏口径
            from utils.log_redact import redact_paths

            result["knowledge_error"] = (
                f"知识片段恢复失败：{redact_paths(exc)}"
            )
    # 恢复入口可能没有任何用户数据写入（仅知识片段 / 消息全被剔除）：
    # 显式落盘重建被隔离移走的主文件，否则账号恢复后仍持续 503。
    # 但账号处于隔离态而本次又没写回任何用户数据时，重建主文件会把
    # .corrupt-* / .quarantine 的恢复指引全部抹掉（用户以为已恢复，
    # 实际数据仍只在隔离备份里）——此时保留隔离证据，返回
    # recovery_incomplete 供上层提示「用户数据未恢复」。
    # 知识片段恢复失败时视为恢复未完成，保留隔离 / 删除标记待重试；
    # 「无用户数据 + 隔离证据」的判定与知识片段无关：知识失败路径同样
    # 必须报告用户数据未恢复（否则上层只看 knowledge_error，面板会把
    # 空恢复显示成「已恢复」）。ensure_persisted 仍只在知识片段成功时
    # 调用——知识失败不重建主文件，隔离 / 删除标记得以保留。
    wrote_user_data = any(
        int(result.get(key) or 0)
        for key in (
            "preferences",
            "trips_created",
            "trips_updated",
            "messages_added",
            "titles",
            "pinned",
            "archived",
        )
    )
    if not wrote_user_data and _store_has_quarantine_evidence(store):
        result["recovery_incomplete"] = True
    elif "knowledge_error" not in result:
        ensure_persisted = getattr(store, "ensure_persisted", None)
        if callable(ensure_persisted):
            ensure_persisted()
    return result


def _store_has_quarantine_evidence(store: Any) -> bool:
    """存储实例是否处于隔离 / 待复核态（含落盘标记）。"""
    for name in (
        "get_fresh_quarantine_backup",
        "get_active_quarantine_backup",
        # 遗留隔离备份（无标记 / 历史格式）同样属于「隔离证据」：
        # 空导入重建主文件会让它们从此不可见
        "get_recovered_corrupt_backup",
    ):
        getter = getattr(store, name, None)
        if callable(getter):
            try:
                if getter():
                    return True
            except Exception:  # noqa: BLE001 - 探测失败按无证据处理
                continue
    pending = getattr(store, "has_pending_corrupt_quarantine", None)
    if callable(pending):
        try:
            if pending():
                return True
        except Exception:  # noqa: BLE001
            return False
    return False