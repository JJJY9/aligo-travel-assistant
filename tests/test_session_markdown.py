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


def test_multiline_code_span_content_preserved():
    """跨行代码段内的字符不被实体化；未闭合的跨行反引号按字面处理。"""
    from utils.session_markdown import sanitize_body

    text = sanitize_body("示例：`if a < b\nand c > d` 结束")
    assert "if a < b" in text
    assert "and c > d" in text, "跨行代码段内的 > 不能被实体化"
    assert "c &gt; d" not in text
    # 没有任何关闭串：反引号只是字面字符，禁止据此关闭转义
    text = sanitize_body("`启示 <img src=x\n<img src=x onerror=alert(1)>")
    assert text.count("&lt;img") >= 2
    print("✓ 跨行代码段保真且未闭合时不放行")


if __name__ == "__main__":
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
    test_multiline_code_span_content_preserved()
    print("\n全部通过")
