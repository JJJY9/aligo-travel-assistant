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
def test_knowledge_duplicates_panel_states():
    """「查重复」按钮：无重复 / 有重复（截断提示）/ 失败三种状态文案。"""
    results = _run_harness("duplicates")
    assert results["empty"] == "没有重复内容的独立知识片段"
    assert results["groups"] == (
        "重复检查：3 组（共 7 条）：#3、#7（完全相同的内容）；#9"
        " · 其余 1 组未显示 · 确认后可在列表删除多余片段"
    ), results["groups"]
    assert results["error"] == "重复检查失败：网络抖动"
    assert results["button"] == "查重复", "结束后按钮文案必须复位"
    print("✓ 知识库重复检查面板状态")


def test_knowledge_dedupe_panel_states():
    """「清理重复」按钮：无重复 / 取消 / 执行（部分失败）/ 接口失败四种状态。"""
    results = _run_harness("dedupe")
    empty = results["empty"]
    assert empty["text"] == "没有重复片段可清理"
    assert empty["requests"] == 1, "无可清理项时只发 dry-run，不弹确认"
    assert empty["confirm_calls"] == 0

    cancel = results["cancel"]
    assert cancel["text"] == "没有重复片段可清理", (
        "取消确认不应覆盖之前的结果（此前是重复列表 / 空态）"
    )
    assert cancel["confirm_calls"] == 1
    assert cancel["requests"] == 2, "取消后不能再发删除请求"

    applied = results["applied"]
    assert applied["text"] == (
        "已清理 1 条重复片段 · 1 条已跳过（期间已删除或内容变化）"
        " · 1 条删除失败：#11（库内已有变化：计划 2 → 3）"
    )
    assert applied["warn"] is True, "有失败条目必须标黄"
    assert applied["hidden"] is False, "清理结果必须真实可见（结果条此前可能是 hidden）"
    assert applied["requests"] == 4, "dry-run + 删除各一次"
    assert applied["button"] == "清理重复"

    error = results["error"]
    assert error["text"] == "清理重复失败：boom"
    assert error["button"] == "清理重复"
    assert error["disabled"] is False, "失败后按钮必须恢复可点"
    print("✓ 知识库重复清理面板状态")


def test_knowledge_actions_arbitrate_shared_result_bar():
    """评测 / 查重复共用结果条：迟到的旧响应不能覆盖新结果。"""
    results = _run_harness("arbitrate")
    assert results["after_duplicates"].startswith("重复检查：1 组"), results
    assert results["final_text"] == results["after_duplicates"], (
        "先发起、后返回的评测不能覆盖后发起的查重复结果"
    )
    assert results["eval_button"] == "检索评测"
    assert results["duplicates_button"] == "查重复"
    print("✓ 知识库工具栏动作结果条序号仲裁")


def test_trip_import_file_decoding_and_size_guard():
    """行程导入文件：UTF-8 / GBK 解码、超大文件不读取、导入中防重复提交。"""
    results = _run_harness("decode")
    assert results["utf8"] == "目的地,杭州"
    assert results["gbk"] == "目的地,上海", "GBK CSV 必须回退 gb18030 解码"
    assert results["utf16_bom"] == "目的地,上海", "UTF-16LE（BOM）必须按 UTF-16 解码"
    assert results["utf16_nobom"] == "origin,destination\n北京,上海\n", (
        "无 BOM 的 UTF-16 按 NUL 字节规律识别"
    )
    assert results["oversize_toasts"] == [
        "文件过大（上限 2 MB），请拆分后再导入"
    ]
    assert results["oversize_requests"] == 0, "超大文件不应读取或发请求"
    assert results["oversize_button"] == {"text": "导入行程", "disabled": False}
    assert results["pending_error"] is None
    assert results["busy"] == {"text": "导入中…", "disabled": True}
    assert results["busy_requests"] == 1, "导入进行中必须忽略重复选择"
    assert results["after_import"]["text"] == "导入行程"
    assert results["after_import"]["disabled"] is False
    assert results["after_import"]["toasts"][-1] == "已导入 1 条行程，跳过 60 条", (
        "结果 toast 的跳过数量同样要用 skipped_total"
    )
    assert results["after_import"]["confirm"] == (
        "将新增 1 条、更新 0 条行程，跳过 73 条。继续？"
    ), "确认框的跳过数量必须用 skipped_total（列表最多 50 条）"
    urls = results["after_import"]["urls"]
    assert len(urls) == 2 and urls[0].endswith("dry_run=1"), urls
    assert "dry_run" not in urls[1], "正式写入不能带 dry_run"
    assert results["after_import"]["refresh_options"] == {"silentFailure": True}
    assert results["after_import"]["refresh_calls"] == 1, "成功后刷新一次概览"

    closed = results["closed"]
    assert closed["confirm_calls"] == 0, "面板关闭后不能再弹确认"
    assert closed["calls"] == 1 and closed["urls"][0].endswith("dry_run=1"), closed

    assert results["pinned_url"].endswith("user_id=user-a"), (
        "解码 / 请求期间切换用户，导入仍落在选择文件时的用户"
    )
    pinned_urls = results["pinned_urls"]
    assert len(pinned_urls) == 2
    assert pinned_urls[0].endswith("dry_run=1") and "user_id=user-a" in pinned_urls[0]
    assert "dry_run" not in pinned_urls[1] and "user_id=user-a" in pinned_urls[1]
    assert results["user_after"] == "user-b"
    assert results["cancel"]["calls"] == 1
    assert results["cancel"]["confirm_calls"] == 1
    assert results["cancel"]["last_toast"] == "已取消导入"
    assert results["cancel"]["urls"][0].endswith("dry_run=1"), (
        "取消场景只发过一次 dry-run 预览，不能已经写入"
    )
    assert results["char_limit"]["last_toast"] == (
        "文件过大（超过 50 万字符），请拆分后再导入"
    ), "字符数上限必须与服务端 50 万字符一致"
    assert results["char_limit"]["calls_delta"] == 0
    assert results["char_limit"]["button"] == {
        "text": "导入行程",
        "disabled": False,
    }
    print("✓ 行程导入文件解码 / 大小限制 / 用户固定 / 防重复提交")


def test_knowledge_eval_panel_states():
    """「检索评测」按钮：达标 / 低基线+告警 / 失败三种状态与标黄逻辑。"""
    results = _run_harness("eval")
    assert results["ok_text"] == (
        "检索评测：14 个问题 · hit@1 64% · hit@3 93% · hit@5 100% · MRR 0.80"
        " · 未命中 1 个：「住宿标准」"
    ), results["ok_text"]
    assert results["ok_warn"] is False, "达标时不应标黄"
    assert results["ok_button"] == "检索评测", "结束后按钮文案必须复位"

    assert "hit@5 60%" in results["low_text"], results["low_text"]
    assert "低于 80% 基线" in results["low_text"]
    assert "共享锁不可用" in results["low_text"], "告警文案必须展示"
    assert results["low_warn"] is True, "低于基线 / 有告警时必须标黄"

    assert results["error"] == "评测失败：服务暂不可用"
    assert results["error_button"] == "检索评测"
    assert results["error_busy"] is None, "失败后 aria-busy 必须移除"
    print("✓ 检索评测面板状态")


if __name__ == "__main__":
    test_session_request_race_guards()
    test_knowledge_preview_locates_hits_in_original_text()
    test_frontend_render_helpers()
    test_knowledge_duplicates_panel_states()
    test_knowledge_dedupe_panel_states()
    test_knowledge_actions_arbitrate_shared_result_bar()
    test_trip_import_file_decoding_and_size_guard()
    test_knowledge_eval_panel_states()
    print("\n全部通过")
