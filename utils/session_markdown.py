"""会话 Markdown 渲染（全部会话导出使用）。"""
from datetime import datetime
from typing import Any

from utils.stored_message import render_stored_message
from utils.timestamps import chronological_key


def sanitize_title(title: str) -> str:
    """导出用标题清洗：换行会伪造导出结构，尖括号转义避免当 HTML 落盘。"""
    text = " ".join(str(title or "").split())
    return text.replace("<", "&lt;").replace(">", "&gt;")


def sanitize_body(text: str) -> str:
    """导出正文的尖括号转义：<script> 等标签不能在允许 HTML 的 Markdown
    预览器里执行 / 伪造结构（正文里的正常对比符按实体显示，渲染器下不变）。"""
    return str(text).replace("<", "&lt;").replace(">", "&gt;")


def _session_title(
    session_id: str, messages: list[dict[str, Any]], titles: dict[str, str]
) -> str:
    title = str(titles.get(session_id) or "").strip()
    if title:
        return sanitize_title(title)
    for message in messages:
        if message.get("role") == "user":
            text = " ".join(str(message.get("content", "")).split())
            if text:
                # 回退标题同样来自用户输入：与自定义标题一样清洗，
                # 否则 <script> 之类会原样落进 `## ` 标题行
                return sanitize_title(text[:40])
    return "Aligo 对话"


def _latest_timestamp(messages: list[dict[str, Any]]) -> str:
    """会话内最大消息时间戳（与 /api/sessions、cli sessions 同一口径）。"""
    latest = ""
    for message in messages:
        timestamp = str(message.get("timestamp") or "")
        if timestamp and (
            not latest or chronological_key(timestamp) > chronological_key(latest)
        ):
            latest = timestamp
    return latest


def group_sessions(
    messages: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """按 session_id 分组，保持会话内消息顺序；会话按「会话内最大时间戳」倒序。

    不能用转录最后一条消息的时间：恢复早于现有会话的备份后消息按导出顺序
    追加，末尾不一定是会话内最新的那条，导出会把会话排到最旧。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        session_id = str(message.get("session_id") or "")
        if not session_id:
            continue
        grouped.setdefault(session_id, []).append(message)
    ordered = sorted(
        grouped.items(),
        key=lambda item: chronological_key(_latest_timestamp(item[1])),
        reverse=True,
    )
    return ordered


def render_all_sessions_markdown(
    user_id: str,
    messages: list[dict[str, Any]],
    titles: dict[str, str] | None = None,
) -> tuple[str, int]:
    """把全部会话渲染为一份 Markdown，返回 (文本, 会话数)。"""
    titles = titles or {}
    sessions = group_sessions(messages)
    exported_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        "# Aligo 会话导出",
        "",
        f"- 用户：{user_id}",
        f"- 会话数：{len(sessions)}",
        f"- 导出时间：{exported_at}",
        "",
    ]
    for session_id, session_messages in sessions:
        title = _session_title(session_id, session_messages, titles)
        lines.extend(
            [
                "---",
                "",
                f"## {title}",
                "",
                f"- 会话：{session_id}",
                f"- 消息数：{len(session_messages)}",
                f"- 最近更新：{_latest_timestamp(session_messages) or '—'}",
                "",
            ]
        )
        for message in session_messages:
            is_user = message.get("role") == "user"
            role = "用户" if is_user else "Aligo"
            content = (
                str(message.get("content", ""))
                if is_user
                else render_stored_message(message)
            )
            lines.extend([f"### {role}", "", sanitize_body(content.strip()), ""])
    return "\n".join(lines), len(sessions)
