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
    assert "## &lt;script&gt;alert(1)&lt;/script&gt; 你好" in markdown
    assert "## <script>" not in markdown, "回退标题必须与自定义标题同样转义"
    print("✓ 回退标题与自定义标题同一清洗口径")


if __name__ == "__main__":
    test_group_sessions_orders_by_recent_update()
    test_custom_title_single_line_and_escaped()
    test_fallback_title_escaped_like_custom()
    print("\n全部通过")
