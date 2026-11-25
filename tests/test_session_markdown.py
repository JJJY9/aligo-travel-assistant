"""
全部会话 Markdown 导出渲染测试（无需 API Key、不访问网络）

运行：python tests/test_session_markdown.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.session_markdown import group_sessions, render_all_sessions_markdown


def _msg(role, content, session_id, timestamp):
    return {
        "role": role,
        "content": content,
        "session_id": session_id,
        "timestamp": timestamp,
    }


def test_group_sessions_orders_by_recent_update():
    messages = [
        _msg("user", "旧问题", "s1", "2026-11-01T10:00:00"),
        _msg("assistant", "旧回答", "s1", "2026-11-01T10:01:00"),
        _msg("user", "新问题", "s2", "2026-11-02T10:00:00"),
        _msg("assistant", "新回答", "s2", "2026-11-02T10:01:00"),
        {"role": "user", "content": "无会话消息", "session_id": "", "timestamp": ""},
    ]
    grouped = group_sessions(messages)
    assert [session_id for session_id, _ in grouped] == ["s2", "s1"]
    text, count = render_all_sessions_markdown("u1", messages, {"s2": "自定义标题"})
    assert count == 2
    assert "## 自定义标题" in text
    assert "## 旧问题" in text, "无标题时用第一条用户消息"
    assert "### 用户" in text and "### Aligo" in text
    assert "无会话消息" not in text, "没有 session_id 的消息不导出"
    assert "会话数：2" in text
    print("✓ 全部会话分组 / 排序 / 标题回退")


def test_custom_title_single_line_and_escaped():
    """自定义会话标题的换行 / 尖括号不能伪造导出结构或落盘为 HTML。"""
    from utils.session_markdown import render_all_sessions_markdown

    lines = [
        {"role": "user", "content": "你好", "session_id": "s1", "timestamp": "t0"}
    ]
    markdown, count = render_all_sessions_markdown(
        "u1", lines, {"s1": "第一行\n## 伪造标题 <script>"}
    )
    assert count == 1
    assert "## 第一行 ## 伪造标题 &lt;script&gt;" in markdown
    assert "\n## 伪造标题" not in markdown, "换行不能再造一个新的二级标题"
    assert "<script>" not in markdown
    print("✓ 会话标题单行化并转义")


def test_fallback_title_escaped_like_custom():
    """无自定义标题时首条用户消息同样清洗：尖括号不能原样落进标题行。"""
    lines = [
        {
            "role": "user",
            "content": "<script>alert(1)</script> 你好",
            "session_id": "s1",
            "timestamp": "t0",
        }
    ]
    markdown, count = render_all_sessions_markdown("u1", lines, {})
    assert count == 1
    assert "## &lt;script&gt;alert\\(1\\)&lt;/script&gt; 你好" in markdown
    assert "## <script>" not in markdown, "回退标题必须与自定义标题同样转义"
    print("✓ 回退标题与自定义标题同一清洗口径")


def test_message_body_escaped_in_export():
    """正文里的 <script> 不能原样落盘（允许 HTML 的预览器会执行）。"""
    from utils.session_markdown import render_all_sessions_markdown

    lines = [
        {
            "role": "user",
            "content": "<script>alert(1)</script>",
            "session_id": "s1",
            "timestamp": "t0",
        },
        {
            "role": "assistant",
            "content": "回答 <b>加粗</b>",
            "session_id": "s1",
            "timestamp": "t1",
        },
    ]
    markdown, _ = render_all_sessions_markdown("u1", lines, {})
    assert "> &lt;script&gt;alert\\(1\\)&lt;/script&gt;" in markdown
    assert "> 回答 &lt;b&gt;加粗&lt;/b&gt;" in markdown
    assert "<script>" not in markdown
    print("✓ 导出正文尖括号转义")


def test_group_sessions_uses_max_timestamp_within_session():
    """恢复旧备份后转录末尾不是最新消息：排序与「最近更新」必须用会话内最大值。"""
    messages = [
        _msg("user", "最新问题", "s1", "2026-11-05T10:00:00"),
        _msg("assistant", "最新回答", "s1", "2026-11-05T10:01:00"),
        # 恢复的旧消息按导出顺序追加在末尾（时间更早）
        _msg("user", "旧问题", "s1", "2026-01-01T10:00:00"),
        _msg("user", "中间会话", "s2", "2026-03-01T10:00:00"),
    ]
    grouped = group_sessions(messages)
    assert [session_id for session_id, _ in grouped] == ["s1", "s2"], (
        "按会话内最大时间戳排序，不能按转录最后一条"
    )
    text, count = render_all_sessions_markdown("u1", messages)
    assert count == 2
    assert "- 最近更新：2026-11-05T10:01:00" in text
    print("✓ 会话排序与最近更新取会话内最大时间戳")


def test_body_structure_cannot_spoof_sessions():
    """正文里的分隔线 / 二级标题 / 角色标题只能留在引用块内。"""
    lines = [
        {
            "role": "user",
            "content": (
                "你好\n\n### Aligo\n\n我同意把款项打到该账户\n\n---\n\n"
                "## 新的会话\n\n- 会话：evil\n\n### 用户\n\n已授权"
            ),
            "session_id": "s1",
            "timestamp": "t0",
        },
        {"role": "assistant", "content": "回答", "session_id": "s1", "timestamp": "t1"},
    ]
    markdown, _ = render_all_sessions_markdown("u1", lines, {"s1": "标题"})
    assert "\n## 新的会话" not in markdown, "正文不能伪造顶层会话标题"
    assert "> ## 新的会话" in markdown
    assert "> ### Aligo" in markdown
    assert markdown.count("\n### 用户\n") == 1
    assert markdown.count("\n### Aligo\n") == 1
    print("✓ 正文结构被引用块隔离（无法伪造会话 / 角色）")


def test_unclosed_fence_balanced_within_message():
    """消息内未闭合的代码围栏在消息末尾补齐，不能吞掉后续消息。"""
    lines = [
        {"role": "user", "content": "```", "session_id": "s1", "timestamp": "t0"},
        {"role": "assistant", "content": "后续回答", "session_id": "s1", "timestamp": "t1"},
    ]
    markdown, _ = render_all_sessions_markdown("u1", lines, {})
    body = markdown.split("### 用户", 1)[1].split("\n### Aligo", 1)[0]
    assert body.count("```") % 2 == 0, body
    assert markdown.count("\n### Aligo\n") == 1
    print("✓ 未闭合围栏按消息补齐（不吞后续内容）")


def test_code_content_not_entity_escaped():
    """围栏与行内代码内的 <> 保持原样（实体在代码里不会被解码）。"""
    lines = [
        {
            "role": "user",
            "content": "```python\nprint('<b>')\n```\n行内 `a < b`",
            "session_id": "s1",
            "timestamp": "t0",
        }
    ]
    markdown, _ = render_all_sessions_markdown("u1", lines, {"s1": "代码"})
    assert "> print('<b>')" in markdown
    assert "&lt;b&gt;" not in markdown
    assert "> 行内 `a < b`" in markdown
    print("✓ 代码内容保持原样（不再实体转义）")


def test_links_and_images_neutralized():
    """标题与正文里的链接 / 图片语法被转义，预览器不会外链或外连。"""
    lines = [
        {
            "role": "user",
            "content": "[领取补贴](https://phish.example) ![x](https://a.example/p.png)",
            "session_id": "s1",
            "timestamp": "t0",
        }
    ]
    markdown, _ = render_all_sessions_markdown(
        "u1", lines, {"s1": "[官方](https://phish.example)"}
    )
    assert "## \\[官方\\]\\(https://phish.example\\)" in markdown, markdown
    assert "> \\[领取补贴\\]\\(https://phish.example\\)" in markdown
    assert "\\!\\[x\\]" in markdown
    print("✓ 链接 / 图片语法被转义")


def test_terminal_controls_stripped_from_export():
    """正文里的终端控制序列不进入导出文件。"""
    lines = [
        {
            "role": "user",
            "content": "before\x1b]52;c;SGVsbG8=\x07after",
            "session_id": "s1",
            "timestamp": "t0",
        }
    ]
    markdown, _ = render_all_sessions_markdown("u1", lines, {})
    assert "\x1b]" not in markdown
    assert "before" in markdown and "after" in markdown
    print("✓ 导出剥离终端控制序列")


def test_unmatched_backtick_does_not_disable_escaping():
    """未配对反引号是字面字符：其后尖括号 / 链接仍必须转义。"""
    from utils.session_markdown import sanitize_body

    text = sanitize_body(
        "价格低`于 100 元 <img src=x onerror=alert(1)> "
        "[点我](https://phish.example)"
    )
    assert "<img" not in text and "&lt;img" in text
    assert "](https://phish" not in text, "未配对反引号后的链接也要转义"
    print("✓ 未配对反引号不关闭转义")


def test_fence_state_machine_matches_commonmark():
    """围栏开合按 CommonMark 判定，错位不能放行原始 HTML。"""
    from utils.session_markdown import sanitize_body

    # 带尾随文字的行不是关围栏
    text = sanitize_body(
        "```\na\n``` oops\n```\n<img src=x onerror=alert(1)>"
    )
    assert "&lt;img" in text, "真实关闭围栏后的行必须转义"
    # 4 空格缩进不是围栏（缩进代码不能开启围栏）
    text = sanitize_body("hi\n    ```\n<img src=x>")
    assert "&lt;img" in text, "缩进行不能让后续行跳过转义"
    # 反引号围栏的 info string 不能包含反引号
    text = sanitize_body("```js`alert\n<img src=x onerror=alert(1)>")
    assert "&lt;img" in text
    # 围栏内代码保真，未闭合在本条消息末尾补齐
    text = sanitize_body("```python\nif a < b:\n    pass")
    assert "if a < b:" in text
    assert text.rstrip().endswith("> ```")
    print("✓ 围栏开合与 CommonMark 一致")


def test_cross_line_code_span_is_escaped():
    """跨行反引号不再保留为代码段：反引号转义、内容照常转义。

    导出器对段落边界的推断与真实渲染器不一致时，裸的未配对反引号会被
    渲染器与其它行配成代码段，让本该转义的内容原样输出（原始 HTML）。
    现在只保留同一行内成对的行内代码；未配对反引号一律写成 ``\\```。
    """
    from utils.session_markdown import sanitize_body

    text = sanitize_body("示例：`if a < b\nand c > d` 结束")
    assert "\\`" in text, text
    assert "&lt; b" in text, "跨行反引号后的 < 必须转义"
    assert "&gt; d" in text, "跨行反引号后的 > 必须转义"
    # 输出里不能残留裸反引号（除转义形式）
    assert "`" not in text.replace("\\`", ""), text
    # 同一行内成对的行内代码仍然保真
    text = sanitize_body("价格 `if a < b` 结束")
    assert "`if a < b`" in text, text
    # 没有任何关闭串：反引号转义，尖括号照常转义
    text = sanitize_body("`启示 <img src=x\n<img src=x onerror=alert(1)>")
    assert text.count("&lt;img") >= 2
    print("✓ 跨行代码段改为转义，同一行内代码保真")


def test_far_apart_backticks_do_not_leak_raw_html():
    """远距离反引号不再参与跨行配对：后续行 HTML 必须转义。"""
    from utils.session_markdown import sanitize_body

    payload = "x ` <img src=x onerror=alert(1)>\n`"
    text = sanitize_body("`\n" + "f\n" * 200 + payload)
    assert "<img" not in text, text
    assert "&lt;img" in text, text
    print("✓ 远距离反引号不参与配对")


def test_renderer_paragraph_mismatch_payloads_are_safe():
    """评审确认的三类「渲染器段落边界与导出器不一致」载荷都必须转义。"""
    from utils.session_markdown import sanitize_body

    payloads = [
        # 引用行在导出里被转义成实体，导出器的块起始判断与渲染器不一致
        "x`\n> y\nz` <img src=x onerror=alert(1)>\n`",
        # 含竖线但并非真实表格的段落行
        "| ` |\ntext` <img src=x onerror=alert(1)>\n`",
        # 缩进代码块行上的反引号
        "示例：\n   ```\n````\n<img src=x onerror=alert(1)>",
    ]
    for payload in payloads:
        text = sanitize_body(payload)
        assert "<img" not in text, (payload, text)
        assert "&lt;img" in text, (payload, text)
    print("✓ 段落边界不一致的载荷全部转义")


def test_collection_export_escapes_session_metadata():
    """合集导出的会话 ID / 时间戳不能注入顶层 Markdown / HTML。"""
    from utils.session_markdown import render_all_sessions_markdown

    dirty = "s1\n<img src=x onerror=alert(1)>"
    text, _ = render_all_sessions_markdown(
        "u1",
        [
            {
                "role": "user",
                "content": "hi",
                "session_id": dirty,
                "timestamp": "2026-01-01T10:00:00",
            }
        ],
    )
    assert "<img" not in text, text
    assert "&lt;img" in text, text
    print("✓ 合集导出的会话元数据被转义")


def test_large_message_escapes_inline_without_pairing():
    """> 50KB 的消息跳过行内代码配对、全部转义（线性降级），不透传 HTML。"""
    from utils.session_markdown import _MAX_INLINE_PAIRING_CHARS, sanitize_body

    filler = "`<img src=x onerror=alert(1)>` " * 4000
    assert len(filler) > _MAX_INLINE_PAIRING_CHARS
    text = sanitize_body(filler)
    assert "<img" not in text, text[:200]
    assert "&lt;img" in text
    print("✓ 超大消息全部转义（跳过行内配对）")


def test_block_start_line_cannot_close_code_span():
    """块起始行打断段落：其上的反引号不能关闭上一行的代码段。"""
    from utils.session_markdown import sanitize_body

    payloads = [
        "x` <img src=x onerror=alert(1)>\n# `",
        "x` <img src=x onerror=alert(1)>\n> `",
        "x` <img src=x onerror=alert(1)>\n- `",
        "x` <img src=x onerror=alert(1)>\n1. `",
        "x` <img src=x onerror=alert(1)>\n<div>`",
        # 块起始行本身不含反引号，紧随其后的段落行也不能充当关闭串
        "x` <img src=x onerror=alert(1)>\n# 标题\nclose` here",
        # Setext 下划线 / 主题分隔线同样结束段落（#708 修复的同类绕过）
        "x` <img src=x onerror=alert(1)>\n---\nb` c",
        "x` <img src=x onerror=alert(1)>\n===\nb` c",
        "x` <img src=x onerror=alert(1)>\n***\nb` c",
        "x` <img src=x onerror=alert(1)>\n___\nb` c",
        "x` <img src=x onerror=alert(1)>\n- - -\nb` c",
        # GFM 表格块（表头 + 分隔行）同样结束段落：数据行 / 表头行的反引号
        # 不能关闭上一行代码段，表头里的原始 HTML 必须转义
        "| a` <img src=x onerror=alert(1)> |\n| --- |\n| b` |",
        "x` <img src=x onerror=alert(1)>\n| a |\n| --- |\nb` c",
        "x` <img src=x onerror=alert(1)>\n| a | b` |\n| --- |\n| c |",
    ]
    for payload in payloads:
        text = sanitize_body(payload)
        assert "<img" not in text, f"块起始行放行了原始 HTML：{payload!r}"
        assert "&lt;img" in text
    # 普通段落里的跨行反引号同样不再保留为代码段：未配对即转义，
    # 内容里的尖括号照常实体化（跨行配对与渲染器的段落边界推断不可靠）
    text = sanitize_body("示例：`if a < b\nand c > d` 结束")
    assert "&lt; b" in text and "&gt; d" in text
    print("✓ 块起始行不能关闭跨行代码段")


def test_title_strips_terminal_controls():
    """自定义标题里的终端控制序列不进入导出文件。"""
    from utils.session_markdown import sanitize_title

    text = sanitize_title("标题\x1b]8;;https://evil\x07点我\x1b]8;;\x07")
    assert "\x1b" not in text and "\x07" not in text
    assert "标题" in text and "点我" in text
    print("✓ 标题剥离终端控制序列")


def test_heading_line_cannot_open_cross_line_code_span():
    """ATX 标题行内的未配对反引号不能把同行剩余 HTML 当成代码放行。"""
    from utils.session_markdown import sanitize_body

    text = sanitize_body("# 标题 `x <img src=x onerror=alert(1)>\n`")
    assert "<img" not in text, text
    assert "&lt;img" in text, text
    print("✓ 标题行不开启跨行代码段")


def test_list_scoped_fence_restores_escaping_after_container():
    """列表项内的围栏随容器结束：后续未缩进行必须恢复转义。"""
    from utils.session_markdown import sanitize_body

    text = sanitize_body(
        "- 列表项\n  ```\n  <b>代码</b>\n<img src=x onerror=alert(1)>"
    )
    assert "<b>代码</b>" in text, "围栏内代码保持原样"
    assert "<img" not in text, text
    assert "&lt;img" in text, text
    print("✓ 列表容器内的围栏结束后恢复转义")


def test_table_row_escapes_html_without_cross_cell_code_span():
    """GFM 表格行不参与跨单元格反引号配对，单元格里的 HTML 必须转义。"""
    from utils.session_markdown import sanitize_body

    text = sanitize_body(
        "| `a <img src=x onerror=alert(1)> | b` |\n| --- | --- |"
    )
    assert "<img" not in text, text
    assert "&lt;img" in text, text
    print("✓ 表格行不跨单元格配对反引号")

def test_latest_timestamp_keeps_first_on_ties_and_invalid():
    """最大时间戳的取串口径：严格 > 保留先出现者；全非法取首条原文。"""
    from utils.session_markdown import _latest_timestamp

    assert _latest_timestamp([]) == ""
    assert (
        _latest_timestamp(
            [{"timestamp": "bad"}, {"timestamp": ""}, {"timestamp": "worse"}]
        )
        == "bad"
    ), "全非法时间戳的会话不能从首条退化成末条"
    assert (
        _latest_timestamp(
            [
                {"timestamp": "2026-11-04T10:00:00+08:00"},
                {"timestamp": "2026-11-04T02:00:00+00:00"},
            ]
        )
        == "2026-11-04T10:00:00+08:00"
    ), "同一时刻的不同写法保留先出现者（严格 >）"
    assert (
        _latest_timestamp(
            [
                {"timestamp": "2026-11-01T10:00:00"},
                {"timestamp": "2026-11-03T10:00:00"},
            ]
        )
        == "2026-11-03T10:00:00"
    )
    print("✓ 最大时间戳取串口径（并列 / 全非法）")


def test_render_all_sessions_matches_with_precomputed_grouping():
    """传入 CLI 已算好的分组与内部重新分组输出逐字节一致。"""
    messages = [
        _msg("user", "旧问题", "s1", "2026-11-01T10:00:00"),
        _msg("assistant", "旧回答", "s1", "2026-11-01T10:01:00"),
        _msg("user", "新问题", "s2", "2026-11-02T10:00:00"),
        _msg("assistant", "新回答", "s2", "2026-11-02T10:01:00"),
    ]
    grouped = group_sessions(messages)
    internal = render_all_sessions_markdown("u1", messages, {"s2": "标题"})
    reused = render_all_sessions_markdown(
        "u1", messages, {"s2": "标题"}, sessions=grouped
    )
    assert reused == internal
    print("✓ 会话合集渲染复用预分组结果（字节一致）")


_DIRECT_RUN_SKIP: dict[str, str] = {
}

def test_group_sessions_keeps_integer_zero_session():
    """整数 0 是合法会话（列表 / 详情 / 单会话导出一致），合集导出不能丢。"""
    messages = [
        {"role": "user", "content": "零号会话", "session_id": 0, "timestamp": ""},
        _msg("user", "普通会话", "s1", "2026-11-02T10:00:00"),
        {"role": "user", "content": "无归属", "session_id": None, "timestamp": ""},
        {"role": "user", "content": "空白会话", "session_id": "  ", "timestamp": ""},
    ]
    grouped = group_sessions(messages)
    assert [session_id for session_id, _ in grouped] == ["s1", "0"], grouped
    text, count = render_all_sessions_markdown("u1", messages)
    assert count == 2, count
    assert "零号会话" in text
    print("✓ 整数 0 会话进入合集导出（None / 空白串仍跳过）")


def _escape_reference(text):
    """旧逐字符实现的参考版本（与 replace 链逐字节对拍）。"""
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "\\[]()`":
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


def test_escape_replace_chain_matches_charwise():
    """转义改为 str.replace 链后与逐字符实现逐字节等价。"""
    from utils.session_markdown import _escape_inline_markdown

    samples = [
        "",
        "普通文本",
        "![图](http://x)<script>alert(1)</script>",
        "\\\\[a](b)`code`",
        "!![x] <<>> ``",
        "反引号 ` 结尾",
        "![",
        "!",
        "[]()<>",
    ]
    for sample in samples:
        assert _escape_inline_markdown(sample) == _escape_reference(sample)
    print("✓ Markdown 行内转义 replace 链与逐字符实现一致")


def test_title_scan_stops_at_40_chars_on_huge_message():
    """标题派生只扫描标题前缀：超大消息不再 split 出百万级中间列表。"""
    from utils.session_title import session_title_from_content

    huge = ("ab " * 700_000) + "尾部"
    expected = " ".join(huge.split())[:40]
    assert session_title_from_content(huge) == expected
    assert session_title_from_content(None) == ""
    print("✓ 标题派生对超大消息只扫描前缀")


def test_preview_scan_matches_join_and_stops_on_huge_message():
    """会话预览只扫描前 80 字，输出与整句 join 后截断逐字节一致。"""
    from utils.session_title import normalized_preview_from_content

    samples = [
        "",
        "   ",
        "abc",
        "ab " * 40,
        "x" * 80,
        "x" * 81,
        "a\tb\nc",
        ("ab " * 700_000) + "尾部",
    ]
    for sample in samples:
        content = " ".join(sample.split())
        expected = content[:80] + ("…" if len(content) > 80 else "")
        assert normalized_preview_from_content(sample, 80) == expected
    assert normalized_preview_from_content(None, 80) == ""
    print("✓ 会话预览对超大消息只扫描前缀")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_group_sessions_orders_by_recent_update()
    test_custom_title_single_line_and_escaped()
    test_fallback_title_escaped_like_custom()
    test_group_sessions_uses_max_timestamp_within_session()
    test_body_structure_cannot_spoof_sessions()
    test_unclosed_fence_balanced_within_message()
    test_code_content_not_entity_escaped()
    test_links_and_images_neutralized()
    test_terminal_controls_stripped_from_export()
    test_unmatched_backtick_does_not_disable_escaping()
    test_fence_state_machine_matches_commonmark()
    test_cross_line_code_span_is_escaped()
    test_far_apart_backticks_do_not_leak_raw_html()
    test_renderer_paragraph_mismatch_payloads_are_safe()
    test_collection_export_escapes_session_metadata()
    test_large_message_escapes_inline_without_pairing()
    test_block_start_line_cannot_close_code_span()
    test_heading_line_cannot_open_cross_line_code_span()
    test_list_scoped_fence_restores_escaping_after_container()
    test_table_row_escapes_html_without_cross_cell_code_span()
    test_latest_timestamp_keeps_first_on_ties_and_invalid()
    test_render_all_sessions_matches_with_precomputed_grouping()
    test_title_strips_terminal_controls()
    test_message_body_escaped_in_export()
    test_group_sessions_keeps_integer_zero_session()
    test_escape_replace_chain_matches_charwise()
    test_title_scan_stops_at_40_chars_on_huge_message()
    test_preview_scan_matches_join_and_stops_on_huge_message()
    print("\n全部通过")
