"""会话 Markdown 渲染（全部会话导出使用）。"""
import re
from datetime import datetime
from typing import Any

from utils.console import strip_terminal_controls
from utils.stored_message import render_stored_message
from utils.timestamps import chronological_key

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _escape_inline_markdown(text: str) -> str:
    """转义单行文本里的 Markdown / HTML 元字符（代码内容不要经过本函数）。

    标题行与引用块外的正文：链接 / 图片语法（`[x](url)`、`![x](url)`）与
    尖括号会让导出文件在允许 HTML 的预览器里变成可点击钓鱼链接、自动加载
    远程图片或执行原始标签；反斜杠转义在渲染后按原字面显示。
    """
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "\\[]()":
            out.append("\\" + char)
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif char == "!" and index + 1 < len(text) and text[index + 1] == "[":
            out.append("\\!")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def sanitize_title(title: str) -> str:
    """导出用标题清洗：换行会伪造导出结构，Markdown / HTML 元字符转义。"""
    text = " ".join(str(title or "").split())
    return _escape_inline_markdown(text)


def _escape_body_line(line: str) -> str:
    """引用块内、围栏外的正文行：行内代码之外才转义。

    CommonMark 在行内代码里不解码实体：无条件把 ``<`` 换成 ``&lt;`` 会让
    导出的示例代码被改写（复制出来是错误源码），因此按反引号开合跳过。
    """
    out = []
    index = 0
    in_code = False
    fence_run = 0
    while index < len(line):
        char = line[index]
        if char == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - index
            if not in_code:
                in_code = True
                fence_run = run
            elif run == fence_run:
                in_code = False
            out.append(line[index:end])
            index = end
            continue
        if in_code:
            out.append(char)
            index += 1
            continue
        if char in "\\[]()":
            out.append("\\" + char)
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif char == "!" and index + 1 < len(line) and line[index + 1] == "[":
            out.append("\\!")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _quote_line(line: str) -> str:
    return f"> {line}" if line else ">"


def sanitize_body(text: str) -> str:
    """把消息正文渲染为引用块（每行以 ``> `` 开头）。

    - 结构隔离：正文里伪造的 ``---`` / ``## 标题`` / ``- 会话：…`` /
      ``### Aligo`` 只会是引用块内的文字，不能制造顶层会话段落或角色标题；
    - 代码围栏平衡：消息内未闭合的 ``` / ~~~ 在本条消息末尾补齐，后续
      消息与会话不会被吞进同一个代码块；围栏内不做任何转义（代码保真）；
    - 行内代码之外仍转义尖括号与链接 / 图片语法，避免预览器执行原始 HTML
      或自动加载远程图片；终端控制序列（ESC / OSC）一并剥离。
    """
    text = strip_terminal_controls(str(text)).strip()
    rendered: list[str] = []
    fence = ""
    for line in text.split("\n"):
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            rendered.append(_quote_line(line))
            continue
        if fence:
            rendered.append(_quote_line(line))
            continue
        rendered.append(_quote_line(_escape_body_line(line)))
    if fence:
        rendered.append(_quote_line(fence))
    return "\n".join(rendered)


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
