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
import contextlib
import json
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.chat_import import filter_import_messages, prepare_import_messages
from utils.errors import ImportVolumeError, MemoryPersistenceError
from utils.identifiers import is_valid_web_session_id
from utils.json_safe import MAX_JSON_DEPTH
from utils.timestamps import localize_naive, parse_iso_datetime
from utils.trip_import import (
    _strict_date,
    normalize_identity_date,
    trip_identity,
)

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
# 单个偏好值序列化后的字符上限：与 Web PUT /api/preferences 的
# _PREFERENCE_VALUE_MAX_JSON、CLI 偏好编辑同一口径——恢复不能写进
# 面板自身拒绝保存的超大值（之后编辑该偏好必然失败）
PREFERENCE_VALUE_MAX_CHARS = 10000

# 导出形状里的 user_id 占位：合法 user_id 最多 64 个字符，字符集允许
# Unicode 字母（含 4 字节的扩展平面字符，最多 256 字节）。按 ASCII 或
# 3 字节汉字占位都会在近 50 MB 时系统性低估；用 64 个 4 字节字母占位
_USER_ID_PLACEHOLDER = chr(0x10400) * 64

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


def _limit_problems(
    backup: dict[str, Any],
    *,
    crlf: bool | None = None,
    size_out: dict[str, int] | None = None,
    precomputed_size: int | None = None,
) -> list[tuple[str, str]]:
    """按轴列出导入上限问题（条数在前、体积在后）。

    轴名用于「导入前基线已超限」豁免：同一轴在导入前已越限时，任何恢复
    都无法让账号在该轴上更糟，不再作为拒绝理由；不同轴互不豁免
    （存量消息超限不能放行把偏好推过上限的备份）。

    ``crlf`` 控制体积轴按哪种行尾估算：``None`` 按运行平台（与 CLI
    ``write_text_atomic`` 的落盘口径一致）；``False`` 按 LF（Web
    ``/api/export`` 响应体原样字节）——Windows 上把 LF 备份按 CRLF
    估算会按行多算 1 字节，接近 50 MB 的合法 Web 备份被误判超限。

    ``precomputed_size``：调用方已按导出格式（``ensure_ascii=False``、
    indent=2、LF、UTF-8）序列化过同一份**已清洗**备份时直接复用字节数，
    省掉本函数内再清洗深拷贝 + 再序列化一遍（导出端对 37MB 备份会因此
    多付一份全量工作）。仅在该口径下传入；``crlf`` 此时被忽略。
    """
    problems: list[tuple[str, str]] = []
    messages = backup.get("chat_history")
    if isinstance(messages, list) and len(messages) > _MAX_MESSAGES:
        problems.append(
            (
                "messages",
                f"消息 {len(messages)} 条超过导入上限 {_MAX_MESSAGES}",
            )
        )
    trips = backup.get("trip_history")
    if isinstance(trips, list) and len(trips) > _MAX_TRIPS:
        problems.append(
            ("trips", f"行程 {len(trips)} 条超过导入上限 {_MAX_TRIPS}")
        )
    preferences = backup.get("preferences")
    if isinstance(preferences, (dict, list)) and len(preferences) > _MAX_PREFERENCES:
        problems.append(
            (
                "preferences",
                f"偏好 {len(preferences)} 条超过导入上限 {_MAX_PREFERENCES}",
            )
        )
    knowledge = backup.get("knowledge")
    if isinstance(knowledge, list) and len(knowledge) > _MAX_KNOWLEDGE:
        problems.append(
            (
                "knowledge",
                f"知识片段 {len(knowledge)} 条超过导入上限 {_MAX_KNOWLEDGE}",
            )
        )
    size = (
        precomputed_size
        if precomputed_size is not None
        else _backup_payload_size(backup, crlf=crlf)
    )
    if size_out is not None:
        size_out["size"] = size
    if size > MAX_BACKUP_BYTES:
        problems.append(
            (
                "size",
                f"约 {size / (1024 * 1024):.1f} MB 超过导入上限 "
                f"{MAX_BACKUP_BYTES // (1024 * 1024)} MB",
            )
        )
    return problems


def _backup_payload_size(
    backup: dict[str, Any], *, crlf: bool | None = None
) -> int:
    """按导出实际写出的格式估算体积（indent=2，可选 CRLF 换行翻译）。

    ``crlf=None`` 按运行平台推定（Windows 的 CLI 写盘加 1 字节/行）；
    ``crlf=False`` 按 LF 计（Web 响应体与浏览器下载的备份就是 LF）。
    """
    # 孤立代理项先转义（否则 encode 直接抛 UnicodeEncodeError）——
    # 紧凑 JSON 的估算会让 1.3-1.6 倍体积的备份通过自检、写盘后却被导入侧
    # 「超过 50 MB」拒绝，导出器产出自己声明可恢复、实际无法导入的备份
    from utils.exporter import sanitize_backup_data

    serialized = json.dumps(
        sanitize_backup_data(backup), ensure_ascii=False, indent=2
    )
    size = len(serialized.encode("utf-8"))
    use_crlf = (os.linesep != "\n") if crlf is None else crlf
    if use_crlf:
        size += serialized.count("\n")
    return size


def backup_limit_problem(
    backup: dict[str, Any],
    *,
    crlf: bool | None = None,
    size_out: dict[str, int] | None = None,
    precomputed_size: int | None = None,
) -> str | None:
    """导出的备份是否超出导入侧上限（超出的备份永远无法恢复）。

    口径与 ``parse_user_export`` / ``import_user_data`` 的硬上限一致：
    条数与备份文件字节上限。返回具体原因或 None。``crlf`` 见
    :func:`_limit_problems`（Web 通道按 LF 估算）；``precomputed_size``
    的适用前提见同函数说明。
    """
    if not isinstance(backup, dict):
        return None
    problems = _limit_problems(
        backup,
        crlf=crlf,
        size_out=size_out,
        precomputed_size=precomputed_size,
    )
    return problems[0][1] if problems else None


def _store_export_snapshot(store: Any) -> dict[str, Any] | None:
    """账号现有数据的导出形状快照；读取失败返回 None。

    None 表示「无法确定导入前基线」：调用方保持原严格口径（不做基线
    豁免），而不是把读取失败当成「账号未超限」放行。
    """
    try:
        if callable(getattr(store, "export_snapshot", None)):
            snapshot = store.export_snapshot() or {}
            return snapshot if isinstance(snapshot, dict) else None
        return {
            "preferences": store.get_preference() or {},
            "trip_history": store.get_trip_history(limit=None) or [],
            "chat_history": store.get_chat_history() or [],
            "session_titles": store.get_session_titles() or {},
            "pinned_sessions": store.get_pinned_sessions() or [],
            "archived_sessions": store.get_archived_sessions() or [],
        }
    except Exception:  # noqa: BLE001 - 读不到现存数据时退回只查备份自身
        return None


def merged_backup_limit_problem(
    store: Any,
    sections: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    crlf: bool | None = None,
    knowledge_unverified: dict[str, str] | None = None,
) -> str | None:
    """恢复后「账号现有数据 + 备份」按导出格式是否仍可能超限。

    ``backup_limit_problem`` 只看备份自身：小备份恢复到接近上限的账号后，
    账号仍会因导出体积永久 413。这里把现有数据与备份合并后再复核。

    合并按导入层的去重语义预演：备份里与现有数据重复的消息 / 行程会被
    跳过或更新，不进入合并结果——直接用「现有 + 备份」拼接会让重导自己
    备份的账号组合体积约为两倍，接近上限时被误判为恢复后超限（幂等恢复
    失效、--dry-run 与真实执行结论相反）。

    ``existing`` 可由调用方传入已取得的导出快照（预检同一次读取），
    避免每个请求对账号数据做两次全量读取、两次快照不一致。
    ``crlf`` 见 :func:`_limit_problems`。

    ``knowledge_unverified`` 传入 dict 时：知识轴无法核对（现有库读不到 /
    本次分片规划失败）会写入 ``{"reason": ...}``。调用方必须据此跳过本次
    知识段写入并上报错误（见 ``import_user_data`` / ``preview_user_import``
    的同名参数）——把知识轴当作「无问题」放行会让真实导入在未核对体积的
    情况下把全局知识库推过导出上限。用户数据恢复不受知识库故障影响
    （知识体积轴冻结约定：不得引入「阻断用户数据恢复」类回归），不可读
    期间的 with_knowledge 体积边界按冻结记录接受残余风险。
    """
    if not isinstance(sections, dict):
        return None
    # 基线按真实账号 ID 估算（与合并 payload 同一口径：占位只在 ID
    # 未知时兜底，否则导出逼近上限的账号重导自身备份会被多算 ~250
    # 字节误拒 413）
    target_user_id = getattr(store, "user_id", None)
    if not isinstance(target_user_id, str) or not target_user_id:
        target_user_id = None
    if existing is None:
        existing = _store_export_snapshot(store)
    if existing is None:
        return sections_limit_problem(
            sections,
            pg_shape=not getattr(store, "snapshot_based", False),
            conservative=True,
            crlf=crlf,
            user_id=target_user_id,
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
    # 现有全局知识库：库文件存在但读不出（忙 / 锁冲突 / 损坏）时按
    # 「不可核对」优雅降级——用户数据照常恢复，知识段跳过并上报
    # （knowledge_unverified），不阻断恢复。知识库忙是常见并发状态
    # （面板知识导入持写锁、Web 进程持 milvus-lite 文件锁时 CLI 访问），
    # 对它做硬失败会阻断全部恢复（含不含知识段 / --skip-knowledge 的
    # 备份）——知识体积轴冻结约定：不得引入「阻断用户数据恢复」类回归。
    # 代价是知识库不可读期间的 with_knowledge 体积边界无法核对，按冻结
    # 记录接受该残余风险。未初始化 / 集合缺失仍在读取层按空库处理
    existing_kb: list[Any] | None
    unverified_reason = ""
    try:
        existing_kb = _existing_knowledge_rows()
    except MemoryPersistenceError as exc:
        logger.warning(
            "合并预检：读取现有知识库失败（%s），知识轴不可核对", exc
        )
        existing_kb = None
        unverified_reason = (
            "知识库当前不可读，无法核对恢复后的知识体积；"
            "本次未恢复知识片段，请稍后重试补全"
        )
    plan_reason: dict[str, str] = {}
    knowledge_rows = (
        _knowledge_rows_for_merge(
            existing_kb, sections, reason_out=plan_reason
        )
        if existing_kb is not None
        else None
    )
    if existing_kb is not None and knowledge_rows is None:
        unverified_reason = plan_reason.get(
            "reason",
            "知识库规划失败，无法核对恢复后的知识体积；"
            "本次未恢复知识片段，请稍后重试补全",
        )
    if (
        knowledge_unverified is not None
        and unverified_reason
        and sections.get("knowledge")
    ):
        knowledge_unverified["reason"] = unverified_reason
    # 以现有快照为底，再覆盖为合并后的各段；真实导出端会在
    # export_snapshot 之外补 user_id / exported_at——这里放与导出端
    # 等价的形状（user_id 用真实账号 ID；exported_at 取微秒宽度，
    # CLI 导出的秒级时间戳更窄；session_id 是 CLI 导出恒写的空串，
    # Web 导出没有——三者取跨通道上界，高估是 fail-closed 的安全方向）
    payload = dict(existing)
    payload.setdefault("user_id", target_user_id or _USER_ID_PLACEHOLDER)
    payload.setdefault("exported_at", "1970-01-01T00:00:00.000000")
    payload.setdefault("session_id", "")
    # 合并 payload 的知识段按「恢复后真正会存在的库内容」计：规划失败时
    # 本次跳过知识写入（见上），库内容就是已知的现有库——用空列表替换会
    # 把既有全局库从 with_knowledge 导出体积里抹掉，用户数据增长可把
    # 导出推过 50MB 而复核放行（此后永久 413）
    merged_knowledge = (
        list(knowledge_rows)
        if knowledge_rows is not None
        else list(existing_kb or [])
    )
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
                _preview_dropped_trip_links(
                    existing_trip_rows, sections.get("trips") or []
                ),
                include_empty_keys=pg_shape,
                existing_last_number=_existing_trip_number_watermark(
                    store, existing, pg_shape=pg_shape
                ),
            ),
            "preferences": merged_prefs,
            "knowledge": merged_knowledge,
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
    # JSON 后端恒写该字段（空行程也序列化 0，与真实导出一致）；只在
    # PG 形状省略。行程为空时取现有水位（无则 0），不能漏键——
    # sections 预检与真实导出都带它，漏写会低估恢复后的导出体积
    if not pg_shape:
        # 写入层对每个新建行至少推进一次水位；冲突改号行还能把水位推到
        # 18 位以上——取「现有水位 / 最大编号 / 新建行数」的保守上界。
        # PG 的 get_statistics 没有该字段，保持后端真实导出形状
        existing_last = _existing_last_trip_number(existing) or 0
        water = max([existing_last, *trip_numbers], default=existing_last)
        statistics["last_trip_number"] = water + created_trip_count
    payload["statistics"] = statistics
    problem = backup_limit_problem(payload, crlf=crlf)
    if problem is None:
        return None
    # 导入前基线的按轴豁免：存量账号在某个轴上已超限时（体积 / 消息 /
    # 行程 / 偏好 / 知识片段）——任何恢复都无法让账号在该轴上更糟，
    # 该轴不再作为拒绝理由（否则账号连幂等重导自身备份都会被 413）。
    # 基线包含现有全局知识库：知识轴只在自身上已超限时豁免，不再被
    # 其它轴的超限连带放行。不同轴互不豁免：存量消息超限不能放行把
    # 知识库推过上限的备份；未超限的轴照常拦截「恢复后永远无法导出」
    # 的增量。
    baseline_axes = {
        axis
        for axis, _message in _limit_problems(
            _export_shape_baseline(
                existing,
                existing_kb if existing_kb is not None else [],
                user_id=target_user_id,
                # 基线对齐真实 Web 导出键集（无 session_id）：基线高估会
                # 把临近上限的账号误判成「已超限」并整体豁免 size 轴
                include_session_id=False,
            ),
            crlf=crlf,
        )
    }
    problems = _limit_problems(payload, crlf=crlf)
    for axis, message in problems:
        if axis not in baseline_axes:
            return message
    # 默认导出（不含知识库）的体积轴独立复核：知识库只进带知识库的全量
    # 导出，不能抬高用户数据轴的基线把用户数据增长一并豁免——否则恢复
    # 可以把「默认导出」推过 50 MB 而 with_knowledge 基线掩盖了它。
    # 默认导出没有 knowledge 键（两个通道都不写）：删掉而不是置空，
    # 合成的键会让判定边界整体平移、临近上限时翻转豁免判定
    user_payload = dict(payload)
    user_payload.pop("knowledge", None)
    user_baseline_axes = {
        axis
        for axis, _message in _limit_problems(
            _export_shape_baseline(
                existing,
                [],
                user_id=target_user_id,
                include_knowledge=False,
                include_session_id=False,
            ),
            crlf=crlf,
        )
    }
    for axis, message in _limit_problems(user_payload, crlf=crlf):
        if axis not in user_baseline_axes:
            return message
    # 知识库体积轴独立复核：size 轴与用户数据共享，账号自身在 size 轴
    # 已超限时上面的按轴豁免会连带放过知识增量。这里按「全局知识库
    # 单独导出」判定，只在库自身已超限时豁免——其它轴不连带。
    kb_baseline_axes = {
        axis
        for axis, _message in _limit_problems(
            {"knowledge": list(existing_kb or [])}, crlf=crlf
        )
    }
    for axis, message in _limit_problems(
        {"knowledge": list(merged_knowledge)}, crlf=crlf
    ):
        if axis not in kb_baseline_axes:
            return message
    return problem if not problems else None


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

    「库文件存在但集合缺失」（半初始化）按空库处理：写路径的
    ``_get_agent(create_if_missing=True)`` 会自动建集合并成功导入，
    把它当成「不可读」会让每次导入都跳过知识段并报不可恢复的错误，
    与真实写路径相反。探测失败（``_collection_exists`` 返回 True）
    保持原 fail-closed 口径。
    """
    from utils import knowledge_admin

    try:
        if not knowledge_admin.knowledge_db_path().exists():
            return []
        return list(knowledge_admin.export_documents() or [])
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - 交给合并预检按知识轴跳过
        exists_fn = getattr(knowledge_admin, "_collection_exists", None)
        if callable(exists_fn) and not exists_fn():
            return []
        raise MemoryPersistenceError(
            f"读取知识库现有片段失败：{exc}"
        ) from exc


def _planned_knowledge_rows(
    sections: dict[str, Any], *, reason_out: dict[str, str] | None = None
) -> list[Any] | None:
    """本次恢复真正会写入的知识分片（导入层 dry-run 规划）。

    与真实 ``import_documents`` 同一实现（分组还原 / 去重 / 切分后的
    最终形状）：预演与落库逐字节同源。返回 None 表示知识轴不可评估
    （规划失败）：调用方跳过知识体积轴、不阻断用户数据恢复（导入层会
    把知识段以 ``knowledge_error`` 上报）。``reason_out`` 传入 dict 时
    写入面向用户的原因（未初始化与通用规划失败分开，前者重试无益）。
    """
    rows = sections.get("knowledge") or []
    if not rows:
        return []
    try:
        from utils.knowledge_admin import import_documents

        plan = import_documents(rows, dry_run=True, return_pieces=True)
    except FileNotFoundError:
        # 与真实导入同一文案：知识库未初始化，重试无益
        if reason_out is not None:
            reason_out["reason"] = (
                "知识库未初始化（先运行初始化脚本再恢复知识片段）"
            )
        return None
    except Exception as exc:  # noqa: BLE001 - 知识失败不阻断用户数据恢复
        logger.warning("合并预检：知识库规划失败（%s），跳过知识体积轴", exc)
        if reason_out is not None:
            reason_out["reason"] = (
                "知识库规划失败，无法核对恢复后的知识体积；"
                "本次未恢复知识片段，请稍后重试补全"
            )
        return None
    return list(plan.get("pieces") or [])


def knowledge_rows_for_volume_check(
    rows: list[Any],
    skip_out: dict[str, int] | None = None,
) -> list[Any] | None:
    """导出体积校验使用的知识行：按「导入空库」规划的分片形状。

    不能在源库上规划：整库导出行本来就在源库中，``dry_run`` 去重后
    pieces 只剩「新增分片」，pieces 非空时整体替换原始行会让其余全部
    知识行退出体积 / 条数估算（40 MB 库 + 一条旧版长单条时只校验 2 片，
    导出写出 55 MB 文件却永远无法导入）。``ignore_existing`` 不碰库内容、
    不持锁，按导入空库返回全部分片。

    ``skip_out`` 传入 dict 时回填导入规划会跳过的行数（无效 / 残缺组 /
    布局冲突）：导出端据此提示「文件仍可导出、但恢复时这些知识会跳过」。

    规划失败返回 None：调用方不能退回原始行放行（旧版长单条重切分后的
    分片可能远超原始行体积）。导出端有两种处置：按「无法确认备份可恢复」
    拒绝导出，或在用户数据本身仍可导出时省略知识段降级（见
    :func:`export_backup_limit_problem` 的 ``reason_out``）。
    """
    if not rows:
        return []
    try:
        from utils.knowledge_admin import import_documents

        plan = import_documents(
            rows,
            dry_run=True,
            return_pieces=True,
            ignore_existing=True,
        )
    except Exception as exc:  # noqa: BLE001 - 由调用方按不可核对拒绝导出
        logger.warning("导出预检：知识分片规划失败（%s）", exc)
        return None
    pieces = plan.get("pieces") if isinstance(plan, dict) else None
    if pieces is None:
        return None
    if skip_out is not None and isinstance(plan, dict):
        groups = plan.get("skipped_groups")
        # skipped_invalid 已按行计入坏组内的每个分片（knowledge_admin 的
        # planner 口径）：skipped_groups 只是组数，仅供提示文案，不能再与
        # 行数相加——否则跳过条数会超过备份里的知识行总数
        skip_out["skipped_invalid"] = int(
            plan.get("skipped_invalid") or 0
        )
        skip_out["skipped_groups"] = (
            len(groups) if isinstance(groups, list) else int(groups or 0)
        )
        skip_out["skipped_conflict"] = int(
            plan.get("skipped_conflict") or 0
        )
    return list(pieces)


def export_backup_limit_problem(
    backup: dict[str, Any],
    *,
    warning_out: dict[str, Any] | None = None,
    reason_out: dict[str, Any] | None = None,
    precomputed_size: int | None = None,
) -> str | None:
    """导出端自检：原始备份与「导入空库」的知识分片形状都必须可导入。

    两道轴缺一不可：
    - 原始备份：导入端在规划前就按原始知识行数 / 文件字节拒绝；
    - 空库分片计划：长文档重切分后每片都带元数据，恢复后的体积可能
      大于原始行（#876 的口径），只看原始行会写出无法恢复的备份。

    ``precomputed_size``：原始备份已按导出格式序列化过时传入字节数，
    省掉第一次体积估算的再次清洗 + 再序列化（见 :func:`_limit_problems`）。
    分片计划形状（``shaped``）与原始备份字节不同，那次复核**不得**复用。

    知识规划失败时返回拒绝原因（无法确认备份可恢复）。``warning_out``
    传入 dict 时回填「恢复时会被跳过的知识行」提示：这些行不会阻断导出
    （用户数据仍可恢复），但文件并不包含完整知识。

    ``reason_out`` 传入 dict 时回填知识轴的拒绝原因代码
    （``over-limit`` 超限 / ``planning-failed`` 分片规划失败）：导出端在
    用户数据本身仍可导出时可按此省略知识段降级，并向用户给出区分文案
    （「超限请清理」与「暂无法核对请稍后重试」是不同处置）。
    """
    if not isinstance(backup, dict):
        return None
    size_out: dict[str, int] = {}
    problem = backup_limit_problem(
        backup,
        crlf=False,
        size_out=size_out,
        precomputed_size=precomputed_size,
    )
    if problem:
        return problem
    shaped: dict[str, Any] = backup
    skip_counts: dict[str, int] = {}
    knowledge = backup.get("knowledge")
    if isinstance(knowledge, list) and knowledge:
        planned = knowledge_rows_for_volume_check(
            knowledge, skip_out=skip_counts
        )
        if planned is None:
            if reason_out is not None:
                reason_out["knowledge"] = "planning-failed"
            return (
                "知识片段无法按导入规则规划，无法确认备份可恢复；"
                "请稍后重试（知识库忙时暂停写入）"
            )
        shaped = {**backup, "knowledge": planned}
        problem = backup_limit_problem(
            shaped, crlf=False, size_out=size_out
        )
        if problem:
            if reason_out is not None:
                reason_out["knowledge"] = "over-limit"
            return problem
    # 落库形状复核：恢复端会把消息时间戳归一为带时区 32 字符、补齐行程
    # 编号 / 时间戳并重算统计——只按原始文件形状自检会产出「导出 200、
    # 恢复到新账号 413」的不可恢复备份。只在逼近上限时精确复核（解析 +
    # 重组整份导出 payload 的代价与导出本身同量级，逐条裸算不值得）。
    messages = shaped.get("chat_history")
    trips = shaped.get("trip_history")
    margin = (
        64 * (len(messages) if isinstance(messages, list) else 0)
        + 512 * (len(trips) if isinstance(trips, list) else 0)
        + 4096
    )
    if size_out.get("size", 0) + margin > MAX_BACKUP_BYTES:
        shape_problem = _post_restore_shape_problem(shaped)
        if shape_problem:
            return f"恢复后{shape_problem}"
    # 行数口径：skipped_invalid 已含坏组内的各分片行，skipped_groups 只是
    # 组数，不能再相加（否则跳过条数会超过备份里的知识行总数）
    total_skipped = skip_counts.get(
        "skipped_invalid", 0
    ) + skip_counts.get("skipped_conflict", 0)
    if warning_out is not None and total_skipped:
        warning_out["knowledge_skipped_count"] = total_skipped
        warning_out["knowledge_skipped"] = (
            f"{total_skipped} 条知识片段在恢复时会被跳过"
            "（无效内容 / 残缺分片组 / 布局冲突）；"
            "备份中的用户数据仍可恢复"
        )
    return None


def _post_restore_shape_problem(backup: dict[str, Any]) -> str | None:
    """按导入端的落库形状复核 50MB（导出 200 / 导入 413 的非对称修复）。

    解析失败按原口径放行：能走到这里说明原始备份已通过全部结构预检，
    解析异常属于内部问题，不应把合法导出误拒。``pg_shape`` 取当前部署
    后端——恢复到同部署的新账号是最常见场景，也是导出承诺的可恢复性
    口径。
    """
    try:
        sections, _errors = parse_user_export_data(backup)
    except Exception:  # noqa: BLE001 - 见 docstring
        return None
    if not isinstance(sections, dict):
        return None
    try:
        from config import STORAGE_CONFIG

        pg_shape = bool(STORAGE_CONFIG.get("postgres_dsn"))
    except Exception:  # noqa: BLE001 - 配置不可读时按 JSON 口径
        pg_shape = False
    return sections_limit_problem(
        sections,
        pg_shape=pg_shape,
        existing=None,
        conservative=True,
        crlf=False,
        user_id=str(backup.get("user_id") or "") or None,
    )


def _export_shape_baseline(
    existing: dict[str, Any],
    knowledge_rows: list[Any],
    *,
    user_id: str | None = None,
    include_knowledge: bool = True,
    include_session_id: bool = True,
) -> dict[str, Any]:
    """导入前的完整导出形状（现有存量 + 现有知识库）。

    知识轴是否允许作为拒绝理由，取决于「恢复前」账号本身是否仍可导出
    WITH-knowledge 备份：已超限的账号不可能被恢复推得更糟。

    ``user_id`` 用真实账号 ID 计算基线：按最坏占位会把实际未超限的
    账号误判成基线超限、连带豁免本次导入的体积增长（占位只保留在合并后
    的 payload，那里高估是 fail-closed 的安全方向）。

    ``include_knowledge=False`` 用于「默认导出」轴：真实默认导出的键集
    不含 ``knowledge``，合成的空键会让判定边界整体平移。

    ``include_session_id=False``（基线判定用）对齐真实 Web 导出：Web
    `/api/export` 没有该键，合成键会把基线抬高 ~18 字节——基线高估会让
    临近上限的账号被误判成「已超限」、size 轴整体豁免（fail-open）。
    合并后的 payload 仍保留合成键（跨通道上界，那里高估是安全方向）。
    """
    payload = dict(existing)
    payload["user_id"] = user_id or _USER_ID_PLACEHOLDER
    payload.setdefault("exported_at", "1970-01-01T00:00:00.000000")
    if include_session_id:
        payload.setdefault("session_id", "")
    payload.update(
        {
            "chat_history": list(existing.get("chat_history") or []),
            "trip_history": list(existing.get("trip_history") or []),
            "preferences": existing.get("preferences") or {},
            "session_titles": existing.get("session_titles") or {},
            "pinned_sessions": list(existing.get("pinned_sessions") or []),
            "archived_sessions": list(
                existing.get("archived_sessions") or []
            ),
            "statistics": existing.get("statistics") or {},
        }
    )
    if include_knowledge:
        payload["knowledge"] = list(knowledge_rows)
    return payload


def _knowledge_rows_for_merge(
    existing_kb: list[Any],
    sections: dict[str, Any],
    *,
    reason_out: dict[str, str] | None = None,
) -> list[Any] | None:
    """合并预检的知识段：现有全局库 + 本次真正写入的分片。

    None = 跳过知识轴（仅当本次规划失败——现有库读不到时调用方直接
    跳过，不进入这里）。知识轴是否允许作为拒绝理由由最终 payload 的
    按轴基线豁免统一判定（与用户数据轴同一语义）：此前用「完整导出
    形状是否有任意问题」决定跳过，账号在其它轴超限时知识轴被连带
    豁免，可把全局知识库推过导出上限（此后所有用户的带知识库导出
    永久 413）。``reason_out`` 见 :func:`_planned_knowledge_rows`。
    """
    planned = _planned_knowledge_rows(sections, reason_out=reason_out)
    if planned is None:
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


def _preview_dropped_trip_links(
    existing_rows: list[Any], trips: list[Any]
) -> list[Any]:
    """合并预演用：复刻 ``_resolve_dropped_trip_links`` 的写库语义（副本上）。

    带 ``_DROPPED_SESSION_KEY`` 标记的备份行在真实导入时：目标库同一行程
    的 ``session_id`` 正是被丢弃的会话 → 显式置 None；否则去掉标记、保留
    既有链接。标记本身绝不进入存储——预览既不能把标记算进导出体积，也
    不能漏掉 ``session_id=None`` 的字节（临近上限时预演与真实落库结论
    会相反）。sections 上的标记不被消耗（真实清除仍由导入层执行）。
    """
    if not any(
        isinstance(row, dict) and _DROPPED_SESSION_KEY in row for row in trips
    ):
        return list(trips)
    existing: dict[Any, dict[str, Any]] = {}
    for row in existing_rows:
        if isinstance(row, dict):
            existing.setdefault(trip_identity(row), row)
    cleaned: list[Any] = []
    for row in trips:
        if not isinstance(row, dict) or _DROPPED_SESSION_KEY not in row:
            cleaned.append(row)
            continue
        copy = dict(row)
        dropped = str(copy.pop(_DROPPED_SESSION_KEY) or "")
        match = existing.get(trip_identity(copy))
        if match is not None and str(match.get("session_id") or "") == dropped:
            copy["session_id"] = None
        cleaned.append(copy)
    return cleaned


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
    messages: list[Any],
    *,
    pg_shape: bool = False,
    trim_json_timestamps: bool = True,
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
        if (
            trim_json_timestamps
            and not pg_shape
            and isinstance(timestamp, str)
            and len(timestamp) > 26
        ):
            # 解析把 naive 时间戳归一成带偏移的 32 字符，但幂等重导时
            # 重复行不落库、真实导出仍是 26 字符：已有快照、后续还有
            # 权威合并复核时按 26 估算（新建行由合并复核精确判定）。
            # conservative=True（快照读不到、本预检即最终判定）不截断，
            # 按落库后的真实宽度 32 计，避免低估放行「恢复后永久 413」
            row = {**row, "timestamp": timestamp[:26]}
        rows.append(row)
    return rows


def sections_limit_problem(
    sections: dict[str, Any],
    *,
    pg_shape: bool = False,
    existing: dict[str, Any] | None = None,
    conservative: bool = False,
    crlf: bool | None = None,
    user_id: str | None = None,
) -> str | None:
    """恢复后账号是否仍能导出全量备份（按导出格式复核条数与体积）。

    CLI 导入此前只按原始文件字节做 50MB 预检：紧凑 JSON 备份可以通过并
    恢复成功，随后 user-export 按 indent=2 估算体积超限、永久拒绝导出——
    用户必须先删数据才能再备份。这里把可恢复段按导出形状重新组装后复用
    :func:`backup_limit_problem` 的同一口径（与 Web /api/import 一致）。

    ``existing`` 为账号现有数据的导出形状快照：导入前基线已在某个轴
    超限时该轴不再作为拒绝理由（与 :func:`merged_backup_limit_problem`
    同一豁免语义）——否则存量已超 50MB 的账号恢复自身体积超限的备份会
    被预检 413，合并复核的基线放行永远不可达。传 None 保持原严格口径。
    传快照时体积轴不在本函数判定（交给随后必定执行的合并复核）：本函数
    的空行程表预演会给缺 trip_id 的历史行程补 id，比真实导入的同身份
    批量更新更严；条数轴仍按基线豁免判定。

    知识库段只在 ``existing is None``（快照读不到、后续没有权威合并
    复核）时计入：知识轴（含知识段体积）由随后的
    :func:`merged_backup_limit_problem` 按「现有数据 + 现有知识库」
    完整基线判定，预检只有用户数据快照时无法复刻该基线——把备份知识段
    计入会把部署级超限知识库的重导误拒，而 merged 本会放行。

    ``conservative=True`` 用于「本预检即最终判定」的回退路径（合并复核
    读不到快照时）：JSON 时间戳不按 26 字符截断，按落库后的真实宽度
    32 计，避免低估放行「恢复后永久无法导出」的备份。
    """
    if not isinstance(sections, dict):
        return None
    # 已知存量基线的调用方（CLI / Web 主路径）随后必定执行合并复核：
    # 知识轴交给它判定；预检只覆盖用户数据轴
    include_knowledge = existing is None
    # 与 merged_backup_limit_problem 同一导出形状（空存量）：行程走编号
    # 仲裁（同身份重复折叠、补 trip_id/timestamp、PG 空键），会话元数据
    # 与重算统计一并计入——手搭的窄字段 payload 会低估同内容的真实导出，
    # 让 CLI / Web 把实际可恢复的备份判成超限
    # 起号从 0 开始：独立预检没有 store、拿不到 PG 计数器，而后续的
    # 权威合并复核会用真实水位（读不到时按最坏占位 fail-closed）——
    # 预检按最坏占位会比权威复核更严，把实际可恢复的备份误拒
    trip_rows = _merged_trip_rows(
        [],
        # 预演同样不能把 drop_sessions 的私有标记算进导出行
        _preview_dropped_trip_links([], sections.get("trips") or []),
        include_empty_keys=pg_shape,
        existing_last_number=0,
    )
    messages = _export_shape_messages(
        sections.get("messages") or [],
        pg_shape=pg_shape,
        trim_json_timestamps=not conservative,
    )
    payload: dict[str, Any] = {
        # 传入真实账号 ID 时按真实宽度估算；ID 未知才用最坏占位
        # （占位 256 字节，会把逼近 50MB 的合法账号重导自身备份误拒）
        "user_id": user_id or _USER_ID_PLACEHOLDER,
        "exported_at": "1970-01-01T00:00:00.000000",
        "session_id": "",
        "chat_history": messages,
        "trip_history": trip_rows,
        "preferences": sections.get("preferences") or {},
        "knowledge": (sections.get("knowledge") or [])
        if include_knowledge
        else [],
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
    problem = backup_limit_problem(payload, crlf=crlf)
    if problem is None:
        return None
    problems = _limit_problems(payload, crlf=crlf)
    if existing is not None:
        # 体积轴交给随后的权威合并复核（merged_backup_limit_problem 用
        # 同一快照、含存量消息与现有知识库基线）：本函数的空行程表预演
        # 会给缺 trip_id 的历史行程逐行补 id（真实导入只按同身份批量
        # 更新、不补 id），独立判定比权威复核更严、临近上限的合法备份
        # 被误拒 413。CLI / Web 主路径随后必定执行合并复核
        problems = [
            (axis, message) for axis, message in problems if axis != "size"
        ]
    if not problems:
        return None
    if existing is None:
        return problems[0][1]
    baseline_axes = {
        axis
        for axis, _message in _limit_problems(
            _export_shape_baseline(
                existing, [], user_id=user_id, include_session_id=False
            ),
            crlf=crlf,
        )
    }
    for axis, message in problems:
        if axis not in baseline_axes:
            return message
    return None


def parse_user_export(
    text: str, *, stats_out: dict[str, int] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """解析并校验 user-export JSON 文本，返回 ``(可恢复数据, 错误列表)``。"""
    try:
        data = json.loads(text)
    except RecursionError:
        # 结构性深层嵌套（如 '['*3000）不是 ValueError：不捕获会让 CLI
        # --json 模式打印 traceback 而不是结构化错误
        return {}, ["JSON 嵌套过深，无法解析"]
    except (TypeError, ValueError) as exc:
        return {}, [f"JSON 解析失败：{exc}"]
    return parse_user_export_data(data, stats_out=stats_out)


def parse_user_export_data(
    data: Any, *, stats_out: dict[str, int] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """校验已解析的 user-export JSON 对象（CLI 文本路径与 Web API 共用）。

    可恢复数据键：preferences / trips / messages / titles / pinned / archived。
    没有任何可恢复数据时返回空 dict（错误列表说明原因）。

    ``stats_out`` 传入 dict 时回填 ``knowledge_dropped``：备份里存在、但
    在解析阶段被丢弃的知识行数（非法行 / 残缺分片组）。这些行不会进入
    :func:`import_user_data` 的 ``knowledge_skipped``，调用方据此把它并入
    恢复结果，与 README「恢复时计入 knowledge_skipped」的口径一致。
    """

    def _stats(dropped: int = 0) -> None:
        if stats_out is not None:
            stats_out["knowledge_dropped"] = max(0, int(dropped))

    if not isinstance(data, dict):
        _stats()
        return {}, ["文件内容不是 JSON 对象"]

    errors: list[str] = []
    sections: dict[str, Any] = {}
    raw_knowledge = data.get("knowledge")
    raw_knowledge_count = (
        len(raw_knowledge) if isinstance(raw_knowledge, list) else 0
    )

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
        if name == "preferences":
            # 保留类型：存储层加载时把该类型的列表值展开成独立偏好类型
            # （并丢弃 / 覆盖同名条目），恢复后类型数可远超上限、账号
            # 永久无法导出；与 Web PUT / CLI pref-set 的拒绝口径一致
            errors.append(
                f"跳过第 {index} 个偏好：preferences 是保留的偏好类型"
            )
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
        try:
            value_size = len(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            value_size = None
        if value_size is not None and value_size > PREFERENCE_VALUE_MAX_CHARS:
            # 与 Web / CLI 编辑同一序列化口径：导入超限值会让该偏好
            # 之后无法在面板保存（新增 / 编辑都会被 422 拒绝）
            errors.append(
                f"跳过第 {index} 个偏好：序列化后超过 "
                f"{PREFERENCE_VALUE_MAX_CHARS} 字符"
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
    _stats(raw_knowledge_count - len(knowledge))

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


def _probe_embedding_model() -> None:
    """只加载嵌入模型（不建库 / 不建集合）：半初始化库的预览探针。

    路径解析与技能侧 ``RAGKnowledgeAgent`` 的初始化保持一致（本地模型
    候选锚点 → 模型 ID 兜底）；两边漂移会让预览误报模型不可用，调整时
    需同步。
    """
    from config import RAG_CONFIG
    from utils.embedding_model import embedding_dimension, get_embedding_model

    from agents.rag_knowledge_agent import RAGKnowledgeAgent

    default_local_model = "data/models/bge-small-zh-v1.5"
    configured = str(RAG_CONFIG.get("embedding_model") or default_local_model)
    path_obj = Path(configured).expanduser()
    model_path_or_id = configured
    if not path_obj.is_absolute():
        resolved = next(
            (
                candidate
                for candidate in RAGKnowledgeAgent._model_path_candidates(
                    path_obj
                )
                if candidate.exists()
            ),
            None,
        )
        if resolved is not None:
            path_obj = resolved
    if path_obj.exists():
        model_path_or_id = str(path_obj.resolve())
    elif RAGKnowledgeAgent._looks_like_filesystem_path(configured):
        # 与技能侧一致：只把「看起来是路径」的缺失值当错误；默认本地
        # 模型未下载时回退同名 HF 模型（联网下载）。显式配置的其它本地
        # 路径缺失必须报错——静默换模型会让查询向量与库内向量来源不一致
        if configured != default_local_model:
            raise RuntimeError(
                f"配置的本地嵌入模型路径不存在：{configured}"
                "（请下载该模型，或修改 RAG_CONFIG['embedding_model']）"
            )
        model_path_or_id = "BAAI/bge-small-zh-v1.5"
    model = get_embedding_model(model_path_or_id)
    if embedding_dimension(model) is None:
        raise RuntimeError("无法读取嵌入模型维度")


def _knowledge_ready_probe() -> None:
    """确认嵌入模型可加载（与真实导入同一入口）。

    ``import_documents(dry_run=True)`` 只做规划、不加载模型：模型文件
    缺失 / 损坏的部署里预览会通过、真实导入才失败。这里复用维护模块
    的懒加载入口（Agent 与模型被缓存，真实导入随后直接命中）。
    构造成功但 ``initialized`` 为 False（依赖缺失 / 集合不可用）同样
    视为不可用——真实导入会在写入批次时以状态错误失败。
    半初始化库（库文件存在但集合缺失）下不构造写路径 Agent：
    ``create_if_missing=True`` 会在「只读」预览里创建空集合、改写知识库
    状态；这类部署的写路径会自动建集合，预览只验证嵌入模型可加载。
    单测 / 直跑清单默认把它替换为 no-op（见 tests/conftest.py），
    避免逐用例加载真实模型。
    """
    from utils import knowledge_admin

    exists_fn = getattr(knowledge_admin, "_collection_exists", None)
    if callable(exists_fn) and not exists_fn():
        _probe_embedding_model()
        return
    from utils.knowledge_admin import _get_agent

    agent = _get_agent()
    if getattr(agent, "initialized", True) is False:
        raise RuntimeError("嵌入模型或知识库集合不可用")


def preview_user_import(
    store: Any, sections: dict[str, Any], *, knowledge_unverified: str = ""
) -> dict[str, Any]:
    """预览恢复结果（只读）：返回与 import_user_data 相同键的计数，不写库。

    ``knowledge_unverified`` 与 :func:`import_user_data` 同名参数一致：
    合并复核无法核对知识体积时，真实执行会跳过知识段并上报错误，预览
    必须给出同一结论（否则 --dry-run ok=true、真实执行 ok=false）。
    """
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
    # 预览与真实执行同一结论：知识库不可用时真实导入会报 knowledge_error
    # （知识一条未恢复、ok=false），dry-run 也必须如实报告。规划结果同时
    # 给出「将新增 / 跳过」的真实计数（而不是备份输入条数），并按真实
    # 导入同一入口探测嵌入模型可用性——dry-run 规划本身不加载模型，
    # 只看它会漏掉「模型不可用」这一类失败
    knowledge_rows = sections.get("knowledge") or []
    if knowledge_rows and knowledge_unverified:
        # 与真实执行同一结论：知识段本次不写，如实上报原因
        result["knowledge_error"] = knowledge_unverified
    elif knowledge_rows:
        try:
            from utils.knowledge_admin import import_documents

            plan = import_documents(
                knowledge_rows, dry_run=True, return_pieces=True
            )
            # 与写层同一判定：快速判重（内容齐全且分片顺序一致）会跳过
            # 嵌入模型；其它路径（含顺序重排、集合缺失）都会加载模型，
            # 预览按 needs_model 决定是否探测。旧计划接口无该字段时
            # 退回「确有新增才需要模型」的保守近似
            needs_model = plan.get("needs_model")
            if needs_model is None:
                needs_model = int(plan.get("would_import") or 0) > 0
            if needs_model:
                _knowledge_ready_probe()
            # 全局导出上限：恢复前知识库已越限（合并复核按基线豁免放行）
            # 或账号快照不可读时，真实导入会在写锁内被拒绝并记
            # knowledge_error——预览必须同一结论，不能报「将新增 N 条」
            export_limit_problem = plan.get("export_limit_problem")
            if export_limit_problem:
                from utils.log_redact import redact_paths

                result["knowledge_error"] = (
                    f"知识片段恢复失败：{redact_paths(export_limit_problem)}"
                )
            else:
                # 计数在探测之后回填：探测失败时真实导入一条未写，预览也
                # 不应给出「将新增 N 条」的乐观计数
                result["knowledge_imported"] = int(
                    plan.get("would_import") or 0
                )
                result["knowledge_skipped"] = int(plan.get("skipped") or 0)
        except FileNotFoundError:
            result["knowledge_error"] = (
                "知识库未初始化（先运行初始化脚本再恢复知识片段）"
            )
        except Exception as exc:  # noqa: BLE001 - 与真实导入同口径
            # 上游异常可能带本机路径 / URL：与其它知识库接口同一脱敏口径
            from utils.log_redact import redact_paths

            result["knowledge_error"] = (
                f"知识片段恢复失败：{redact_paths(exc)}"
            )
    # 预览与真实执行同一结论：隔离账号 + 备份无可写回用户数据时，
    # --dry-run / ?dry_run=1 同样报告恢复未完成（否则预览 ok=true、
    # 真实执行 ok=false、退出码相反，用户按预览结论误判恢复可用）
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
        # 导入前已越限的账号（对话热路径加硬上限之前的存量）不再按
        # 「推过上限」拒绝：恢复无法让它在该轴上更糟，备份新增量与
        # 基线豁免同语义
        and existing_messages <= _MAX_MESSAGES
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
        # 与消息同一基线语义：已越限的账号不再按「推过上限」拒绝
        and existing_trips <= _MAX_TRIPS
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
                if (
                    len(existing_preferences) <= _MAX_PREFERENCES
                    and len(merged) > _MAX_PREFERENCES
                ):
                    raise ValueError(
                        f"恢复后偏好类型将超过上限 {_MAX_PREFERENCES}"
                        f"（现有 {len(existing_preferences)} + 备份 "
                        f"{len(incoming_preferences)}，合并后 {len(merged)}），"
                        "请先删除不用的偏好类型后重试"
                    )


@contextlib.contextmanager
def _import_exclusion(store: Any):
    """导入写入的跨进程互斥（JSON 数据目录文件锁 / PG 用户级 advisory 锁）。

    锁文件与数据锁分离（``<db>.import.lock``）：嵌套同一把文件锁会自锁。
    持锁期间另一个导入（CLI / Web / 其它进程）不能插入——两次并发导入
    各自在锁外通过体积复核后先后写入，合计越限会让账号此后永久无法
    导出。拿不到锁按可重试的持久化失败上报（导入幂等，稍后重试安全）。
    """
    db_path = getattr(store, "db_path", None)
    advisory = getattr(store, "_advisory_lock", None)
    user_id = str(getattr(store, "user_id", "") or "")
    if isinstance(db_path, str) and db_path:
        from utils.file_lock import file_lock

        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(file_lock(f"{db_path}.import"))
            except TimeoutError as exc:
                raise MemoryPersistenceError(
                    f"另一个导入正在进行，请稍后重试：{exc}"
                ) from exc
            yield
    elif callable(advisory):
        with advisory(f"aligo-import!{user_id}"):
            yield
    else:
        yield


def import_user_data(
    store: Any, sections: dict[str, Any], *, knowledge_unverified: str = ""
) -> dict[str, Any]:
    """把解析后的数据写入长期记忆；返回各类计数（供 CLI / Web 输出）。

    ``knowledge_unverified`` 由合并体积复核给出（见
    :func:`merged_backup_limit_problem`）：现有知识库读不到 / 本次分片
    规划失败时知识轴无法核对，此时跳过知识段写入并上报
    ``knowledge_error``，绝不把全局知识库无上限写下去（推过导出上限后
    所有用户的带知识库导出永久 413）。用户数据恢复不受影响，重跑可
    补齐知识片段（导入幂等）。

    写入全程持有跨进程导入互斥锁，并在锁内重取快照重跑合并体积复核：
    锁外预检挡不住两个并发导入各自通过后合计越限（此后导出永久 413）。
    """
    # 存量 + 本备份不能越过导出上限：否则恢复后 /api/export 与 CLI
    # user-export 永久 413 / 中止，该账号再也导不出备份（上限要避免的正是
    # 这种不可恢复状态）。重复行按去重语义不计入新增，见 _enforce_import_limits
    _enforce_import_limits(store, sections)
    with _import_exclusion(store):
        return _import_user_data_locked(store, sections, knowledge_unverified)


def _import_user_data_locked(
    store: Any, sections: dict[str, Any], knowledge_unverified: str
) -> dict[str, Any]:
    """导入互斥锁内的实际写入（含锁内体积复核）。"""
    # 锁内重取快照并重跑合并体积复核：另一个导入可能已在本次锁外预检
    # 之后写入，合计体积越限会让账号此后永久无法导出全量备份。体积按
    # 导出落盘的 LF 口径（CLI / Web 导出都写 LF，与调用方预检一致）
    late_unverified: dict[str, str] = {}
    late_problem = merged_backup_limit_problem(
        store,
        sections,
        crlf=False,
        knowledge_unverified=late_unverified,
    )
    if late_problem:
        raise ImportVolumeError(
            f"恢复中止：{late_problem}（恢复后账号导出的备份将超过导入"
            "上限，请先清理部分数据后重试）"
        )
    if late_unverified and sections.get("knowledge"):
        knowledge_unverified = late_unverified.get(
            "reason", knowledge_unverified
        )
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
    if knowledge and knowledge_unverified:
        # 合并复核无法核对知识体积：跳过写入并如实上报（用户数据已恢复，
        # 重跑补全知识片段即可）
        result["knowledge_error"] = knowledge_unverified
    elif knowledge:
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