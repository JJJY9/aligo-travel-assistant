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
    assert (
        'href="https://en.wikipedia.org/wiki/Foo**bar**"' in results["link_bold_url"]
    ), "URL 里的 ** 不能被加粗规则改写进 href"
    assert "<th>a|b</th>" in results["table_escaped_pipe"], "\\| 必须是字面竖线"
    assert "<th><code>a|b</code></th>" in results["table_code_pipe"], (
        "行内代码里的竖线不能拆列"
    )
    # 反斜杠转义：标点按字面输出，普通字符保留反斜杠；不能出现 ${ch} 乱码
    table_escape = results["table_backslash_escape"]
    assert "<th>C:\\temp</th>" in table_escape, table_escape
    assert "<th>a*b</th>" in table_escape, table_escape
    assert "${ch}" not in table_escape, "反斜杠转义不能被模板字符串求值成 ${ch}"
    # 链接文字里的行内代码要还原成 <code>，不能留下不可见占位符
    label_code = results["link_label_code"]
    assert "<code>patch</code>" in label_code, label_code
    assert "\ue000" not in label_code and "\ue001" not in label_code, label_code
    # ID 校验与服务器同口径：设备保留名拒绝、长度按码点计数
    assert results["id_reserved_nul"] is False
    assert results["id_reserved_com1"] is False
    assert results["id_ok_console"] is True
    assert results["id_astral_33"] is True, "33 个增补平面字母（66 单元）按码点合法"
    assert results["id_astral_65"] is False, "65 个码点超过 64 上限"
    sentinel = results["sentinel_injection"]
    assert "" not in sentinel and "" not in sentinel, (
        "输入里的私用区占位符必须被剥离"
    )
    assert "<code>" not in sentinel and "<a " not in sentinel, (
        "伪造占位符不能注入代码 / 链接"
    )

    table = results["table"]
    assert "<table>" in table and "<th>日期</th>" in table and "<td>小雨</td>" in table
    assert results["heading"] == "<h2>标题</h2>", "标题最小层级压到 h2"

    assert results["slice_emoji"] == "😀😀…", "截断按码点，不能劈开代理对"
    assert results["pref_object"] == '{"a":1}', "对象偏好显示 JSON"
    assert results["pref_list"] == '汉庭、{"b":2}', "列表偏好用「、」拼接并保留对象 JSON"
    print("✓ 前端渲染 / 高亮 / 截断 / 偏好格式化基线")


def test_silent_reload_preserves_scroll():
    """静默重载（停止对账）不抢占阅读位置，普通切换仍滚到底。"""
    data = _run_harness("scrollPreserve")
    assert data["silent"] == {"scrollTop": 100, "calls": 0}, data
    assert data["normal"]["calls"] == 1, data
    assert data["normal"]["scrollTop"] == 1000, data
    print("✓ 静默重载保留滚动位置")


def test_search_hit_renders_bounded_window():
    """搜索命中在窗口外：只渲染一个窗口 + 「显示更新消息」补批。"""
    data = _run_harness("searchJump")
    initial = data["initial"]
    assert initial["count"] == 200, initial
    assert initial["partial"] is True, initial
    assert initial["earlierText"] == "", "命中在开头时不应出现更早消息按钮"
    assert initial["laterText"].startswith("显示更新消息"), initial
    # 静默重载（备份恢复后）保持同一窗口：不能恢复起点却渲染到结尾
    silent = data["silent"]
    assert silent["count"] == 200, silent
    assert silent["partial"] is True, silent
    assert silent["renderedStart"] == 0, silent
    first = data["afterFirst"]
    assert first["count"] == 400, first
    assert first["partial"] is True, first
    second = data["afterSecond"]
    assert second["count"] == 600, second
    assert second["partial"] is False, "补到会话结尾后末尾重新在 DOM 中"
    assert second["laterText"] == "", "到达结尾后按钮移除"
    assert data["afterThird"]["count"] == 600, data["afterThird"]
    print("✓ 搜索命中按窗口渲染（不会从命中处渲染到结尾）")


def test_silent_reload_restores_expanded_window():
    """静默重载恢复已展开的渲染起点，不把窗口重置回最近 200 条。"""
    data = _run_harness("silentRestoreWindow")
    assert data["initialCount"] == 200, data
    assert data["expandedStart"] == 100, data
    assert data["expandedCount"] == 400, data
    restored = data["restored"]
    assert restored["start"] == 100, restored
    assert restored["count"] == 400, restored
    assert restored["hasEarlier"] is True, restored
    print("✓ 静默重载恢复已展开的历史窗口")


def test_search_failure_restores_previous_query():
    """搜索失败后查询词回到旧值，列表与「加载更多」保持同一查询口径。"""
    data = _run_harness("sessionSearchFailure")
    assert data["searchQuery"] == "old", data
    assert data["sessions"] == ["s-old"], data
    assert any("加载会话列表失败" in item for item in data["toasts"]), data
    print("✓ 搜索失败恢复旧查询词（列表与分页不混入新查询）")


def test_history_renders_in_windows():
    """大历史首屏只渲染一个窗口，补批不请求网络、读完按钮消失。"""
    data = _run_harness("historyWindow")
    initial = data["initial"]
    assert initial["count"] == 200, initial
    assert initial["hasButton"] is True, initial
    assert "300" in initial["buttonText"], initial

    mid = data["afterFirst"]
    assert mid["count"] == 400, mid
    assert mid["hasButton"] is True and "100" in mid["buttonText"], mid

    done = data["afterSecond"]
    assert done["count"] == 500, done
    assert done["hasButton"] is False, done
    print("✓ 会话历史按窗口渲染并可补批")


def test_delete_last_turn_sends_turn_id():
    """删除最后一轮携带 client_turn_id：服务端据此精确匹配本轮。"""
    data = _run_harness("deleteTurn")
    assert data["turn_id"] == "turn-1", data
    assert data["expect"] == "问题", data
    assert data["removed"] is False, data
    print("✓ 删除最后一轮请求携带 turn_id")


def test_sse_stream_parsing_robustness():
    """CRLF 分帧、多行 data 与 data: null 都不能让流解析失效。"""
    events = _run_harness("sse")
    assert events == [
        {"type": "session", "session_id": "s"},
        {"type": "done", "reply": "ok"},
    ], events
    print("✓ SSE 解析兼容 CRLF / 多行 data，非对象帧被跳过")


def test_copy_fallback_cleans_up_and_restores_focus():
    """execCommand 抛错时隐藏 textarea 必须移除、焦点回到原元素。"""
    results = _run_harness("copy")
    assert results["removed"] == ["textarea"], results
    assert results["focus_restored"] == 1, results
    assert results["toasts"] == ["复制失败"], results
    print("✓ 复制回退异常时清理节点并恢复焦点")


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

    stale = results["stale"]
    assert stale["refreshes"] == 1, (
        "删除期间其它动作抢走结果条时，列表仍必须刷新（已删除项不能继续显示）"
    )
    assert any("已清理 1 条重复片段" in text for text in stale["toasts"]), stale
    print("✓ 知识库重复清理面板状态（含并发动作占用结果条）")


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
        "将向用户 web_user 新增 1 条、更新 0 条行程，跳过 73 条。继续？\n\n"
        "跳过原因（前 2 条）：\n"
        "第 3 行：目的地为空\n"
        "第 7 行：出发日期格式应为 YYYY-MM-DD"
    ), "确认框的跳过数量必须用 skipped_total（列表最多 50 条），并展示前几条原因"
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


def test_session_html_export_uses_full_server_endpoint():
    """面板「导出对话（HTML）」走服务端完整会话（不克隆窗口化 DOM）。"""
    results = _run_harness("sessionExportHtml")
    downloads = results["downloads"]
    assert len(downloads) == 1, downloads
    url = downloads[0]["url"]
    assert "/api/sessions/s1/export?" in url, url
    assert "format=html" in url, url
    assert "user_id=u1" in url, url
    assert downloads[0]["filename"] == "aligo_s1.html"
    assert results["toasts"] == ["HTML 已导出"]
    print("✓ 会话 HTML 导出走服务端完整会话接口")


def test_trips_export_button_uses_filtered_ics_endpoint():
    """「导出行程」按展开视图的关键词 / 状态筛选走 format=ics。"""
    results = _run_harness("tripsExport")
    filtered = results["filtered"]
    assert len(filtered) == 1, filtered
    url = filtered[0]["url"]
    assert url.startswith("/api/trips/export?"), url
    assert "format=ics" in url, url
    assert "q=%E4%B8%8A%E6%B5%B7" in url, "关键词筛选必须随导出提交"
    assert "status=past" in url, "状态筛选必须随导出提交"
    assert "user_id=u-trips" in url
    assert filtered[0]["filename"] == "aligo_trips.ics"

    collapsed = results["collapsed"][-1]
    assert "format=ics" in collapsed["url"], collapsed
    assert "q=" not in collapsed["url"] and "status=" not in collapsed["url"], (
        "收起视图不带筛选（与 CSV / HTML 导出同口径）"
    )
    print("✓ 导出行程按钮按筛选走 format=ics")


def test_trips_dedupe_panel_flow():
    """面板「清理重复」：空 / 取消 / 执行三种路径的请求与提示。"""
    results = _run_harness("tripsDedupe")

    empty = results["empty"]
    assert empty["urls"] == ["/api/trips/dedupe"], empty
    assert empty["methods"] == ["POST"]
    assert empty["bodies"] == [{"user_id": "u-dedupe", "dry_run": True}], empty
    assert empty["contentTypes"] == ["application/json"], (
        "去重是破坏性操作，必须走 JSON body（跨站表单发不出）"
    )
    assert empty["confirmCalls"] == 0, "没有重复时不弹确认"
    assert empty["toasts"] == ["没有重复行程可清理"]
    assert empty["refreshed"] == 0
    assert empty["button"] == {"text": "清理重复", "disabled": False}

    cancel = results["cancel"]
    assert cancel["urls"] == ["/api/trips/dedupe"], "取消确认后不能再发删除请求"
    assert cancel["bodies"][0]["dry_run"] is True
    assert cancel["confirmCalls"] == 1
    assert "2 条重复行程" in cancel["confirmMessage"]
    assert cancel["toasts"] == ["已取消清理重复行程"]
    assert cancel["refreshed"] == 0
    assert cancel["button"] == {"text": "清理重复", "disabled": False}

    applied = results["applied"]
    assert applied["urls"] == ["/api/trips/dedupe", "/api/trips/dedupe"], applied
    assert applied["methods"] == ["POST", "POST"]
    assert applied["bodies"][0] == {"user_id": "u-dedupe", "dry_run": True}
    assert applied["bodies"][1] == {"user_id": "u-dedupe"}, (
        "正式执行不能带 dry_run"
    )
    assert applied["contentTypes"] == ["application/json", "application/json"]
    assert applied["toasts"] == ["已清理 2 条重复行程"]
    assert applied["refreshed"] == 1, "执行成功后刷新一次数据概览"
    assert applied["button"] == {"text": "清理重复", "disabled": False}
    print("✓ 行程去重面板（空 / 取消 / 执行）")


def test_startup_does_not_override_user_session_click():
    """初始化期间的会话恢复不能作废用户已点击打开的会话。"""
    results = _run_harness("startup")
    restore = results["restore"]
    assert [item[0] for item in restore["opened"]] == ["saved-session"]
    assert restore["ready"] is True

    clicked = results["user_clicked"]
    assert [item[0] for item in clicked["opened"]] == ["clicked-session"], (
        "用户已在启动期间点开会话时，init 不能再按启动快照恢复旧会话"
    )
    assert clicked["ready"] is True
    print("✓ 启动恢复不覆盖用户点击")


def test_panel_fixes_cursor_stale_session_and_import_batches():
    """会话游标前进、幽灵会话移除、知识导入分批与字节预算。"""
    data = _run_harness("panelFixes")

    cursor = data["sessions_cursor"]
    assert "offset=50" in cursor["first_url"], cursor
    assert "offset=100" in cursor["second_url"], cursor
    assert cursor["offset"] == 100, cursor
    assert cursor["count"] == 75, cursor

    stale = data["stale_session"]
    assert stale["sessions"] == 0, stale
    assert stale["sessionId"] == "keep", stale
    assert any("已不存在" in item for item in stale["toasts"]), stale

    batches = data["kb_batches"]
    assert batches["total"] == 25000, batches
    assert batches["batch_count"] > 1, batches
    assert batches["max_batch"] <= 200, batches

    group_batches = data["kb_group_batches"]
    assert sum(len(batch) for batch in group_batches) == 4, group_batches
    for batch in group_batches:
        if "a" in batch:
            assert "c" in batch and "d" in batch, group_batches

    errors = data["kb_errors"]
    assert errors["oversize"], errors
    assert errors["huge_group"], errors
    print("✓ 面板修复：会话游标 / 幽灵会话 / 知识导入分批")


def test_reconcile_confirms_after_open_session():
    """停止对账：openSession 后以最新数据确认，迟到的落库不再误报未保存。"""
    data = _run_harness("reconcile")
    late = data["persistedLate"]
    assert late["input"] == "", late
    assert late["toasts"] == [], late
    assert late["open_calls"] == 1, late

    missing = data["stillMissing"]
    assert missing["input"] == "你好", missing
    assert any("没有保存" in item for item in missing["toasts"]), missing
    print("✓ 停止对账按最终确认结果判定是否保存")


def test_retry_checks_turn_persisted_first():
    """重试前按 client_turn_id 确认已落库：已保存则刷新历史、不再重发。"""
    data = _run_harness("retryGuard")
    persisted = data["persisted"]
    assert persisted["sent"] == [], persisted
    assert persisted["opened"] == 1 and persisted["removed"] == 1, persisted
    assert any("已保存" in item for item in persisted["toasts"]), persisted

    missing = data["missing"]
    assert missing["sent"] == ["你好"], missing
    assert missing["opened"] == 0, missing
    print("✓ 重试前检查本轮是否已落库（避免重复问答）")


def test_session_refresh_keeps_expanded_list():
    """刷新会话列表不能把「加载更多」展开的条数截回单页上限。"""
    data = _run_harness("sessionRefresh")
    assert data["count"] == 250, data
    assert data["offset"] == 250, data
    assert "limit=200" in data["first_url"], data
    assert "offset=200" in data["second_url"], data
    print("✓ 会话列表刷新按原数量续页合并")


def test_open_session_confirms_pending_edit():
    """切换会话时正在编辑的草稿要确认；取消则不发请求。"""
    data = _run_harness("editGuard")
    assert data["blocked"]["asked"] == 1, data
    assert data["blocked"]["queued"] == 0, data
    assert data["allowedQueued"] == 1, data
    print("✓ 切换会话前确认丢弃编辑草稿")


def test_chat_done_persisted_signal():
    """落库被墓碑否决：done(persisted=false) 标记提问为临时并切回新对话。"""
    data = _run_harness("chatPersisted")
    veto = data["veto"]
    assert veto["sessionId"] is None, veto
    assert veto["storedSession"] is None, veto
    assert veto["transient"] == "1", veto
    assert veto["finished"] == 1 and veto["reply"] == "回答", veto
    assert any("未保存" in item for item in veto["toasts"]), veto

    ok = data["ok"]
    assert ok["transient"] is None, "正常落库要清除临时标记"
    assert ok["veto_helper"] == "1", "persisted=false 的 done 仍要保留临时标记"
    assert ok["stopped"] == "1", "停止 / 失败的回合必须保留临时标记"
    print("✓ 聊天 done 事件透传落库状态（未保存 / 正常）")


def test_knowledge_import_skip_summary_distinguishes_conflicts():
    """导入跳过摘要区分「已存在 / 无效 / 布局冲突未导入」。"""
    data = _run_harness("kbImportSummary")
    assert data["conflict"] == "（跳过 2 条：2 条布局冲突未导入）", data
    mixed = data["mixed"]
    assert "1 条已存在" in mixed and "2 条无效" in mixed, mixed
    assert "3 条布局冲突未导入" in mixed, mixed
    assert data["none"] == "", data
    assert data["legacy"] == "（跳过 2 条：2 条已存在）", data
    print("✓ 知识导入跳过摘要区分布局冲突")


def test_knowledge_delete_legacy_parts_without_group():
    """旧版多分片（有 parts 无 group）单条删除：不能带 group=1（服务端 400）。"""
    data = _run_harness("kbDelete")
    assert data["legacy_url"] == "/api/knowledge/documents/7", data
    assert data["legacy_toasts"][-1].startswith("已删除该分片"), data
    assert "旧版多分片" in data["legacy_toasts"][-1], data
    assert data["group_url"] == "/api/knowledge/documents/8?group=1", data
    assert data["group_toasts"][-1] == "已删除整组分片", data
    assert data["single_url"] == "/api/knowledge/documents/9", data
    assert data["single_toasts"][-1] == "知识片段已删除", data
    print("✓ 旧版多分片删除不再带 group=1")


def test_user_backup_import_rejects_non_utf8():
    """面板恢复备份：GBK 等非 UTF-8 文件必须提示另存，不能静默损坏后导入。"""
    data = _run_harness("userBackupDecode")
    assert any("UTF-8" in toast for toast in data["gbk_toasts"]), data
    assert data["gbk_requests"] == 0, "解码失败不能发起导入请求"
    # 读取期间切换用户：取消恢复，不弹确认、不发请求
    assert any("用户已切换" in toast for toast in data["switch_toasts"]), data
    assert data["switch_requests"] == 0, "身份已切换不能继续导入"
    print("✓ 备份恢复拒绝非 UTF-8 文件")


if __name__ == "__main__":
    test_session_request_race_guards()
    test_knowledge_preview_locates_hits_in_original_text()
    test_frontend_render_helpers()
    test_knowledge_duplicates_panel_states()
    test_knowledge_dedupe_panel_states()
    test_knowledge_actions_arbitrate_shared_result_bar()
    test_trip_import_file_decoding_and_size_guard()
    test_trips_export_button_uses_filtered_ics_endpoint()
    test_trips_dedupe_panel_flow()
    test_startup_does_not_override_user_session_click()
    test_knowledge_eval_panel_states()
    test_panel_fixes_cursor_stale_session_and_import_batches()
    test_reconcile_confirms_after_open_session()
    test_knowledge_import_skip_summary_distinguishes_conflicts()
    test_knowledge_delete_legacy_parts_without_group()
    test_user_backup_import_rejects_non_utf8()
    test_chat_done_persisted_signal()
    test_search_hit_renders_bounded_window()
    test_silent_reload_restores_expanded_window()
    test_search_failure_restores_previous_query()
    print("\n全部通过")
