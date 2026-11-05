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


if __name__ == "__main__":
    test_group_sessions_orders_by_recent_update()
    print("\n全部通过")
