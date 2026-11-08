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


if __name__ == "__main__":
    test_session_request_race_guards()
    test_knowledge_preview_locates_hits_in_original_text()
    print("\n全部通过")
