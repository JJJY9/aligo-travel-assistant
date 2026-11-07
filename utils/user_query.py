"""从 Orchestrator 的结构化输入中取出用户问题（技能智能体共用）。

``rewritten_query`` 缺失 / 为空 / null 时回退到原始用户提问，避免把整段
JSON（包含历史上下文）当作检索关键词或搜索词。输入形态包括字符串、
结构化 dict 与 agentscope content block 列表。
"""
import json
from typing import Any


def _content_text(content: Any) -> str:
    """把 content block 列表（agentscope 的 ``str | Sequence[ContentBlock]``）拼成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _extract_from_mapping(data: dict[str, Any]) -> str | None:
    """按 rewritten_query → user_query 的顺序取用户问题；都没有时返回 None。"""
    raw_context = data.get("context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    extracted = context.get("rewritten_query", data.get("rewritten_query", ""))
    if not isinstance(extracted, str) or not extracted.strip():
        extracted = context.get("user_query") if context else ""
    if not isinstance(extracted, str) or not extracted.strip():
        return None
    return extracted


def extract_user_query(content: Any) -> str:
    """返回用于检索 / 搜索的用户问题。

    非字符串输入不再直接 ``str()``（dict / content block 列表会变成
    Python repr 并被当作检索关键词）：dict 按结构化语义取字段，
    content block 列表拼接文本块后按字符串处理。
    """
    if isinstance(content, dict):
        extracted = _extract_from_mapping(content)
        if extracted is not None:
            return extracted
        inner = content.get("content")
        if isinstance(inner, (str, list, tuple)):
            return extract_user_query(inner)
        return ""
    if isinstance(content, (list, tuple)):
        content = _content_text(content)
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
    extracted = _extract_from_mapping(data)
    return extracted if extracted is not None else content
