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
    ``", ".join`` 报 TypeError。
    """
    merged: list[Any] = (
        list(current) if isinstance(current, list) else ([current] if current else [])
    )
    incoming = value if isinstance(value, list) else [value]
    for item in incoming:
        if item not in merged:
            merged.append(item)
    if not merged:
        merged = [value]
    return merged
