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
# 公开常量：导入端据此拒绝「比导出保存得更深」的内容，保证
# 导入成功的数据下一次导出不会被静默截断（备份链不丢数据）。
MAX_JSON_DEPTH = 50


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
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                # 键里的 NUL 同样会被导入端整条拒绝、面板也无法显示 / 删除：
                # 清理必须覆盖键（此前只清值，含 NUL 的偏好键导出成功、恢复丢失）
                cleaned[key.replace("\x00", "")] = strip_nul(item)
            else:
                cleaned[key] = strip_nul(item)
        return cleaned
    return value


def sanitize_json(
    value: Any, _depth: int = 0, *, strip_nuls: bool = False
) -> Any:
    """把 NaN / Infinity 归一为 None、孤立代理项转义（递归处理列表与字典）。

    嵌套超过 ``MAX_JSON_DEPTH`` 的子树返回 None：这类结构在序列化 /
    响应的其它环节同样会触发 RecursionError，截断比整条接口 500 可用。

    ``strip_nuls=True`` 在同一次遍历里顺带移除字符串值与字典键中的 NUL
    （备份导出需要这一遍）：此前清洗后再整树跑一次 ``strip_nul``，大备份
    要多付一份全量拷贝与约百毫秒 CPU。
    """
    if _depth >= MAX_JSON_DEPTH:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        text = _safe_text(value)
        return text.replace("\x00", "") if strip_nuls else text
    if isinstance(value, list):
        return [
            sanitize_json(item, _depth + 1, strip_nuls=strip_nuls)
            for item in value
        ]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                key_text = _safe_text(key)
                if strip_nuls:
                    key_text = key_text.replace("\x00", "")
                cleaned[key_text] = sanitize_json(
                    item, _depth + 1, strip_nuls=strip_nuls
                )
            else:
                cleaned[key] = sanitize_json(
                    item, _depth + 1, strip_nuls=strip_nuls
                )
        return cleaned
    return value
