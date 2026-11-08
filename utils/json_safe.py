"""JSON 安全序列化辅助。

历史数据里可能残留 NaN / Infinity（Python 扩展，不是合法 JSON），直接
json.dumps 会输出裸 NaN/Infinity，标准解析器与 /api/status 之外的下游
都无法消费。孤立代理项（``\\ud800``）同理：json.dumps 能编码成转义文本，
但 Starlette JSONResponse（ensure_ascii=False）在 UTF-8 编码响应时抛
UnicodeEncodeError，表现为该接口永久 500。
"""
import math
from typing import Any


def _safe_text(text: str) -> str:
    """把无法 UTF-8 编码的字符串（孤立代理项）转成等价的转义文本。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


def sanitize_json(value: Any) -> Any:
    """把 NaN / Infinity 归一为 None、孤立代理项转义（递归处理列表与字典）。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, dict):
        return {
            _safe_text(key) if isinstance(key, str) else key: sanitize_json(item)
            for key, item in value.items()
        }
    return value
