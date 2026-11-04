"""从 Orchestrator 的结构化输入中取出用户问题（技能智能体共用）。

``rewritten_query`` 缺失 / 为空 / null 时回退到原始用户提问，避免把整段
JSON（包含历史上下文）当作检索关键词或搜索词。
"""
import json
from typing import Any


def extract_user_query(content: Any) -> str:
    """返回用于检索 / 搜索的用户问题。"""
    if not isinstance(content, str):
        return str(content or "")
    if not content.strip().startswith("{"):
        return content
    try:
        data = json.loads(content)
    except Exception:  # noqa: BLE001 - 非 JSON 时按原文处理
        return content
    if not isinstance(data, dict):
        return content
    raw_context = data.get("context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    extracted = context.get("rewritten_query", data.get("rewritten_query", ""))
    if not isinstance(extracted, str) or not extracted.strip():
        extracted = context.get("user_query") if context else ""
    if not isinstance(extracted, str) or not extracted.strip():
        extracted = content
    return extracted
