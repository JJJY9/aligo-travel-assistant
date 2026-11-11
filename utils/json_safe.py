"""JSON 安全序列化辅助。

历史数据里可能残留 NaN / Infinity（Python 扩展，不是合法 JSON），直接
json.dumps 会输出裸 NaN/Infinity，标准解析器与 /api/status 之外的下游
都无法消费。孤立代理项（``\\ud800``）同理：json.dumps 能编码成转义文本，
但 Starlette JSONResponse（ensure_ascii=False）在 UTF-8 编码响应时抛
UnicodeEncodeError，表现为该接口永久 500。
"""
import math
from typing import Any

# 递归深度上限：历史 / 手写数据可能带数千层嵌套的 _meta，继续递归会
# RecursionError——会话详情、状态与导出接口对这类记录会永久 500。
# 上限取 50：pydantic-core 的响应序列化在约 90 层触发
# 「Circular reference detected (depth exceeded)」，截断必须低于该限制，
# 否则清洗后的结构仍会在响应序列化阶段 500。超出部分按 None 截断
# （与 NaN / Infinity 同一口径），其余数据仍可返回。
_MAX_JSON_DEPTH = 50


def _safe_text(text: str) -> str:
    """把无法 UTF-8 编码的字符串（孤立代理项）转成等价的转义文本。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


def strip_nul(value: Any) -> Any:
    """递归移除字符串中的 NUL。

    PostgreSQL 的 JSONB / TEXT 都拒绝 `\\u0000`：模型输出里的 NUL 会让
    偏好 / 行程写入确定性失败（被逐条吞掉、回复却宣称已保存），JSON 后端
    却能保存——同一对话跨后端行为分叉。写入口径与 Web 请求 / 备份恢复
    一致：入库前剔除。
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_nul(item) for key, item in value.items()}
    return value


def sanitize_json(value: Any, _depth: int = 0) -> Any:
    """把 NaN / Infinity 归一为 None、孤立代理项转义（递归处理列表与字典）。

    嵌套超过 ``_MAX_JSON_DEPTH`` 的子树返回 None：这类结构在序列化 /
    响应的其它环节同样会触发 RecursionError，截断比整条接口 500 可用。
    """
    if _depth >= _MAX_JSON_DEPTH:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [sanitize_json(item, _depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            _safe_text(key) if isinstance(key, str) else key: sanitize_json(
                item, _depth + 1
            )
            for key, item in value.items()
        }
    return value
