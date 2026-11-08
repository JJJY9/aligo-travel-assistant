"""app.js 行为测试（Node 驱动，无需浏览器 / 网络）。

用 node:vm 加载 webapp/static/app.js（去掉末尾 init()，不启动页面），
直接调用 loadMoreSessions / openSession / knowledgePreview 验证：

- 已作废的会话分页 / 加载请求失败不弹提示、不清空当前会话；
- 未作废的失败仍按原语义提示 / 回退欢迎页；
- knowledgePreview 对 İ / emoji / 正则元字符的窗口定位。

node 缺失时跳过（CI ubuntu runner 自带 node）。
运行：python tests/test_app_js_behaviors.py
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

APP_JS = os.path.join(project_root, "webapp", "static", "app.js")
HARNESS = os.path.join(project_root, "tests", "app_js_harness.js")


def _run_harness(scenario: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过前端行为测试")
    completed = subprocess.run(
        [node, HARNESS, APP_JS, scenario],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=project_root,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_session_request_race_guards():
    """作废的会话请求失败不打扰当前视图；未作废的失败保留原语义。"""
    results = _run_harness("race")
    assert results["stale_load_more_toasts"] == [], "作废的分页请求失败不能弹提示"
    stale_open = results["stale_open_session"]
    assert stale_open["sessionId"] == "B", "迟到的失败不能把当前会话清掉"
    assert stale_open["showWelcome"] == 0
    assert stale_open["toasts"] == []
    assert results["live_load_more_toasts"] == ["boom"], "未作废的分页失败仍要提示"
    live_open = results["live_silent_open"]
    assert live_open["sessionId"] is None, "静默恢复失败要丢弃旧会话 ID"
    assert live_open["showWelcome"] == 1
    assert live_open["toasts"] == []
    print("✓ 会话请求竞态守卫（作废静默 / 未作废照常提示）")


def test_knowledge_preview_locates_hits_in_original_text():
    """İ / emoji / 正则元字符查询都能把预览窗口定位到关键词。"""
    results = _run_harness("preview")
    assert results["dotted"]["contains"], results["dotted"]["text"]
    assert results["emoji"]["contains"], "emoji 前缀不能让 UTF-16 下标错位"
    assert results["emoji"]["lone_surrogate"] is False, "窗口边界不能劈开代理对"
    assert results["meta_error"] is None, results["meta_error"]
    assert results["uppercase"]["contains"], "大小写不敏感匹配仍要生效"
    assert results["plain"]["contains"], results["plain"]
    print("✓ 知识库预览原文定位（İ / emoji / 元字符）")


def test_frontend_render_helpers():
    """转义 / 高亮 / Markdown 渲染 / 截断 / 偏好格式化的回归基线。"""
    results = _run_harness("render")
    assert results["escape_html"] == "&lt;b&gt;&quot;&amp;&#39;�", (
        "私用区占位符必须替换，避免被代码块占位恢复步骤当成注入"
    )
    assert results["highlight_dotted"] == "<mark>İstanbul</mark> 出差", (
        "İ 关键字按原文长度命中并高亮"
    )
    assert results["highlight_plain"] == "no match here", "无命中时返回转义原文"
    assert results["highlight_meta"] == "<mark>a+b</mark> (x)", (
        "正则元字符按字面匹配，不能被当成模式"
    )

    fence = results["fence_info"]
    assert fence.startswith("<pre>"), fence
    assert "&lt;&gt;" in fence and "<p>" not in fence, "代码块内容必须转义"
    assert "```inner" in results["fence_four"], "四反引号围栏内可包含三反引号"

    inline = results["inline_code"]
    assert "<code>**[x]**</code>" in inline, inline
    assert "<strong>" not in inline, "行内代码里的 ** 不能参与强调渲染"

    assert 'href="https://en.wikipedia.org/wiki/Foo_(bar)"' in results["link_paren"], (
        "链接 URL 的一层括号必须保留"
    )

    table = results["table"]
    assert "<table>" in table and "<th>日期</th>" in table and "<td>小雨</td>" in table
    assert results["heading"] == "<h2>标题</h2>", "标题最小层级压到 h2"

    assert results["slice_emoji"] == "😀😀…", "截断按码点，不能劈开代理对"
    assert results["pref_object"] == '{"a":1}', "对象偏好显示 JSON"
    assert results["pref_list"] == '汉庭、{"b":2}', "列表偏好用「、」拼接并保留对象 JSON"
    print("✓ 前端渲染 / 高亮 / 截断 / 偏好格式化基线")


if __name__ == "__main__":
    test_session_request_race_guards()
    test_knowledge_preview_locates_hits_in_original_text()
    test_frontend_render_helpers()
    print("\n全部通过")
