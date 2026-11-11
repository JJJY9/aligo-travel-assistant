"""
长期记忆消息渲染

助手消息存的是编排结果 JSON（`results` 或 `status`），展示前需要重新渲染为
Markdown；用户消息原样返回。Web API 与 CLI 导出共用本模块，避免两套逻辑漂移。
"""
import json
from typing import Any

from utils.json_safe import sanitize_json
from utils.result_formatter import result_to_markdown


def _safe_output(text: str) -> str:
    """渲染结果统一转义孤立代理项。

    旧记忆 / 手写文件里的 assistant 内容是 JSON 文本，``json.loads`` 会把
    ``\\ud800`` 还原成真代理项：直接放进 JSON / HTML / Markdown 响应会在
    UTF-8 编码时抛 UnicodeEncodeError，让该会话每次打开 / 导出都 500。
    """
    return sanitize_json(text)


def render_stored_message(message: dict[str, Any]) -> str:
    """把历史消息转换为可展示文本：助手消息为编排 JSON 时重新渲染为 Markdown。"""
    role = message.get("role", "assistant")
    content = str(message.get("content", ""))
    if role != "assistant":
        return _safe_output(content)
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        # 深度嵌套（RecursionError）与超 4300 位整数字面量（普通 ValueError，
        # 不是 JSONDecodeError）的历史内容会让会话查看 / 搜索 / 导出永久
        # 500：一律按旧格式原样返回
        return _safe_output(content)
    status = data.get("status") if isinstance(data, dict) else None
    if isinstance(data, dict) and (
        "results" in data
        or (
            isinstance(status, str)
            and status in {"completed", "partial_failure", "no_agents"}
        )
    ):
        try:
            return _safe_output(result_to_markdown(data))
        except Exception:  # noqa: BLE001 - 渲染失败时退回原始内容
            return _safe_output(content)
    return _safe_output(content)
