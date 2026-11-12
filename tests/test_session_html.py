"""会话 HTML 渲染单元测试（无需 API Key、不访问网络）。

运行：python tests/test_session_html.py
"""
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.session_html import (
    message_text,
    render_all_sessions_html,
    render_session_html,
)


def _msg(role, content, session_id="s1", timestamp="2026-11-01T10:00:00"):
    return {
        "role": role,
        "content": content,
        "session_id": session_id,
        "timestamp": timestamp,
    }


def test_single_session_escapes_every_dynamic_field():
    text = render_session_html(
        'u"><script>alert(1)</script>',
        "<s1>",
        "预算<5000 对比 A>B",
        [
            _msg("user", "<img src=x onerror=alert(1)>"),
            _msg("assistant", json.dumps({"reply": "回答 <b>加粗</b>"}, ensure_ascii=False)),
        ],
    )
    assert text.startswith("<!doctype html>")
    assert "<title>预算&lt;5000 对比 A&gt;B</title>" in text
    assert "&amp;lt;" not in text, "标题只应转义一次"
    assert "<script>alert(1)</script>" not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "回答 &lt;b&gt;加粗&lt;/b&gt;" in text
    assert 'class="msg user"' in text and 'class="msg assistant"' in text
    print("✓ 单会话 HTML 全字段转义且不双重转义")


def test_assistant_message_text_variants():
    assert message_text(_msg("user", "原文")) == "原文"
    stored = json.dumps(
        {
            "results": [
                {
                    "agent_name": "rag_knowledge",
                    "status": "success",
                    "data": {"answer": "正文回答"},
                }
            ]
        },
        ensure_ascii=False,
    )
    assert "正文回答" in message_text(_msg("assistant", stored)), "编排 JSON 应渲染正文"
    # 非编排 JSON / 非法 JSON：原样返回可读文本而不是崩溃
    assert message_text(_msg("assistant", '{"reply": "旧格式"}')) == '{"reply": "旧格式"}'
    fallback = message_text(_msg("assistant", "不是 JSON 的回答"))
    assert fallback == "不是 JSON 的回答"
    print("✓ 助手消息复用 render_stored_message")


def test_all_sessions_bundle_grouping_and_escaping():
    messages = [
        _msg("user", "旧会话 <script>", "old", "2026-11-01T09:00:00"),
        _msg("assistant", "旧回答", "old", "2026-11-01T09:01:00"),
        _msg("user", "新会话提问", "new", "2026-11-02T09:00:00"),
    ]
    text, count = render_all_sessions_html(
        "u1", messages, {"new": "自定义<标题>"}
    )
    assert count == 2
    assert "全部会话（2 个）" in text
    assert text.index("自定义&lt;标题&gt;") < text.index("旧会话 &lt;script&gt;"), (
        "最近更新的会话排在前面"
    )
    assert "自定义&lt;标题&gt;" in text and "&amp;lt;" not in text
    assert "<h2>旧会话 &lt;script&gt;</h2>" in text, "回退标题同样只转义一次"
    assert "会话 old · 2 条消息" in text
    # 合集样式作为 format 的值插入：双花括号会原样进入 <style>，浏览器
    # 丢弃整段合集样式（标题间距 / 边距失效）
    assert ".session {" in text and "{{" not in text, "合集样式不能带双花括号"
    print("✓ 合集 HTML：分组排序 / 自定义与回退标题单次转义")


def test_empty_bundle_renders_placeholder():
    text, count = render_all_sessions_html("u1", [], {})
    assert count == 0
    assert "（暂无会话）" in text
    assert "全部会话（0 个）" in text
    print("✓ 空合集渲染占位")


if __name__ == "__main__":
    test_single_session_escapes_every_dynamic_field()
    test_assistant_message_text_variants()
    test_all_sessions_bundle_grouping_and_escaping()
    test_empty_bundle_renders_placeholder()
    print("\n全部通过")
