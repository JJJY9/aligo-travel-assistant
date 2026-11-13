"""偏好值合并（列表语义，JSON / PostgreSQL / 编排器共用）。"""
import re
from typing import Any

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
    text = pref_type.strip()
    if re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", text):
        # 控制字符 / 行分隔符会被写进 INFO 日志（%0A 伪造日志行）与面板路径
        return False
    return 0 < len(text) <= 64 and "/" not in text
