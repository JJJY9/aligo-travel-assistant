"""偏好值合并（列表语义，JSON / PostgreSQL / 编排器共用）。"""
from typing import Any


def has_preference_value(value: Any) -> bool:
    """偏好是否有实际值。

    0 / False 是合法偏好（如预算为 0、不要靠窗），只有 None、空串、
    空列表 / 空字典视为「未设置」。
    """
    return value is not None and value != "" and value != [] and value != {}


def merge_preference_value(current: Any, value: Any) -> list[Any]:
    """把新值合并进现有偏好，返回去重后的列表（保持先后顺序）。

    模型可能给出列表形式的 append 值（如 ``["汉庭", "如家"]``）；逐项合并
    而不是把整个列表塞进列表，避免存储出现嵌套列表并让后续的
    ``", ".join`` 报 TypeError。``0`` / ``False`` 是合法偏好（预算为 0、
    不要靠窗），不能用真值判断丢弃。
    """
    merged: list[Any] = (
        list(current)
        if isinstance(current, list)
        else ([current] if has_preference_value(current) else [])
    )
    incoming = value if isinstance(value, list) else [value]
    for item in incoming:
        if item not in merged:
            merged.append(item)
    # 兜底只在「新值本身有意义」时启用：value 为空列表时按未设置返回 []，
    # 否则会把 [] 塞进列表得到 [[]]（正是本函数要避免的嵌套列表）
    if not merged and has_preference_value(value):
        merged = [value]
    return merged
