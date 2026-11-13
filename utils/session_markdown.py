"""会话 Markdown 渲染（全部会话导出使用）。"""
import re
from datetime import datetime
from typing import Any

from utils.console import strip_terminal_controls
from utils.stored_message import render_stored_message
from utils.timestamps import chronological_key

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# 超大消息跳过行内代码配对：未配对反引号的逐行回扫在恶意构造（无空行 +
# 递增反引号串）下可退化为超线性。全部转义（围栏内容仍原样保留）是安全
# 且线性的降级——代价只是代码段样式丢失，不会放过原始 HTML
_MAX_INLINE_PAIRING_CHARS = 50_000


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
        if char in "\\[]()`":
            # 反引号同样转义：裸反引号在渲染器里可能与其它行 / 其它片段
            # 组成行内代码段，让本该转义的内容被当作代码原样输出
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


def _escape_body_line(line: str) -> str:
    """引用块内、围栏外的正文行：同一行内成对的行内代码之外全部转义。

    跨行行内代码不再保留：导出器对段落边界的推断与真实渲染器不完全一致
    （块起始行 / 表格 / 缩进代码块 / 转义后结构变化），一旦两边对反引号的
    配对位置判断不同，渲染器就会把本该转义的内容当代码段原样输出原始
    HTML。**未配对的反引号一律写成 ``\\```**（CommonMark 转义，显示为
    字面反引号）：渲染器不可能再拿它与其它行组成代码段。表格行与缩进
    代码块行（行首 4+ 空格）的上下文不可靠，反引号同样全部转义。
    """
    out: list[str] = []
    index = 0
    # 表格行 / 缩进代码块行的渲染上下文与普通段落不同：不保留行内代码，
    # 反引号全部转义（转义后不可能参与任何配对）
    no_pair = "|" in line or _leading_spaces(line) >= 4
    while index < len(line):
        char = line[index]
        if char == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - index
            close = None if no_pair else _find_backtick_run(line, end, run)
            if close is None:
                out.append("\\`" * run)
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
    return "".join(out)


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _quote_line(line: str) -> str:
    return f"> {line}" if line else ">"


def sanitize_body(text: str) -> str:
    """把消息正文渲染为引用块（每行以 ``> `` 开头）。

    - 结构隔离：正文里伪造的 ``---`` / ``## 标题`` / ``- 会话：…`` /
      ``### Aligo`` 只会是引用块内的文字，不能制造顶层会话段落或角色标题；
    - 代码围栏平衡：消息内未闭合的 ``` / ~~~ 在本条消息末尾补齐，后续
      消息与会话不会被吞进同一个代码块；缩进围栏（列表项内）随容器
      结束恢复转义；缩进更小的合法关围栏优先按关闭处理（不能重新当成
      新开围栏）；
    - 行内代码只保留同一行内成对的反引号；未配对反引号一律反斜杠转义
      （显示为字面反引号）：导出器对段落边界的推断与真实渲染器不可能
      完全一致，裸反引号一旦被渲染器与其它行配对，后续内容会被当作代码
      原样输出原始 HTML；
    - 行内代码之外仍转义尖括号与链接 / 图片语法，避免预览器执行原始 HTML
      或自动加载远程图片；终端控制序列（ESC / OSC）一并剥离；
    - 超过 :data:`_MAX_INLINE_PAIRING_CHARS` 的消息跳过行内代码配对、
      正文全部转义（围栏内的代码行仍原样保留）：配对扫描在恶意构造下
      是超线性的，多转义永远安全，只损失代码段样式。
    """
    text = strip_terminal_controls(str(text)).strip()
    lines = text.split("\n")
    rendered: list[str] = []
    fence = ""
    fence_indent = 0
    # 超大消息放弃行内代码配对（线性、始终安全）；常规消息保留同一行内配对
    fast_mode = len(text) > _MAX_INLINE_PAIRING_CHARS
    for line in lines:
        if fence:
            if (
                fence_indent
                and line.strip()
                and _leading_spaces(line) < fence_indent
            ):
                if _fence_close(line, fence):
                    # 缩进更小的合法关围栏：渲染器在这里关闭围栏，必须
                    # 先按关闭处理（否则会被当成容器结束，再被重新解析
                    # 为新开围栏，后续行被当作围栏内容原样输出）
                    rendered.append(_quote_line(line))
                    fence = ""
                    fence_indent = 0
                    continue
                # 列表项等容器内的围栏随容器结束：后续未缩进行是正文，
                # 必须恢复转义（渲染器此时已退出围栏/列表上下文）
                fence = ""
                fence_indent = 0
            else:
                if _fence_close(line, fence):
                    fence = ""
                    fence_indent = 0
                rendered.append(_quote_line(line))
                continue
        opener = _fence_open(line)
        if opener:
            fence = opener
            fence_indent = _leading_spaces(line)
            rendered.append(_quote_line(line))
            continue
        if fast_mode:
            # 超过配对阈值：行内反引号不再参与配对，连同尖括号全部转义
            rendered.append(_quote_line(_escape_inline_markdown(line)))
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
                f"- 会话：{sanitize_title(session_id)}",
                f"- 消息数：{len(session_messages)}",
                f"- 最近更新：{sanitize_title(_latest_timestamp(session_messages) or '—')}",
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
