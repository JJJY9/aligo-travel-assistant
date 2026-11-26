"""会话 Markdown 渲染（全部会话导出使用）。"""
import re
from datetime import datetime
from typing import Any

from utils.console import strip_terminal_controls
from utils.session_title import session_title_from_content
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


def _fence_like(line: str, fence: str) -> bool:
    """任意缩进（含制表符 / 4+ 空格）的围栏形行：渲染器可能在此关闭围栏。

    ``_fence_close`` 只能证伪 0-3 个空格缩进的顶层形态；列表项等内容
    容器的相对缩进、制表符按 tab stop 展开后都可能让这一形态在渲染器
    一侧已是合法关围栏，而导出器仅凭文本无法确定。这类行一律按「可能
    关闭」处理：本行保留原样（在渲染器里也是围栏行），其后内容转义。
    """
    if not fence:
        return False
    body = line.lstrip(" \t")
    if not body.startswith(fence[0]):
        return False
    run = len(body) - len(body.lstrip(fence[0]))
    return run >= len(fence) and not body[run:].strip()


def _escape_inline_markdown(text: str) -> str:
    """转义单行文本里的 Markdown / HTML 元字符（代码内容不要经过本函数）。

    标题行与引用块外的正文：链接 / 图片语法（`[x](url)`、`![x](url)`）与
    尖括号会让导出文件在允许 HTML 的预览器里变成可点击钓鱼链接、自动加载
    远程图片或执行原始标签；反斜杠转义在渲染后按原字面显示。

    用 C 层 ``str.replace`` 链而不是逐字符 Python 循环：导出允许单条
    50MB 级消息，逐字符追加列表会在「全部是可转义字符」的合法备份上
    放大出数百 MB 内存与数十秒 CPU。替换顺序与逐字符语义逐字节等价。
    """
    # 反斜杠最先转义，后续插入的转义反斜杠不会被二次转义
    text = text.replace("\\", "\\\\")
    # 图片语法先于单独的 "[" 处理：两者都会再转义那个 "["
    text = text.replace("![", "\\![")
    for char in "[]()`":
        text = text.replace(char, "\\" + char)
    return text.replace("<", "&lt;").replace(">", "&gt;")


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
      消息与会话不会被吞进同一个代码块；缩进围栏（列表项内）随容器结束
      恢复转义；缩进更小的合法关围栏优先按关闭处理（不能重新当成新开围栏）；
    - 围栏状态一旦出现无法判定的歧义（缩进变小、4+ 空格或制表符缩进的
      围栏形行），本条消息余下内容全部转义：渲染器可能已按容器相对缩进
      关闭了围栏，原样透传会放行原始 HTML；若它仍认为在围栏内，多转义
      只损失代码样式，不会破坏安全。歧义后不再开 / 关围栏，避免把真实
      关围栏重新解析成新开围栏；
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
    # 围栏状态一旦出现无法判定的歧义（缩进变小、更深缩进或 Tab 的围栏
    # 形行），本条消息余下内容一律转义：渲染器可能已随容器结束退出围栏，
    # 原样输出会放行原始 HTML；若它仍在围栏内，多转义只是显示成字面量
    degraded = False
    # 超大消息放弃行内代码配对（线性、始终安全）；常规消息保留同一行内配对
    fast_mode = len(text) > _MAX_INLINE_PAIRING_CHARS

    def _escaped_line(line: str) -> str:
        return _quote_line(
            _escape_inline_markdown(line)
            if fast_mode
            else _escape_body_line(line)
        )

    for line in lines:
        if degraded:
            rendered.append(_escaped_line(line))
            continue
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
                # 缩进变小且非合法关围栏：无法判定渲染器是随容器结束退出
                # 了围栏（列表项），还是仍在围栏内（顶层 1-3 空格缩进的
                # 围栏不会因普通行缩进变小而结束）。余下内容只能转义——
                # 原样输出会在渲染器已退出围栏时放行原始 HTML
                degraded = True
                fence = ""
                fence_indent = 0
                rendered.append(_escaped_line(line))
                continue
            if _fence_close(line, fence):
                fence = ""
                fence_indent = 0
                rendered.append(_quote_line(line))
                continue
            if _fence_like(line, fence):
                # 列表内容缩进 / 制表符展开后的围栏形行：渲染器可能按
                # 容器相对缩进把它当关围栏，此后即为正文——保留本行后
                # 进入降级转义，余下内容不再原样透传
                degraded = True
                fence = ""
                fence_indent = 0
                rendered.append(_quote_line(line))
                continue
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
            text = session_title_from_content(message.get("content", ""))
            if text:
                # 回退标题同样来自用户输入：与自定义标题一样清洗，
                # 否则 <script> 之类会原样落进 `## ` 标题行
                return sanitize_title(text[:40])
    return "Aligo 对话"


def _latest_timestamp(messages: list[dict[str, Any]]) -> str:
    """会话内最大消息时间戳（与 /api/sessions、cli sessions 同一口径）。"""
    latest = ""
    latest_key = ""
    seen = False
    for message in messages:
        timestamp = str(message.get("timestamp") or "")
        if not timestamp:
            continue
        key = chronological_key(timestamp)
        # 严格 >：并列时刻保留先出现者；用 seen 判断首条而不是「key 为空」，
        # 全非法时间戳的会话不能从首条退化成末条
        if not seen or key > latest_key:
            seen = True
            latest = timestamp
            latest_key = key
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
        # 与 Web 列表 / CLI sessions 同口径：整数 0（以及 False）是合法
        # 会话，只有 None 与纯空白串视为无归属——按 truthiness 归一会把
        # 0 折成空串，合集导出（备份）静默丢掉整段会话
        session_id = message.get("session_id")
        if session_id is None or (
            isinstance(session_id, str) and not session_id.strip()
        ):
            continue
        grouped.setdefault(str(session_id), []).append(message)
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
    *,
    sessions: list[tuple[str, list[dict[str, Any]]]] | None = None,
) -> tuple[str, int]:
    """把全部会话渲染为一份 Markdown，返回 (文本, 会话数)。

    ``sessions`` 传入调用方已算好的分组时直接复用（CLI 列表 / 导出同一份
    确定性分组），不再对全量消息做第二遍分组与最大时间戳解析。
    """
    titles = titles or {}
    if sessions is None:
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
