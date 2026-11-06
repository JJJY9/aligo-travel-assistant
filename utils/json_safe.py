"""JSON 安全序列化辅助。

历史数据里可能残留 NaN / Infinity（Python 扩展，不是合法 JSON），直接
json.dumps 会输出裸 NaN/Infinity，标准解析器与 /api/status 之外的下游
都无法消费。
"""
import math
from typing import Any


def sanitize_json(value: Any) -> Any:
    """把 NaN / Infinity 归一为 None（递归处理列表与字典）。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_json(item) for key, item in value.items()}
    return value
