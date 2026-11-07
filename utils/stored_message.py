"""
长期记忆消息渲染

助手消息存的是编排结果 JSON（`results` 或 `status`），展示前需要重新渲染为
Markdown；用户消息原样返回。Web API 与 CLI 导出共用本模块，避免两套逻辑漂移。
"""
import json
from typing import Any

from utils.result_formatter import result_to_markdown


def render_stored_message(message: dict[str, Any]) -> str:
    """把历史消息转换为可展示文本：助手消息为编排 JSON 时重新渲染为 Markdown。"""
    role = message.get("role", "assistant")
    content = str(message.get("content", ""))
    if role != "assistant":
        return content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    status = data.get("status") if isinstance(data, dict) else None
    if isinstance(data, dict) and (
        "results" in data
        or (
            isinstance(status, str)
            and status in {"completed", "partial_failure", "no_agents"}
        )
    ):
        try:
            return result_to_markdown(data)
        except Exception:  # noqa: BLE001 - 渲染失败时退回原始内容
            return content
    return content
