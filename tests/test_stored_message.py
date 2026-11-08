"""
长期记忆消息渲染测试（Web 导出与 CLI 导出共用）

运行：python tests/test_stored_message.py
"""
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.stored_message import render_stored_message


def test_user_message_is_raw():
    assert render_stored_message({"role": "user", "content": "原始提问"}) == "原始提问"
    print("✓ 用户消息原样返回")


def test_assistant_plain_text_is_raw():
    assert (
        render_stored_message({"role": "assistant", "content": "普通回答"})
        == "普通回答"
    )
    print("✓ 非 JSON 助手消息原样返回")


def test_assistant_orchestration_json_rendered():
    content = json.dumps({"status": "no_agents", "results": []}, ensure_ascii=False)
    rendered = render_stored_message({"role": "assistant", "content": content})
    assert "已记录" in rendered, "编排结果 JSON 应渲染为 Markdown"
    print("✓ 编排 JSON 重新渲染为 Markdown")


def test_assistant_bad_json_falls_back():
    content = "{not-json"
    assert render_stored_message({"role": "assistant", "content": content}) == content
    # 合法 JSON 但不是编排结果（无 results / 状态不在白名单）：原样返回
    other = json.dumps({"reply": "x"}, ensure_ascii=False)
    assert render_stored_message({"role": "assistant", "content": other}) == other
    print("✓ 非法 / 非编排 JSON 安全回退原文")


def test_render_failure_falls_back_to_raw_content(monkeypatch):
    """result_to_markdown 抛异常时退回原始内容（会话查看不能因此 500）。"""
    import utils.stored_message as stored_module

    def boom(_data):
        raise RuntimeError("formatter crash")

    monkeypatch.setattr(stored_module, "result_to_markdown", boom)
    content = json.dumps({"results": [{"agent_name": "A"}]}, ensure_ascii=False)
    assert render_stored_message({"role": "assistant", "content": content}) == content
    print("✓ 渲染异常安全回退原文")


if __name__ == "__main__":
    test_user_message_is_raw()
    test_assistant_plain_text_is_raw()
    test_assistant_orchestration_json_rendered()
    test_assistant_bad_json_falls_back()
    _mp = pytest.MonkeyPatch()
    try:
        test_render_failure_falls_back_to_raw_content(_mp)
    finally:
        _mp.undo()
    print("\n全部通过")
