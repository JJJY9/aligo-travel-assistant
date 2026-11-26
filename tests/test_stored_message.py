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


def test_object_results_rendered_not_placeholder():
    """results 为对象的历史消息（旧编排 / 补充导入）显示答案而非占位文案。"""
    item = {
        "agent_name": "information_query",
        "status": "success",
        "data": {"answer": "北京明天晴"},
    }
    content = json.dumps(
        {"status": "completed", "results": item}, ensure_ascii=False
    )
    rendered = render_stored_message({"role": "assistant", "content": content})
    assert "北京明天晴" in rendered, rendered
    assert rendered != "已处理您的请求。", rendered
    print("✓ results 为对象的历史消息显示答案")


def test_render_escapes_lone_surrogates():
    """旧 JSON 文本里的 \\ud800 在渲染出口转义，会话详情 / 导出不再 500。"""
    content = (
        '{"results":[{"agent_name":"A","status":"success",'
        '"data":{"answer":"坏\\ud800答"}}],"status":"completed"}'
    )
    rendered = render_stored_message({"role": "assistant", "content": content})
    rendered.encode("utf-8")  # 未转义会抛 UnicodeEncodeError
    assert chr(0xD800) not in rendered
    assert "\\ud800" in rendered, rendered
    user = render_stored_message({"role": "user", "content": "坏" + chr(0xD800)})
    user.encode("utf-8")
    print("✓ 渲染出口转义孤立代理项（JSON / 用户原文）")


def test_render_cache_reuses_result(monkeypatch):
    """同一 (role, content) 的渲染结果命中缓存；会话详情只解析一次 JSON。"""
    import utils.stored_message as stored_module

    content = json.dumps(
        {
            "status": "completed",
            "results": [
                {"agent_name": "A", "status": "success", "data": {"answer": "缓存"}}
            ],
            "_meta": {"intents": ["查询"]},
        },
        ensure_ascii=False,
    )
    message = {"role": "assistant", "content": content}
    rendered, meta = stored_module.render_stored_message_with_meta(message)
    assert "缓存" in rendered
    assert meta == {"intents": ["查询"]}

    calls = []

    def boom(_data):
        calls.append(1)
        raise AssertionError("缓存命中时不应重新渲染")

    monkeypatch.setattr(stored_module, "result_to_markdown", boom)
    assert stored_module.render_stored_message(message) == rendered
    assert stored_module.render_stored_message_with_meta(message) == (
        rendered,
        {"intents": ["查询"]},
    )
    assert calls == []
    print("✓ 渲染结果按内容缓存，会话详情复用缓存与元数据")


def test_render_falls_back_without_caching_failure(monkeypatch):
    """渲染器临时异常时的原文回退不入缓存：恢复后同一内容仍能正常渲染。"""
    import utils.stored_message as stored_module

    content = json.dumps(
        {
            "status": "completed",
            "results": [
                {"agent_name": "A", "status": "success", "data": {"answer": "恢复"}}
            ],
        },
        ensure_ascii=False,
    )
    message = {"role": "assistant", "content": content}
    real_render = stored_module.result_to_markdown
    monkeypatch.setattr(
        stored_module,
        "result_to_markdown",
        lambda _data: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert stored_module.render_stored_message(message) == content
    monkeypatch.setattr(stored_module, "result_to_markdown", real_render)
    assert "恢复" in stored_module.render_stored_message(message)
    print("✓ 渲染异常回退不缓存，恢复后正常渲染")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_user_message_is_raw()
    test_assistant_plain_text_is_raw()
    test_assistant_orchestration_json_rendered()
    test_assistant_bad_json_falls_back()
    test_object_results_rendered_not_placeholder()
    test_render_escapes_lone_surrogates()
    _mp = pytest.MonkeyPatch()
    try:
        test_render_cache_reuses_result(_mp)
    finally:
        _mp.undo()
    _mp_fallback = pytest.MonkeyPatch()
    try:
        test_render_falls_back_without_caching_failure(_mp_fallback)
    finally:
        _mp_fallback.undo()
    _mp_render = pytest.MonkeyPatch()
    try:
        test_render_failure_falls_back_to_raw_content(_mp_render)
    finally:
        _mp_render.undo()
    print("\n全部通过")
