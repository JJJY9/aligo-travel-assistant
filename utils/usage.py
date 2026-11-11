"""用量账目合并：失败 / 未落库回合的 token 结算。

模型用量是按会话累计的（只增不减）；失败回合若不计入任何消息，下一轮的
增量快照会把它们一起减掉。Web 进度与 CLI 展示都通过本模块把暂存的用量
并入下一轮成功回合，避免永久少记。
"""
from typing import Any

Usage = dict[str, Any]


def add_usage(total: Usage | None, usage: Usage | None) -> Usage | None:
    """把 ``usage`` 累加到 ``total``（数值字段逐项相加，None 原样返回另一侧）。"""
    if not usage:
        return total
    merged = dict(total or {})
    for key, value in usage.items():
        merged[key] = merged.get(key, 0) + value
    return merged
