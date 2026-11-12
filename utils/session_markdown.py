"""会话 Markdown 渲染（全部会话导出使用）。"""
import re
from datetime import datetime
from typing import Any

from utils.console import strip_terminal_controls
from utils.stored_message import render_stored_message
from utils.timestamps import chronological_key

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def _fence_open(line: str) -> str:
    """CommonMark 合法开围栏标记（非法时返回空串）。

    - 最多 3 个空格缩进（4 空格是缩进代码块，不是围栏）；
    - 反引号围栏的 info string 不能包含反引号（```js`x 在 CommonMark 里
      只是普通段落，按围栏处理后会把后续行错误地跳过转义）。
    """
    match = _FENCE_RE.match(line)
    if not match:
        return ""
    marker = match.group(2)
    info = match.group(3)
    if marker[0] == "`" and "`" in info:
        return ""
    return marker


def _fence_close(line: str, fence: str) -> bool:
    """CommonMark 合法关围栏：同字符、长度不小于开围栏，其后只允许空白。

    带尾随文字的 ``\\`\\`\\` 说明`` 不是关围栏——误判会把真实关闭围栏当成
    新开围栏，之后的行完全跳过转义。
    """
    match = _FENCE_RE.match(line)
    if not match:
        return False
    marker = match.group(2)
    if marker[0] != fence[0] or len(marker) < len(fence):
        return False
    return not match.group(3).strip()


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
    text = " ".join(strip_terminal_controls(str(title or "")).split())
    return _escape_inline_markdown(text)


def _find_backtick_run(text: str, start: int, run: int) -> int | None:
    """``text[start:]`` 中第一个长度恰好为 ``run`` 的反引号串起点。"""
    index = text.find("`", start)
    while index != -1:
        end = index
        while end < len(text) and text[end] == "`":
            end += 1
        if end - index == run:
            return index
        index = text.find("`", end)
    return None


_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_BLOCKQUOTE_RE = re.compile(r"^ {0,3}>")
_LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")
_HTML_BLOCK_RE = re.compile(r"^ {0,3}<[A-Za-z/!?]")
# Setext 标题下划线（紧跟段落行时把段落变成标题）与主题分隔线同样结束段落：
# 只认纯 ``=`` / ``-``（可带尾随空白）与 ``***`` / ``___`` / 空格分隔的
# 连字符线；``- -`` 这类按列表项处理（_LIST_ITEM_RE 已覆盖）
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_THEMATIC_BREAK_RE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)


def _starts_block(line: str) -> bool:
    """该行是否开启一个能打断段落的块（CommonMark 块级起始）。

    跨行行内代码只能闭合于同一段落内：导出时每行前缀 ``> ``，标题、引用、
    列表、HTML 块、Setext 下划线与主题分隔线在引用块内仍是真实块，会打断
    上一行所处的段落。若把块起始行上的反引号当成关闭串，上一行剩余内容会
    被当作代码而不转义——攻击者用 ``x` <img onerror=…>`` 加 ``# ` ``（或
    ``---`` / ``***`` 等再跟一行反引号）即可让原始 HTML 落进导出文件。
    列表起始号的精确规则（有序列表须从 1 开始才能打断段落）不做区分：多认
    块起始只会让跨行代码段内容被保守转义（可见实体），不会放过原始 HTML。
    """
    return bool(
        _ATX_HEADING_RE.match(line)
        or _BLOCKQUOTE_RE.match(line)
        or _LIST_ITEM_RE.match(line)
        or _HTML_BLOCK_RE.match(line)
        or _SETEXT_UNDERLINE_RE.match(line)
        or _THEMATIC_BREAK_RE.match(line)
    )


def _closing_run_follows(
    lines: list[str], line_index: int, run: int
) -> bool:
    """后续行（同一段落内）是否存在等长反引号串（跨行代码段）。"""
    for index in range(line_index + 1, len(lines)):
        text = lines[index]
        if not text.strip():
            return False
        if _fence_open(text):
            return False
        if _starts_block(text):
            return False
        if _find_backtick_run(text, 0, run) is not None:
            return True
    return False


def _escape_body_line(
    line: str, lines: list[str] | None = None, line_index: int = 0
) -> tuple[str, int]:
    """引用块内、围栏外的正文行：行内代码之外才转义。

    返回 ``(转义后的文本, 跨行代码段待关闭的反引号长度)``。CommonMark 在
    行内代码里不解码实体：无条件把 ``<`` 换成 ``&lt;`` 会让导出的示例代码
    被改写（复制出来是错误源码），因此按反引号开合跳过；但**未配对**的
    反引号在 CommonMark 里只是普通字符——不能据此把整行剩余内容当成代码
    而不转义（``价格低`于 <img onerror>`` 会原样输出原始 HTML）。打开
    代码段前必须具备「同行或后续段落行存在等长关闭串」，否则按字面字符
    处理、照常转义。
    """
    out: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - index
            close = _find_backtick_run(line, end, run)
            if close is None:
                if (
                    lines is not None
                    and _closing_run_follows(lines, line_index, run)
                ):
                    # 跨行代码段：本行剩余内容都属于代码，原样保留
                    out.append(line[index:])
                    return "".join(out), run
                # 未配对反引号只是字面字符：保留后继续转义其余内容
                out.append(line[index:end])
                index = end
                continue
            out.append(line[index : close + run])
            index = close + run
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
    return "".join(out), 0


def _carry_code_line(line: str, run: int) -> tuple[str, int]:
    """跨行代码段的后续行：原样保留到等长关闭串，之后恢复转义。"""
    close = _find_backtick_run(line, 0, run)
    if close is None:
        return line, run
    rest, pending = _escape_body_line(line[close + run :])
    return line[: close + run] + rest, pending


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
    lines = text.split("\n")
    rendered: list[str] = []
    fence = ""
    pending_code_run = 0
    for line_index, line in enumerate(lines):
        if fence:
            if _fence_close(line, fence):
                fence = ""
            rendered.append(_quote_line(line))
            continue
        opener = _fence_open(line)
        if opener:
            fence = opener
            rendered.append(_quote_line(line))
            continue
        if pending_code_run:
            rendered_line, pending_code_run = _carry_code_line(
                line, pending_code_run
            )
            rendered.append(_quote_line(rendered_line))
            continue
        rendered_line, pending_code_run = _escape_body_line(
            line, lines, line_index
        )
        rendered.append(_quote_line(rendered_line))
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
