"""偏好值合并（列表语义，JSON / PostgreSQL / 编排器共用）。"""
import json
import re
from typing import Any

# 单个偏好值序列化后的字符上限：与 Web PUT /api/preferences 的
# _PREFERENCE_VALUE_MAX_JSON、CLI 偏好编辑、备份恢复同一口径。超过后
# 该偏好在面板 / CLI 永远无法编辑，还会持续撑大每轮意图 prompt。
PREFERENCE_VALUE_MAX_CHARS = 10000

# 旧版字典格式偏好里的说明性键：模型附带 note/message 等时不能被当成
# 偏好类型落库 / 回显 / 进入后续批次上下文（三处消费口径必须一致）
EXPLANATORY_PREF_KEYS = frozenset(
    {
        "has_preferences",
        "error",
        "note",
        "notes",
        "message",
        "reason",
        "reasoning",
        "summary",
        "explanation",
        "confidence",
        "status",
        "action",
    }
)


def _is_blank_item(value: Any) -> bool:
    """单个偏好值是否有实际内容：None / 空白串 / 空容器视为空。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def is_blank_pref_item(value: Any) -> bool:
    """单条偏好值是否为空的公开口径（合并、面板与 CLI 展示共用）。"""
    return _is_blank_item(value)


def has_preference_value(value: Any) -> bool:
    """偏好是否有实际值。

    0 / False 是合法偏好（如预算为 0、不要靠窗），只有 None、空串、空白串、
    空列表 / 空字典以及「列表里全是空值」（``[null]`` / ``[""]``）视为
    「未设置」——模型输出的 null 项不能绕过门禁落库。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(not _is_blank_item(item) for item in value)
    if isinstance(value, dict):
        return bool(value)
    return True


def merge_preference_value(current: Any, value: Any) -> list[Any]:
    """把新值合并进现有偏好，返回去重后的列表（保持先后顺序）。

    模型可能给出列表形式的 append 值（如 ``["汉庭", "如家"]``）；逐项合并
    而不是把整个列表塞进列表，避免存储出现嵌套列表并让后续的
    ``", ".join`` 报 TypeError。``0`` / ``False`` 是合法偏好（预算为 0、
    不要靠窗），不能用真值判断丢弃；``None`` / 空白项逐项过滤，不落库。
    """
    merged: list[Any] = (
        list(current)
        if isinstance(current, list)
        else ([current] if has_preference_value(current) else [])
    )
    # 顺带清理历史数据里已经存在的空值项（旧版本可能写入了 [null]）
    merged = [item for item in merged if not _is_blank_item(item)]
    incoming = value if isinstance(value, list) else [value]
    for item in incoming:
        if _is_blank_item(item):
            continue
        if item not in merged:
            merged.append(item)
    # 兜底只在「新值本身有意义」时启用：value 为空列表时按未设置返回 []，
    # 否则会把 [] 塞进列表得到 [[]]（正是本函数要避免的嵌套列表）
    if not merged and has_preference_value(value):
        merged = [value]
    return merged


def is_manageable_pref_type(pref_type: object) -> bool:
    """偏好键是否能被面板 / CLI 管理（与 Web ``^[^/]{1,64}$`` 同一约束）。

    含 "/" 的键在面板路径里会被拆成两段（404）、CLI 的字符集校验也会拒绝；
    落库、上下文合并与回复渲染必须用同一口径过滤，否则界面宣称「已更新」
    而长期记忆实际没有写入。
    """
    if not isinstance(pref_type, str):
        return False
    # 控制字符 / 行分隔符按原始字符串判定：strip 只用于空串 / 纯空白与
    # 「.」「..」判断；用剥离后的值校验会让 “home\n”（尾随换行）漏过——
    # 面板 DELETE / CLI 都按原始键校验，永远 422 / 拒绝，落库后成了脏键
    if re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", pref_type):
        # 控制字符 / 行分隔符会被写进 INFO 日志（%0A 伪造日志行）与面板路径
        return False
    text = pref_type.strip()
    if text in (".", ".."):
        # 面板用 /api/preferences/{encodeURIComponent(type)} 编辑 / 删除；
        # "." / ".." 不转义且会被浏览器 URL 解析规范化成别的路径（404），
        # 落库后只能靠 CLI 清理
        return False
    return 0 < len(text) <= 64 and "/" not in text


def shadow_preference_types(
    preferences: dict[str, Any], canonical: str
) -> list[str]:
    """返回与 ``canonical`` 仅差首尾空白的存量偏好键（影子键）。

    历史备份导入为防碰撞保留了「 home_location」这类原始键；写入规范键后
    影子行仍回显旧值、继续占类型名额，面板与 prompt 会同时出现两条互相
    矛盾的值。写入路径（面板 PUT / CLI pref-set）在保存规范键后应 best-effort
    删除这些影子键。
    """
    return [
        key
        for key in preferences
        if isinstance(key, str)
        and key != canonical
        and key.strip() == canonical
    ]


def visible_preferences(preferences: dict[str, Any]) -> dict[str, Any]:
    """面板 / 导出 / 上限预检共用的偏好可见口径。

    过滤 None / 空白串 / 空容器，列表内逐项剔除 null、空白项与 {} / []；
    全空的列表整体隐藏（此前 [null] / "   " 会原样返回，面板渲染成
    「东航、null」；空容器会让面板显示「汉庭、{}」且下一次追加时被合并层
    静默丢弃）。面板 / CLI 无法寻址的键（含 "/"、控制字符、"."/".."、超长）
    同样隐藏：PostgreSQL 后端加载时不做这类清洗，脏键经 /api/status 进入
    面板后既改不掉也删不掉，还会占用偏好类型上限（与 JSON 后端加载即丢弃
    同口径）。CLI 备份导出与偏好上限预检必须用同一过滤，否则含隐形行的
    账号会导出一份「导入时必被跳过」的备份、或误报超过 200 上限。
    """
    visible: dict[str, Any] = {}
    for key, value in preferences.items():
        if not is_manageable_pref_type(key):
            continue
        if isinstance(value, list):
            items = [item for item in value if not _is_blank_item(item)]
            if items:
                visible[key] = items
        elif has_preference_value(value):
            visible[key] = value
    return visible


# 偏好拼进 LLM prompt 时的预算：单值截断 + 总量截断。偏好单值上限
# 10000 字符、类型上限 200，全量原样拼接可达约 2MB，任何模型上下文都会
# 超限：该账号的偏好抽取与每轮意图识别会持续失败（不是单次抖动）。
PROMPT_PREFERENCE_VALUE_MAX_CHARS = 500
PROMPT_PREFERENCE_TEXT_MAX_CHARS = 4000


def _prompt_preference_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def preference_prompt_lines(
    preferences: object,
    *,
    bullet: str = "•",
    max_value_chars: int = PROMPT_PREFERENCE_VALUE_MAX_CHARS,
    max_total_chars: int = PROMPT_PREFERENCE_TEXT_MAX_CHARS,
) -> list[str]:
    """把偏好格式化为带预算的 prompt 行（``• 类型: 值``）。

    单值超过 ``max_value_chars`` 截断并加省略号；累计超过
    ``max_total_chars`` 后不再输出值，但对剩余类型仍保留「值已省略」行
    ——模型据此判断该字段已存在，不会把既有列表型偏好当首次设置而
    replace 覆盖；类型名也放不下时以「（更多偏好已省略）」收尾。
    """
    if not isinstance(preferences, dict):
        return []
    lines: list[str] = []
    used = 0
    for key, value in preferences.items():
        name = str(key)
        text = _prompt_preference_text(value)
        if len(text) > max_value_chars:
            text = text[:max_value_chars] + "…"
        line = f"{bullet} {name}: {text}"
        if used + len(line) + 1 > max_total_chars:
            line = f"{bullet} {name}: （值已省略）"
            if used + len(line) + 1 > max_total_chars:
                lines.append("（更多偏好已省略）")
                break
        lines.append(line)
        used += len(line) + 1
    return lines


def preference_value_problem(value: Any) -> str | None:
    """偏好值能否落库：必须可 JSON 序列化且序列化后不超过上限。

    返回问题描述（给日志 / 错误用），可落库时返回 None。上限与 Web PUT、
    CLI 偏好编辑、备份恢复同一口径——模型写入绕过上限后，该偏好会变成
    面板 / CLI 永远无法编辑的「僵尸值」，还会持续撑大每轮意图 prompt。
    """
    try:
        # 与前端 JSON.stringify 同一紧凑分隔符口径：默认的 ", " / ": "
        # 会让前端按 10000 放行的边界值在服务端被多算空格而 422
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "偏好值不是可序列化的 JSON"
    if len(serialized) > PREFERENCE_VALUE_MAX_CHARS:
        return (
            f"偏好值过大（序列化后超过 {PREFERENCE_VALUE_MAX_CHARS} 字符）"
        )
    return None
