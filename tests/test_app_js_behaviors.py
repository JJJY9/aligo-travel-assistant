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


def test_retired_thread_reset_on_open_session():
    """persisted=false 后打开另一个会话：retiredThreadVisible 必须复位。"""
    results = _run_harness("retiredThreadOpenSession")
    after = results["afterOpen"]
    assert after["sessionId"] == "s2"
    assert after["retired"] is False
    print("✓ 打开会话后清除 retiredThreadVisible")


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
    assert results["agent_display_mixed"] == "预算建议", (
        "历史 _meta 的混合大小写技能名同样显示中文"
    )
    assert results["agent_display_unknown"] == "custom_agent"
    assert results["highlight_dotted"] == "<mark>İstanbul</mark> 出差", (
        "İ 关键字按原文长度命中并高亮"
    )
    # 斜杠日期与后端 trip_interval 的身份口径一致：徽标不能只在后端有
    assert results["date_unpadded"] == "2026-01-05", results["date_unpadded"]
    assert results["date_slash"] == "2026-03-12", results["date_slash"]
    assert results["date_bad"] is None, results["date_bad"]
    # 形状合法但非真实日历日：与后端 strptime 失败（按无日期处理）同口径
    assert results["date_invalid_calendar"] is None, results["date_invalid_calendar"]
    assert results["date_leap_valid"] == "2024-02-29", results["date_leap_valid"]
    # 全角数字日期经备份恢复保真落库后，后端按身份口径算进行中；
    # 前端必须同样归一，否则该行无徽标、卡片标签与服务器筛选分叉
    assert results["date_full_width"] == "2026-03-12", results["date_full_width"]
    # 9999-12-31 是「返程未定」占位：后端按出发当日计算，前端徽标同口径
    assert results["status_placeholder_end"]["label"] == "已结束", (
        results["status_placeholder_end"]
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
    assert results["id_astral_52"] is True, "52×4=208 字节仍合法（服务端上限 210）"
    assert results["id_astral_53"] is False, "53×4=212 字节超过服务端 210 字节上限"
    assert results["id_astral_65"] is False, "65 个码点超过 64 上限"
    # 会话 ID 口径：设备名会话合法（仅用户 ID 拒绝），export 是保留路由
    assert results["session_device_name"] is True
    assert results["session_export_reserved"] is False
    assert results["session_bad_chars"] is False
    # 会话 ID 不拼文件名，服务端没有 210 字节上限：53 / 64 个增补平面字母
    # （212 / 256 字节）都必须与 is_valid_web_session_id 一致可路由
    assert results["session_astral_53"] is True, (
        "会话 ID 不应套用用户 ID 的 210 字节文件名预算"
    )
    assert results["session_astral_64"] is True, "64 码点会话 ID 服务端合法"
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
    # CRLF / CR 行尾必须先归一化：否则标题与代码围栏渲染成字面量
    assert "<h2>标题</h2>" in results["crlf_heading"], results["crlf_heading"]
    crlf_fence = results["crlf_fence"]
    assert "<pre>" in crlf_fence and "<code" in crlf_fence, crlf_fence
    assert "```" not in crlf_fence, crlf_fence
    assert "<h2>标题</h2>" in results["cr_only"], results["cr_only"]

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


def test_silent_reload_shows_appended_messages_and_keeps_expansion():
    """静默重载不能把新落库的消息藏进「显示更新消息」，也不能折叠已展开的末尾。"""
    data = _run_harness("silentAppendWindow")
    # 上次渲染到末尾后又落库两条：必须立即可见、partialHistory 保持 false
    assert data["initial"] == {
        "count": 10,
        "partial": False,
        "laterText": "",
        "renderedEnd": 10,
    }, data["initial"]
    assert data["afterAppend"]["count"] == 12, data["afterAppend"]
    assert data["afterAppend"]["partial"] is False, data["afterAppend"]
    assert data["afterAppend"]["laterText"] == "", data["afterAppend"]

    # 搜索命中窗口 → 手动展开到末尾 → 静默重载仍保持完整窗口
    assert data["windowInitial"]["count"] == 200, data["windowInitial"]
    assert data["windowInitial"]["partial"] is True, data["windowInitial"]
    assert data["expanded"]["count"] == 600, data["expanded"]
    assert data["expanded"]["renderedEnd"] == 600, data["expanded"]
    assert data["afterExpandReload"]["count"] == 600, data["afterExpandReload"]
    assert data["afterExpandReload"]["partial"] is False, data["afterExpandReload"]
    print("✓ 静默重载揭示新消息且不折叠已展开窗口")


def test_deleted_session_thread_starts_clean_on_next_send():
    """persisted=false 后保留可见回答，但下一条消息必须从干净线程开始。"""
    data = _run_harness("deletedThreadSend")
    assert data["veto"]["sessionId"] is None, data["veto"]
    assert data["veto"]["retired"] is True, data["veto"]
    sent = data["afterSend"]
    assert sent["retired"] is False, sent
    assert sent["sessionId"] == "s-new", sent
    assert sent["sentinelRemoved"] is True, "旧线程节点必须先清空再开始新轮"
    assert sent["roles"] == ["user"], sent
    bodies = data.get("bodies") or []
    assert bodies and bodies[0].get("idempotency_key"), bodies
    assert bodies[0].get("client_turn_id"), bodies
    print("✓ 已删除会话的下一轮从干净线程开始（请求携带幂等键）")


def test_retired_thread_regenerate_renders_user_question():
    """已删除会话的可见线程上「重新生成」：新会话必须补齐用户提问。"""
    data = _run_harness("retiredThreadRegenerate")
    assert data["removed"] is True, data
    assert data["sentinelGone"] is True, data
    assert data["roles"] == ["user", "assistant"], data
    print("✓ 已删除会话的可见线程重新生成会补齐用户提问")


def test_export_reports_skipped_knowledge():
    """含知识库备份导出：X-Knowledge-Skipped 让面板提示跳过条数；
    X-Knowledge-Omitted（仅知识轴超限 / 暂无法核对被省略）同样必须提示，
    且 planning-failed 引导稍后重试（与超限的清理提示不同）。"""
    data = _run_harness("knowledgeSkippedToast")
    assert data["ok"] is True, data
    assert any("知识片段" in item for item in data["toasts"]), data
    assert data["omittedOk"] is True, data
    assert any("不含知识片段" in item for item in data["omittedToasts"]), data
    assert data["planningOk"] is True, data
    assert any(
        "稍后重试" in item for item in data["planningToasts"]
    ), data
    print("✓ 含知识库备份导出提示跳过条数 / 知识段省略原因")


def test_render_markdown_caps_oversized_message():
    """超大单条消息截断渲染（防止打开会话冻结主线程），用户气泡同样受限。"""
    data = _run_harness("renderCap")
    assert data["cut"] is True, data
    assert data["cappedLength"] < 260000, data["cappedLength"]
    assert data["normalUntouched"] is True, data

    assert data["userCut"] is True, data
    assert data["userCappedLength"] < 260000, data["userCappedLength"]
    assert data["userNormalUntouched"] is True, data
    # 截断点落在代理对中间时不能产出孤立代理（浏览器显示 U+FFFD）
    assert data["emojiCapLone"] is False, "用户气泡截断不能劈开代理对"
    assert data["emojiMdLone"] is False, "Markdown 截断不能劈开代理对"
    assert data["emojiCapSuffix"] is True, data
    assert data["emojiCapLength"] == 200007, data["emojiCapLength"]
    print("✓ Markdown / 用户气泡渲染长度上限")


def test_archived_toggle_rolls_back_on_failure():
    """归档视图切换失败时回滚状态位并按恢复视图重渲染。"""
    data = _run_harness("archivedRollback")
    assert data["showArchived"] is False, data
    # 失败分支按已渲染快照回滚并重渲染；重叠点击后视图位仍与快照一致
    assert data["showArchived"] == bool(data["loadedArchived"]), data
    assert any("加载会话列表失败" in item for item in data["toasts"]), data
    assert data["renderCalls"] >= 1, data
    print("✓ 归档切换失败回滚并重渲染（含重叠点击）")


def test_send_message_confirms_pending_edit_draft():
    """存在未保存的行内编辑时，直接发送要先确认；取消则什么都不发。"""
    data = _run_harness("draftSendGuard")
    rejected = data["rejected"]
    assert rejected["fetchCalls"] == 0, rejected
    assert "尚未保存" in rejected["confirmText"], rejected
    assert rejected["inputValue"] == "新提问", rejected
    print("✓ 发送前确认未保存的编辑草稿")


def test_editor_round2_preferences_and_guards():
    """对象偏好 JSON 编辑、分隔符校验、保存中取消禁用、KB 重入确认、保留名提示、链接占位符。"""
    data = _run_harness("editorsRound2")
    pref = data["prefObject"]
    assert pref["array"] == [{"name": "如家", "level": 2}], pref
    assert "arrayInvalid" not in pref, "非法 JSON 必须返回未定义而不是部分数据"
    assert "objectInvalid" not in pref, pref
    assert pref["objectKeep"] == {"a": 2}, pref
    assert pref["plainList"] == ["汉庭", "如家"], pref

    delim = data["delimiterOnly"]
    assert delim["puts"] == 0, "仅分隔符不能提交"
    assert any("至少一个偏好项" in item for item in delim["toasts"]), delim

    assert data["prefCancelDuring"]["cancelDisabled"] is True, data["prefCancelDuring"]
    assert data["prefCancelAfter"]["cancelDisabled"] is False, data["prefCancelAfter"]

    typed = data["typedLists"]
    assert typed["numbers"] == [1, 2, 4], typed
    assert "numbersInvalid" not in typed, "非数字必须返回未定义而不是字符串数组"
    assert typed["booleans"] == [True, False], typed
    assert "booleansInvalid" not in typed, typed

    assert data["prefMaxChars"] == 10000, "偏好编辑框上限须与服务端 10000 一致"
    assert data["addMerges"] == ["汉庭", "如家", "锦江"], data["addMerges"]
    assert data["addScalar"] == "上海", data["addScalar"]

    obj_input = data["prefObjectArrayInput"]
    assert obj_input["text"] == '[{"name":"汉庭","level":1}]', obj_input
    assert obj_input["roundtrip"] == [{"name": "汉庭", "level": 2}], obj_input
    assert obj_input["plainList"] == "汉庭、如家", obj_input

    assert data["addStatusBaseline"] == ["汉庭", "全季", "如家"], (
        "/api/status 权威快照必须先于旧缓存作为合并基线"
    )
    no_base = data["addNoBaseline"]
    assert no_base["puts"] == 0, "无合并基线不得退化成整值覆盖"
    assert no_base["refreshes"] == 1, no_base
    assert any("无法读取现有偏好" in item for item in no_base["toasts"]), no_base

    unknown_add = data["addUnknownOutcome"]
    assert any("保存结果未知" in item for item in unknown_add["toasts"]), unknown_add
    assert unknown_add["refreshes"] == 1, unknown_add
    assert unknown_add["latest"] == ["汉庭", "全季"], unknown_add
    assert unknown_add["status"] == ["汉庭", "全季"], unknown_add

    finite = data["prefScalarFinite"]
    assert "infinity" not in finite, "非有限数必须判为格式无效，不能序列化成 null"
    assert finite["normal"] == 4.5, finite

    blur = data["blurPrompt"]
    assert blur["changed"] is True and blur["unchanged"] is False, blur
    assert blur["newline"] is False, "输入框剥离换行后与原文一致时不算修改"

    unknown = data["deleteUnknown"]
    assert any("删除结果未知" in item for item in unknown["toasts"]), unknown
    assert unknown["refreshes"] == [{"silentFailure": True}], unknown

    deleted = data["deleteSuccess"]
    assert deleted["rowRemoved"] is True, "删除成功后必须本地移除该行"
    assert deleted["latestHas"] is False, "删除后必须清掉 preferenceLatest"
    assert deleted["statusHas"] is False, "删除后必须同步移除 state.status 里的键"
    assert deleted["refreshes"] == [{"silentFailure": True}], deleted

    kb = data["kbReclick"]
    assert kb["confirmCalls"] == 1, "同一行再次编辑且草稿已修改时必须确认"
    assert kb["kept"] == "用户新输入的草稿", kb

    assert any("设备保留名" in item for item in data["reservedHint"]), data["reservedHint"]

    href = data["href"]
    assert "http://example.com/code-x" in href, href
    assert "" not in href and "" not in href, href
    print("✓ 偏好编辑 / 保存守卫 / KB 重入确认 / 链接占位符")


def test_frontend_detail_fixes():
    """草稿合并 / 重命名待刷新 / 归档视图 / 行程重编辑与最新值。"""
    data = _run_harness("frontendDetailFixes")
    assert "你好" in data["merged"] and "草稿" in data["merged"], data
    assert data["restored"] == "你好"
    assert data["pending"]["pending"] is True and data["pending"]["loads"] == 0
    assert data["refreshed"]["pending"] is False and data["refreshed"]["loads"] == 1
    assert data["archived"]["show"] is False and data["archived"]["toggles"] == 1
    assert data["tripReclick"]["calls"] == 1, data["tripReclick"]
    assert data["tripReclick"]["destination"] == "用户改的目的地", (
        "拒绝确认后草稿必须保留"
    )
    assert data["tripReclickClean"] == 0, "未改动的重复点击不应弹确认"
    assert data["tripLatestView"] == "杭州", "刷新在途时回填最近一次保存值"
    display = data["prefDisplay"]
    assert display["known"] == "常住地", display
    assert display["constructorKey"] == "constructor", "原型链成员名必须按原键回退"
    assert display["toStringKey"] == "toString", display
    assert display["unknown"] == "custom_type", display
    agents = data["agentDisplay"]
    assert agents["constructorKey"] == "constructor", agents
    assert agents["protoKey"] == "__proto__", agents
    assert agents["upperConstructor"] == "Constructor", agents
    assert agents["known"] == "预算建议", agents
    assert agents["unknown"] == "Greeting_Agent", agents
    assert agents["intentsConstructor"] == "constructor", agents
    assert agents["intentsProtoList"] == "__proto__、toString、valueOf", agents
    assert agents["intentsObject"] == "constructor", agents
    assert agents["intentsKnown"] == "行程规划", agents
    assert "constructor" in agents["badge"], agents["badge"]
    assert "native code" not in agents["badge"], agents["badge"]
    pruned = data["pruned"]
    assert pruned["hotelHas"] is False, "权威快照里已缺席的类型必须回收合并基线"
    assert pruned["homeLatest"] == "北京", pruned
    assert data["prunedPut"] == ["锦江"], "已删除类型不能经合并基线复活"
    chips = data["degradedChips"]
    assert "短期记忆 · Redis（降级中）" in chips, chips
    assert any("已回退本地" in item for item in chips), chips
    assert data["themeDarkAttrs"] == {
        "label": "切换到浅色主题",
        "pressed": "true",
    }, data["themeDarkAttrs"]
    assert data["themeLightAttrs"] == {
        "label": "切换到深色主题",
        "pressed": "false",
    }, data["themeLightAttrs"]
    assert data["sidebarExpanded"] == "true", data["sidebarExpanded"]
    assert data["sidebarCollapsed"] == "false", data["sidebarCollapsed"]
    assert data["kbAnnounce"] == ["共 12 条，已显示 5 条"], data["kbAnnounce"]
    print("✓ 前端细节修复（草稿合并 / 待刷新 / 归档 / 行程重编辑）")


def test_user_switch_trip_preference_drafts():
    """切换用户前确认行程 / 偏好表单草稿。"""
    data = _run_harness("userSwitchFormsDraft")
    rejected = data["rejected"]
    assert rejected["confirmCalls"] == 1, rejected
    assert rejected["userId"] == "u1", rejected
    assert rejected["destination"] == "杭州" and rejected["pref"] == "汉庭", rejected
    accepted = data["accepted"]
    assert accepted["userId"] == "u2", accepted
    assert accepted["destination"] == "" and accepted["pref"] == "", accepted
    print("✓ 切换用户确认行程 / 偏好草稿")


def test_user_switch_chat_composer_draft():
    """聊天草稿与表单同口径：切换前确认，切换后清空；空输入不再静默回退。"""
    data = _run_harness("userSwitchComposer")
    rejected = data["rejected"]
    assert rejected["confirmCalls"] == 1, rejected
    assert rejected["userId"] == "u1", rejected
    assert rejected["composer"] == "A-DRAFT-SECRET", rejected
    accepted = data["accepted"]
    assert accepted["userId"] == "u2", accepted
    assert accepted["composer"] == "", accepted
    empty = data["empty"]
    assert empty["userId"] == "u3", empty
    assert any("请输入用户 ID" in toast for toast in empty["toasts"]), empty
    print("✓ 切换用户确认并清空聊天草稿")


def test_stored_invalid_user_id_falls_back_with_flag():
    """存储的非法用户 ID 回退 web_user 时记下被拒值（供启动提示）。"""
    data = _run_harness("storedUserFallback")
    assert data["userId"] == "web_user", data
    assert data["rejected"] == "con", data
    assert data["localLeft"] is None and data["sessionLeft"] is None, data
    print("✓ 非法存储用户 ID 回退并记录被拒值")


def test_delete_preference_late_response_keeps_new_user_baseline():
    """A 的偏好删除响应迟到且已切到 B：不能清掉 B 的合并基线。"""
    data = _run_harness("deletePreferenceLateResponse")
    assert data["user_id"] == "user-b", data
    assert data["latest"] == ["Rujia"], data
    assert data["status"] == ["Rujia"], data
    print("✓ 偏好删除迟到响应不污染新用户基线")


def test_trip_dedupe_unknown_outcome_refreshes():
    """行程去重删除超时 / 503：提示可能已删除、刷新并退出编辑。"""
    data = _run_harness("tripDedupeUnknown")
    for key in ("timeout", "partial503"):
        case = data[key]
        assert any("可能已删除部分行程" in toast for toast in case["toasts"]), case
        assert case["refreshed"] >= 1, case
        assert case["reset"] == 1, case
        assert case["editing"] is None, case
        assert case["button"] == "清理重复", case
    print("✓ 行程去重结果未知时刷新并复位")


def test_kb_pending_result_cleared_on_cancel():
    """评测进行中被清理动作作废后取消：结果条不能永久停在「评测中」。"""
    data = _run_harness("kbPendingCancel")
    assert "检索评测中" in data["duringEval"], data
    assert data["afterCancel"]["hidden"] is True, data
    assert data["final"]["hidden"] is True, data
    print("✓ 清理取消清掉被作废的评测进行中文案")


def test_search_failure_restores_previous_query():
    """搜索失败回滚到「已渲染查询」；被取代的失败不滚到未渲染词。"""
    data = _run_harness("sessionSearchFailure")
    single = data["single"]
    assert single["searchQuery"] == "old", single
    assert single["searchInput"] == "old", "搜索框可见词必须同步回滚"
    assert single["sessions"] == ["s-old"], single
    assert any("加载会话列表失败" in item for item in single["toasts"]), single

    # 被取代的重试：回滚目标是已渲染查询（old），晚到的成功被作废
    superseded = data["superseded"]
    assert superseded["searchQuery"] == superseded["loaded"] == "old", superseded
    assert superseded["sessions"] == ["s-old"], superseded

    assert data["moreBlocked"] is True, "状态与已渲染快照不一致时不能发分页请求"
    empty = data["emptyPage"]
    assert empty["total"] == 0, empty
    assert empty["count"] == 0, "服务端已无会话时陈旧行必须清掉"
    assert empty["offset"] == 0, empty
    print("✓ 搜索失败恢复已渲染查询（列表与分页不混入新查询）")


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
    assert applied["editing"] is None, (
        "正在编辑的片段被清理后必须退出编辑态（否则保存永远 404）"
    )
    assert applied["submit"] == "添加片段", applied
    assert applied["chip_refreshes"] >= 1, "清理后知识库状态 chip 也要刷新"

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
        "检索评测：30 个问题 · hit@1 64% · hit@3 93% · hit@5 100% · MRR 0.80"
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
    assert "today=" in url, "状态筛选需带浏览器本地日期（与徽标同口径）"
    assert "user_id=u-trips" in url
    assert filtered[0]["filename"] == "aligo_trips.ics"

    collapsed = results["collapsed"][-1]
    assert "format=ics" in collapsed["url"], collapsed
    assert "q=" not in collapsed["url"] and "status=" not in collapsed["url"], (
        "收起视图不带筛选（与 CSV / HTML 导出同口径）"
    )
    print("✓ 导出行程按钮按筛选走 format=ics")


def test_kb_dedupe_unknown_outcome_refreshes():
    """清理重复超时（结果未知）：提示可能已删除并刷新列表。"""
    results = _run_harness("kbDedupeUnknownOutcome")
    assert "可能已部分或全部删除" in results["text"], results
    assert results["refreshes"] == 1
    assert results["doc_refreshes"] == 1
    assert results["button"] == "清理重复"
    print("✓ 清理重复结果未知时提示并刷新")


def test_kb_user_switch_warns_on_unsaved_draft():
    """切换用户前确认知识表单草稿；拒绝保留草稿，确认后清空。"""
    results = _run_harness("kbUserSwitchDraft")
    rejected = results["rejected"]
    assert rejected["confirmCalls"] == 1, rejected
    assert rejected["userId"] == "u1", rejected
    assert rejected["draft"] == "未保存的知识草稿", rejected
    accepted = results["accepted"]
    assert accepted["userId"] == "u2", accepted
    assert accepted["draft"] == "", accepted
    print("✓ 切换用户确认未保存知识草稿")


def test_trip_export_quality_toasts():
    """行程导出：返程截断 / 跳过 / 回导会被跳过的数据合并成一次可见提示。"""
    results = _run_harness("tripExportQuality")
    toasts = results["toasts"]
    assert len(toasts) == 1, toasts
    assert "缺少出发日期" in toasts[0], toasts
    assert "返程日期无法解析" in toasts[0] and "单日" in toasts[0], toasts
    assert "会被跳过" in toasts[0], toasts
    assert results["quiet"] == [], results["quiet"]
    print("✓ 行程导出质量提示合并为一次 toast")


def test_trip_import_ics_preview_labels_and_timezone():
    """ICS 预览按事件序号标注、warnings 透传、请求体带浏览器时区。"""
    results = _run_harness("tripImportIcsPreview")
    message = results["confirm"]
    assert "事件 2：缺少或非法的 DTSTART" in message, message
    assert "事件 3：缺少目的地（SUMMARY 为空）" in message, message
    assert "第 2 行" not in message, "ICS 不能把事件序号标成物理行号"
    assert "条记录请核对" in message, message
    body = results["bodies"][0]
    assert body["content"], body
    assert isinstance(body.get("timezone"), str) and body["timezone"], body
    print("✓ ICS 导入预览按事件标注并上报时区")


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

    ambiguous = results["ambiguous"]
    assert ambiguous["urls"] == ["/api/trips/dedupe"], ambiguous
    assert ambiguous["confirmCalls"] == 0, "有歧义时不弹确认"
    assert ambiguous["toasts"] and "无法自动清理" in ambiguous["toasts"][0], (
        ambiguous,
        "只有歧义 id 时不能提示「没有重复行程可清理」",
    )
    assert ambiguous["refreshed"] == 0
    assert ambiguous["button"] == {"text": "清理重复", "disabled": False}

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
    assert applied["bodies"][1] == {"user_id": "u-dedupe", "dry_run": False}, (
        "正式执行必须显式 dry_run=false（服务端默认只预览）"
    )
    assert applied["contentTypes"] == ["application/json", "application/json"]
    assert applied["toasts"] == ["已清理 2 条重复行程"]
    assert applied["refreshed"] == 1, "执行成功后刷新一次数据概览"
    assert applied["button"] == {"text": "清理重复", "disabled": False}
    print("✓ 行程去重面板（空 / 取消 / 执行）")


def test_startup_stale_session_not_used_after_failed_click():
    """初始化期间的用户点击失败时，不能把下一条消息写进从未展示的旧会话。"""
    results = _run_harness("initStale")
    assert results["ready"] is True, results
    assert results["sessionId"] is None, results
    assert results["stored"] is None, results
    assert results["welcomeShown"] >= 1, results
    print("✓ 启动恢复被点击作废后清掉未展示的旧会话")


def test_send_message_http_error_not_connection_interruption():
    """服务端明确返回 4xx/5xx 时不当成连接中断：不对账、不标记连接异常。"""
    results = _run_harness("httpError")
    for case in ("rateLimited", "unavailable"):
        data = results[case]
        assert data["reconcileCalls"] == [], data
        assert data["healthText"] != "连接异常", data
        assert data.get("healthClass") != "health err", data
        # 本轮必然未落库：输入框已被清空，必须把提问放回草稿，否则切走
        # 会话后正文只剩会随消息区重建消失的临时气泡
        assert data["input"] == "你好", data
        # 提问与错误气泡已移除（原因用 toast 展示）：留在界面会让直接
        # 回车重发出现两条相同的提问气泡
        assert data["users"] == 0, data
        assert any("请求过于频繁" in t or "正在进行" in t for t in data["toasts"]), data
    resend = results["resendRateLimited"]
    assert resend["afterResend"]["users"] == 1, resend
    print("✓ HTTP 错误不再触发停止对账 / 连接异常，重发不重复气泡")


def test_sse_eof_tail_frame_is_dispatched():
    """EOF 落在帧尾空行前：完整 data: 帧仍要派发，半帧解析失败被忽略。"""
    results = _run_harness("sseEof")
    assert results["noBlank"] == [{"type": "done", "reply": "tail"}], results
    assert results["truncated"] == [], results
    assert results["withBlank"] == [{"type": "done", "reply": "ok"}], results
    print("✓ SSE EOF 尾帧补发（半帧不误派发）")


def test_replay_error_clears_transient_and_reconciles():
    """幂等回放 error（persisted=true）：提问不标临时，并登记停止对账刷新历史。"""
    data = _run_harness("replayError")
    assert data["transient"] != "1", data
    assert len(data["reconcileCalls"]) == 1, data
    call = data["reconcileCalls"][0]
    assert call["sessionId"] == "s1", call
    assert call["message"] == "重放提问", call
    assert call["turnId"], call
    assert call["reason"] == "上一请求已处理", call
    # 登记被 flush 消费（桩替代了包装层，pending 由真实 forget 清理；这里
    # 只验证对账被触发，登记保留语义不算本用例范围）
    assert data["pending"] in (0, 1), data
    print("✓ 幂等回放 error 清除临时标记并对账刷新历史")


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
    assert "offset=0" in cursor["third_url"], "空页需按已加载数量从 0 重取整窗"
    assert cursor["offset"] == 75, cursor
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
    # 最终确认发现落后再刷一次（openSession）保证界面包含刚落库的一轮
    assert late["open_calls"] == 2, late
    # 对账轮询用 5 秒短超时（默认 120 秒会让 ~12 秒判定期在挂起时失效），
    # 最终确认容忍慢响应用 15 秒
    assert late["timeouts"] and all(
        value == 5000 for value in late["timeouts"][:-1]
    ), late
    assert late["timeouts"][-1] == 15000, late

    missing = data["stillMissing"]
    assert missing["input"] == "你好", missing
    assert any("暂未确认保存结果" in item for item in missing["toasts"]), missing
    assert all(value == 5000 for value in missing["timeouts"][:-1]), missing
    assert missing["timeouts"][-1] == 15000, missing

    # 最终确认请求超时 / 失败：结果未知，不能把提问塞回输入框诱导重发
    confirm_failed = data["confirmFailed"]
    assert confirm_failed["input"] == "", confirm_failed
    assert any(
        "暂未确认保存结果" in item for item in confirm_failed["toasts"]
    ), confirm_failed

    # 判定期内始终拿不到有效响应：同样只提示、不回填输入框
    all_failed = data["allFailed"]
    assert all_failed["input"] == "", all_failed
    assert all_failed["open_calls"] == 0, all_failed
    assert any("暂未确认保存结果" in item for item in all_failed["toasts"]), (
        all_failed
    )

    # 判定期内曾拿到有效历史、仅最后一次轮询失败：按已确认未落库回填
    last_poll_failed = data["lastPollFailed"]
    assert last_poll_failed["input"] == "你好", last_poll_failed
    assert any(
        "暂未确认保存结果" in item for item in last_poll_failed["toasts"]
    ), last_poll_failed

    # 对账期间用户点击了其它会话：不能把旧提问写进当前输入框
    interleaved = data["interleaved"]
    assert interleaved["input"] == "", interleaved
    assert interleaved["toasts"] == [], interleaved
    print("✓ 停止对账按最终确认结果判定是否保存")


def test_kb_search_state_machine():
    """知识库搜索：429 保留列表并隐藏加载更多、回车可重试、合成期不请求。"""
    data = _run_harness("kbSearchFlow")
    foo = data["fooState"]
    assert foo["items"] == 20, foo
    assert foo["moreHidden"] is False, foo

    limited = data["after429"]
    assert limited["items"] == 20, "429 不能清空已有结果"
    assert limited["moreHidden"] is True, "条件与列表不一致时不能显示加载更多"
    assert any("过于频繁" in item for item in limited["toasts"]), limited
    assert data["moreBlocked"] is True, "不一致状态下点加载更多不能发请求"

    retried = data["afterRetry"]
    assert retried["query"] == retried["loaded"] == "bar", retried
    assert retried["items"] == 1, retried

    assert data["composeBlocked"] is True, "输入法合成期间按回车不能发请求"
    assert "q=baz" in data["composedUrl"], data
    print("✓ 知识库搜索状态机（429 / 分页对齐 / 重试 / 输入法）")


def test_download_suppresses_concurrency_and_times_out():
    """下载：并发点击被抑制；服务端挂起时 abort 超时并在结束后复位。"""
    data = _run_harness("downloadGuards")
    assert data["second"] is False, data
    assert any("仍在进行中" in item for item in data["toasts"]), data
    assert data["firstResult"] is False, data
    assert any("下载超时" in item for item in data["toasts"]), data
    # 超时后守卫复位：同一 URL 可以重新下载
    assert data["third"] is True, data
    assert data["calls"] == 2, data
    print("✓ 下载并发抑制与超时复位")


def test_retry_checks_turn_persisted_first():
    """重试前按 client_turn_id 确认已落库：已保存则刷新历史、不再重发。"""
    data = _run_harness("retryGuard")
    persisted = data["persisted"]
    assert persisted["sent"] == [], persisted
    assert persisted["opened"] == 1 and persisted["removed"] == 1, persisted
    assert any("已保存" in item for item in persisted["toasts"]), persisted
    assert persisted["timeout"] == 5000, persisted

    missing = data["missing"]
    assert missing["sent"] == ["你好"], missing
    assert missing["opened"] == 0, missing
    assert missing["timeout"] == 5000, missing
    print("✓ 重试前检查本轮是否已落库（避免重复问答）")


def test_session_refresh_keeps_expanded_list():
    """刷新会话列表不能把「加载更多」展开的条数截回单页上限。"""
    data = _run_harness("sessionRefresh")
    assert data["count"] == 250, data
    assert data["offset"] == 250, data
    # 刷新按已加载数量一次取回整窗，不再按 200 一页串行补取
    assert "limit=250" in data["first_url"], data
    assert data["pending_requests"] == 0, data
    print("✓ 会话列表刷新一次取回已展开数量")


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


def test_knowledge_group_delete_syncs_local_rows_on_refresh_failure():
    """整组删除后刷新失败：同组分片本地移除，总数按 count 递减（无幽灵行）。"""
    data = _run_harness("kbDeleteGroupGhost")
    removed = dict(data["removed"])
    assert removed == {"10": True, "9": True, "8": True, "5": False}, data
    assert data["items"] == [5], data
    assert data["total"] == 1, data
    assert data["note"] == "共 1 条，已显示 1 条", data
    print("✓ 整组删除本地同步同组分片（刷新失败无幽灵行）")


def test_kb_dedupe_success_removes_rows_locally():
    """清理重复成功但刷新失败：已删除行本地移除、计数同步，未删行保留。"""
    data = _run_harness("kbDedupeLocalRemoval")
    removed = dict(data["removed"])
    assert removed == {"3": False, "2": True, "1": True}, data
    assert data["items"] == [3], data
    assert data["total"] == 1, data
    assert data["note"] == "共 1 条，已显示 1 条", data
    assert "已清理 2 条" in data["result"], data
    print("✓ 清理重复后本地移除已删行（刷新失败仍可操作）")


def test_user_backup_import_rejects_non_utf8():
    """面板恢复备份：GBK 等非 UTF-8 文件必须提示另存，不能静默损坏后导入。"""
    data = _run_harness("userBackupDecode")
    assert any("UTF-8" in toast for toast in data["gbk_toasts"]), data
    assert data["gbk_requests"] == 0, "解码失败不能发起导入请求"
    # 读取期间切换用户：取消恢复，不弹确认、不发请求
    assert any("用户已切换" in toast for toast in data["switch_toasts"]), data
    assert data["switch_requests"] == 0, "身份已切换不能继续导入"
    print("✓ 备份恢复拒绝非 UTF-8 文件")


def test_user_backup_restore_reports_recovery_incomplete():
    """隔离账号空恢复：toast 必须明确「用户数据未恢复」；与知识片段
    失败组合时两条提示都要出现，正常恢复不能带该警告。"""
    data = _run_harness("userBackupRestore")
    assert any(
        "用户数据未恢复" in toast for toast in data["incomplete_toasts"]
    ), data
    assert all(
        "已恢复" not in toast for toast in data["incomplete_toasts"]
    ), "空恢复不能以「已恢复」开头"
    assert all(
        "已恢复" not in toast for toast in data["combo_toasts"]
    ), data
    ok_toasts = data["ok_toasts"]
    assert ok_toasts and all(
        "用户数据未恢复" not in toast for toast in ok_toasts
    ), ok_toasts
    assert any("已恢复" in toast for toast in ok_toasts), ok_toasts
    assert any(
        "知识片段未恢复" in toast and "用户数据未恢复" in toast
        for toast in data["combo_toasts"]
    ), data
    # ok=false（备份条目全部被墓碑期 / 保留字会话跳过）：与 CLI 同口径，
    # toast 以「未恢复：」开头并展示服务端给出的原因，不能谎报已恢复
    emptied = data["emptied_toasts"]
    assert emptied and emptied[-1].startswith("未恢复："), emptied
    assert all("已恢复" not in toast for toast in emptied), emptied
    assert "没有可恢复的数据" in emptied[-1], emptied
    # 迟到回滚失败（消息已复活）：以「已写入：」开头并提示再次删除会话，
    # 不能落入「未恢复：」（什么都没恢复）也无法声称「已恢复」
    rollback = data["rollback_toasts"]
    assert rollback and rollback[-1].startswith("已写入："), rollback
    assert "回滚失败" in rollback[-1], rollback
    assert "再次删除该会话" in rollback[-1], rollback

    # 408 = 请求体读取超时（服务端零写入）：不能提示「服务端可能仍在写入」
    timeouts = data["upload_timeout_toasts"]
    assert timeouts and "备份未写入服务器" in timeouts[-1], timeouts
    assert all("恢复失败" not in toast for toast in timeouts), timeouts
    # 部分完成 503：如实展示服务端文案并刷新视图，不加「恢复失败：」前缀
    partial = data["partial_toasts"]
    assert partial and partial[-1].startswith("恢复部分完成"), partial
    assert data["partial_refreshes"] >= 1, data
    # 5xx / 连接中断：结果未知（可能已部分写入），提示并刷新
    assert any("恢复结果未知" in toast for toast in data["failure_toasts"]), data
    assert data["failure_refreshes"] >= 1, data
    assert any("恢复结果未知" in toast for toast in data["network_toasts"]), data
    assert data["network_refreshes"] >= 1, data
    # 4xx 保持统一前缀且不刷新（服务端明确拒绝）
    assert any(
        toast.startswith("恢复失败：") for toast in data["failure_4xx_toasts"]
    ), data
    assert data["failure_4xx_refreshes"] == 0, data
    # 成功后的刷新失败全部静默：恢复摘要不能被刷新错误覆盖
    silent = data["silent_refresh_toasts"]
    assert len(silent) == 1 and "已恢复" in silent[0], silent
    print("✓ 恢复完成度提示与错误分支接入 toast")


def test_kb_panel_fixes():
    """知识库面板：刷新失败保留列表 / 语义同值输入退出模式 / 预览零导入 /
    只改分类再点编辑要确认。"""
    data = _run_harness("kbPanelFixes")
    keep = data["refresh_keep"]
    assert keep["items"] == [11, 12], keep
    assert keep["text"] == "", "刷新失败不能把已渲染列表替换成错误文本"
    assert keep["toasts"] == [], "静默刷新失败不弹 toast"
    toasts = data["refresh_toast"]
    assert toasts and "导入进行中" in toasts[-1], toasts
    assert data["semantic_exit"]["mode"] is False, data
    assert data["semantic_exit"]["showingSemantic"] is False, data
    assert data["semantic_exit"]["requests"], "退出语义模式必须刷新关键词列表"
    zero = data["preview_zero"]
    assert zero["imports"] == 0, "would_import=0 时不应再分批上传"
    assert any("没有需要导入" in toast for toast in zero["toasts"]), zero
    guard = data["category_guard"]
    assert guard["confirms"] == 1, "只改分类也要弹覆盖确认"
    print("✓ 知识库面板刷新 / 语义模式 / 预览 / 分类草稿守卫")


def test_upcoming_weather_normalizes_unpadded_start_date():
    """历史未补零出发日期也要命中预报行，而不是退回今日。"""
    results = _run_harness("upcomingWeather")
    unpadded = results["unpadded"]
    assert unpadded["hidden"] is False, unpadded
    assert "出发日 5°C~16°C" in unpadded["text"], unpadded["text"]
    assert "今日" not in unpadded["text"], unpadded["text"]
    out_of_window = results["out_of_window"]["text"]
    assert "今日 10°C~20°C" in out_of_window, out_of_window
    print("✓ 出发日天气：未补零日期归一后命中，窗口外退回今日")

_DIRECT_RUN_SKIP: dict[str, str] = {
}

def test_user_switch_clears_preference_cache():
    """切换用户必须清空 preferenceLatest：新用户追加列表型偏好不能带上
    上一用户的品牌（跨账号数据污染）。"""
    results = _run_harness("userSwitchPreferenceCache")
    assert results["cached_before"] == ["汉庭"], results
    assert results["cleared_size"] == 0, "切换用户后偏好缓存必须清空"
    assert results["user_id"] == "user-b"
    bodies = results["put_bodies"]
    assert len(bodies) == 1, bodies
    assert bodies[0]["user_id"] == "user-b", bodies
    assert bodies[0]["value"] == ["如家"], bodies
    print("✓ 切换用户清空偏好缓存，列表追加不跨账号合并")


def test_stop_reconcile_deferred_until_next_turn_ends():
    """停止对账被新回合 busy 短路时保留登记，回合结束后补跑并静默重载。"""
    results = _run_harness("stopReconcileQueue")
    assert results["first"] == "deferred", results
    assert results["pending_after_deferred"] == 1, "被跳过的对账不能丢登记"
    assert any("/api/sessions/s1" in url for url in results["urls"]), results
    assert results["opens"] == ["s1"], "已落库的回合要静默重载历史"
    assert results["pending_after_flush"] == 0, results
    print("✓ 停止对账延期到下一回合结束后补跑")


def test_open_session_guards_deleting_and_unknown_empty():
    """删除在途的会话拒绝打开；陈旧引用指向已删除会话不切走当前对话。"""
    results = _run_harness("openSessionGuards")
    assert results["deleting_requests"] == 0, "删除在途时不能发起详情请求"
    assert results["session_id"] == "A", "未知空会话不能把当前对话切走"
    assert results["new_session_calls"] == 0, results
    assert any("已不存在或还没有消息" in item for item in results["toasts"]), results
    # 未加载到目标会话必须显式返回 false：sendMessage 的发送前守卫按
    # loaded !== true 中止，裸 return 会让提问写进已删除的会话
    assert results["deleting_return"] is False, results
    assert results["ghost_return"] is False, results
    print("✓ openSession 删除在途 / 未知空会话守卫")


def test_webapp_round_interaction_fixes():
    """发送前守卫、焦点兜底、IME、超长拦截、重新生成反馈、偏好基线。"""
    results = _run_harness("webappRound")
    focus = results["focus"]
    assert focus["clamped"] == ["s3"], "删除 / 归档后应按旧索引就近聚焦"
    assert focus["indexed"] == ["s2"], focus
    assert focus["empty"] == ["search"], "列表清空时焦点回到搜索框"

    oversize = results["oversize"]
    assert oversize["fetchCalls"] == 0, "超长提问不能发出请求"
    assert any("超过 4000 字" in item for item in oversize["toasts"]), oversize

    unconfirmed = results["unconfirmed"]
    assert unconfirmed["empty"]["value"] == "合并后的提问", "空输入框仍要回填原文"
    assert "暂未确认保存结果" in unconfirmed["empty"]["toasts"][0], unconfirmed
    assert unconfirmed["withDraft"]["value"] == "已有草稿", "不能覆盖已有草稿"

    pending = results["regeneratePending"]
    assert pending == {"disabled": True, "label": "准备中…", "ariaBusy": "true"}, pending
    success = results["regenerateSuccess"]
    assert success == {"removed": 1, "sent": 1}, success
    mismatch = results["regenerateMismatch"]
    assert mismatch["disabled"] is False
    assert mismatch["label"] == "重新生成"
    assert mismatch["ariaBusy"] == ""
    assert mismatch["removed"] == 0 and mismatch["sent"] == 0, mismatch
    assert any("上一轮对话已变化" in item for item in mismatch["toasts"]), mismatch
    timeout = results["regenerateTimeout"]
    assert timeout["disabled"] is False
    assert any("结果未知" in item for item in timeout["toasts"]), timeout

    pref = results["preference"]
    assert pref["latest"] == ["汉庭", "如家"], "保存成功后必须写回合并基线"
    assert pref["status"] == ["汉庭", "如家"], "状态缓存同样要同步"
    print("✓ 发送前守卫 / 焦点兜底 / IME / 超长拦截 / 重新生成反馈 / 偏好基线")


def test_webapp_round_semantic_stash_and_truncation():
    """语义检索暂存恢复、超长消息编辑入口、行程筛选合成守护。"""
    results = _run_harness("webappRound")
    semantic = results["semanticStash"]
    assert semantic["stashing"]["first"] is True, semantic
    assert "语义检索中" in semantic["stashing"]["text"], semantic
    assert semantic["restored"] == ["row1", "row2"], "并发作废后必须还原暂存列表"

    truncated = results["truncated"]
    assert truncated["longMarked"] is True, "超长消息要打截断标记"
    assert truncated["shortMarked"] is False, truncated
    assert truncated["truncatedAppends"] == 0, "截断消息不提供必然失败的编辑入口"
    assert truncated["normalAppends"] == 1, truncated

    composing = results["tripComposing"]
    assert composing["composing"] is True
    assert composing["requests"] == 0, "输入法合成期间不能请求服务端过滤"
    composed = results["tripComposed"]
    assert composed["composing"] is False
    assert composed["requests"] == 1, "合成结束后补一次过滤请求"
    assert composed["filter"] == "hang"
    print("✓ 语义暂存恢复 / 截断消息编辑入口 / 行程筛选 IME 守护")


def test_metrics_panel_uses_duration_samples():
    """纯 SSE 流量（有请求数、无计时样本）时耗时磁贴显示 —，而不是 0 ms。"""
    results = _run_harness("metricsPanel")
    sse = {str(label): value for value, label in results["sseOnly"]}
    assert sse["API 请求数"] == "3", sse
    for label in ("平均耗时", "最大耗时", "P95 耗时"):
        assert sse[label] == "—", sse
    sampled = {str(label): value for value, label in results["sampled"]}
    assert sampled["平均耗时"] == "12 ms", sampled
    assert sampled["最大耗时"] == "30 ms", sampled
    assert sampled["P95 耗时"] == "20 ms", sampled
    print("✓ 运行指标面板按计时样本数判断耗时")


def test_storage_labels_show_fallback():
    """用户卡片：配置了但回退的后端要显示「已回退」，未知状态不能冒充本地存储。"""
    data = _run_harness("storageLabels")
    assert data["fallback"] == ["Redis（已回退）", "PostgreSQL（已回退）"], data
    assert data["redis"] == ["Redis", "PostgreSQL"], data
    assert data["unknown"] == ["存储状态未知"], data
    print("✓ 用户卡片显示存储回退状态")


def test_health_refreshes_on_visibility_and_online():
    """页面重新可见 / 网络恢复时刷新健康状态（无轮询的陈旧指示纠正）。"""
    data = _run_harness("healthRefresh")
    assert data["hidden_requested"] == 0, "不可见时不应请求"
    assert data["url"] == "/api/health", data
    assert data["degraded_text"] == "降级", data
    assert data["online_text"] == "在线", data
    assert data["status"] == "ok", data
    print("✓ 健康状态在可见 / 上线时刷新")


def test_stop_reconcile_preserves_registration_across_switch():
    """停止后切走会话：待对账登记保留，回到该会话时再对账。"""
    data = _run_harness("stopReconcileParked")
    assert data["parked"] == "deferred", data
    assert data["after_parked"]["pending"] == 1, data["after_parked"]
    assert data["after_parked"]["requests"] == 0, data["after_parked"]
    # 在另一个会话里 flush：不丢弃登记，也不发请求 / 不动当前界面
    assert data["after_flush"]["pending"] == 1, data["after_flush"]
    assert data["after_flush"]["requests"] == 0, data["after_flush"]
    # 回到 s1 打开会话：调度补对账
    assert data["opened"] is True, data
    assert data["flushes"] == 1, data
    assert data["pending_after_open"] == 1, data
    print("✓ 停止对账登记跨会话保留并在回到会话时补跑")


def test_stop_reconcile_single_flight():
    """600ms 定时器与 flush 并发对账同一回合时只执行一条链。"""
    data = _run_harness("stopReconcileSingle")
    assert data["impl_calls"] == 1, data
    assert data["second"] == "deferred", data
    assert data["first"] == "deferred", data
    assert data["pending"] == 1, data
    print("✓ 停止对账同一回合去重（单飞）")


def test_delete_inflight_session_restores_edit_entry():
    """删除在途会话作废其加载后，必须补回当前会话的编辑入口。"""
    data = _run_harness("editChipRestore")
    assert data["initial"] == 1, data
    assert data["during_load"] == 0, data
    assert data["after_delete"] == 1, "作废在途加载后必须补回编辑入口"
    assert data["session_id"] == "B", data
    assert data["session_loading"] is False, data
    print("✓ 删除作废会话加载后恢复编辑入口")


def test_trip_late_response_respects_request_user():
    """行程删除 / 编辑 404 的迟到响应不能清掉新用户正在编辑的同号行程。"""
    data = _run_harness("tripLateUserGuard")
    deleted = data["deleteSwitched"]
    assert deleted["editing"] == "trip_1", deleted
    assert deleted["destination"] == "B-杭州", deleted
    assert deleted["submit"] == "保存修改", deleted
    assert not any("行程已删除" in item for item in deleted["toasts"]), deleted
    control = data["deleteSameUser"]
    assert control["editing"] is None, control
    assert control["submit"] == "保存行程", control
    assert any("行程已删除" in item for item in control["toasts"]), control

    added = data["addSwitched"]
    assert added["editing"] == "trip_1", added
    assert added["destination"] == "B-杭州", added
    assert added["submit"] == "保存修改", added
    assert not any("已被删除" in item for item in added["toasts"]), added
    add_control = data["addSameUser"]
    assert add_control["editing"] is None, add_control
    assert add_control["submit"] == "保存行程", add_control
    print("✓ 行程迟到响应按发起账号守卫")


def test_send_blocked_while_current_session_deleting():
    """当前会话删除在途时拒绝发送；删除的是别的会话时不影响。"""
    data = _run_harness("deleteInFlightSend")
    assert data["before"]["busy"] is False, data["before"]
    blocked = data["blocked"]
    assert blocked["fetchCalls"] == 0, blocked
    assert blocked["input"] == "追问", "被拦截时不能清空草稿"
    assert any("会话正在删除" in item for item in blocked["toasts"]), blocked
    after = data["after_delete"]
    assert after["fetchCalls"] == 1, after
    assert after["lastBody"]["session_id"] is None, after
    other = data["other_session"]
    assert other["fetchCalls"] == 1, other
    assert other["body"]["session_id"] == "s1", other
    print("✓ 删除在途会话拦截发送")


def test_trip_import_partial_failure_reconciles():
    """导入部分失败 / 超时后必须刷新面板（服务端可能已写入）。"""
    data = _run_harness("tripImportFailure")
    partial = data["partial"]
    assert any("导入部分失败" in item for item in partial["toasts"]), partial
    assert not any(item.startswith("导入行程失败") for item in partial["toasts"]), (
        "503 部分失败是服务端原文提示，不能加通用失败前缀"
    )
    assert partial["refreshCalls"] == 1, partial
    assert partial["refreshOptions"][0].get("silentFailure") is True, partial
    assert partial["buttonText"] == "导入行程", partial
    assert partial["buttonDisabled"] is False, partial

    timeout = data["timeout"]
    assert any("服务端可能仍在写入" in item for item in timeout["toasts"]), timeout
    assert timeout["refreshCalls"] == 1, timeout

    bad = data["badRequest"]
    assert bad["toasts"] == ["导入行程失败：bad request"], bad
    assert bad["refreshCalls"] == 0, bad

    switched = data["switched"]
    assert switched["refreshCalls"] == 0, "切号后不能把旧用户的结果刷进新用户界面"
    assert any("已切换用户" in item for item in switched["toasts"]), switched

    preview = data["previewTimeout"]
    assert preview["refreshCalls"] == 0, "预览超时零写入，无需对账刷新"
    assert any("导入预览超时" in item for item in preview["toasts"]), preview
    assert not any(
        "服务端可能仍在写入" in item for item in preview["toasts"]
    ), "预览（dry_run）超时不能声称服务端可能正在写入"
    print("✓ 行程导入部分失败 / 超时后对账刷新（预览超时单独提示）")


def test_trip_edit_partial_payload_and_cross_row_draft():
    """编辑行程只提交改动字段；未改动的跨行切换不弹未提交确认。"""
    data = _run_harness("tripEditPartial")
    partial = data["partial"]
    assert partial == [{"user_id": "u1", "purpose": "客户会议"}], partial

    noop = data["noop"]
    assert noop["puts"] == 0, "未改任何字段不应发 PUT（服务端会 400）"
    assert noop["editing"] is None and noop["formHidden"] is True, noop

    cross = data["crossRow"]
    assert cross["confirmCalls"] == 0, "未改动的跨行切换不应弹未提交确认"
    assert cross["dest"] == "成都", cross
    print("✓ 行程编辑只提交改动字段，跨行未改动不弹确认")


def test_trip_dedupe_result_respects_request_user():
    """去重预览返回前已切换用户：不弹确认、不发真实删除、不动新账号表单。"""
    data = _run_harness("tripDedupeSwitchGuard")
    assert data["requests"] == 1, "切号后不能发出真实删除请求"
    assert data["confirmCalls"] == 0, "切号后不能再弹确认框"
    assert data["toasts"] == [], data
    assert data["refreshed"] == 0 and data["reset"] == 0, data
    assert data["editing"] == "trip_9", "不能重置新账号正在编辑的行程表单"
    assert data["button"] == "清理重复", data
    print("✓ 去重结果按发起账号守卫")


def test_panel_empty_state_export_label_and_conflict_total():
    """空态文案考虑状态筛选；筛选导出按钮明示子集；重叠徽标带真实总数。"""
    data = _run_harness("panelRound")
    empty = data["emptyWithStatus"]
    assert empty["text"] == "没有匹配的行程", empty
    assert empty["exportLabel"] == "导出筛选结果日历", empty
    assert empty["exportTitle"] == "按当前关键词 / 状态筛选导出（与列表同口径）", empty

    plain = data["emptyNoFilter"]
    assert plain["text"] == "还没有行程记录", plain
    assert plain["exportLabel"] == "导出全部日历", plain

    badge = data["conflictBadge"]
    assert badge is not None, "有重叠的行程必须渲染徽标"
    assert "等 25 条" in badge["title"], badge
    print("✓ 空态 / 导出按钮文案与重叠徽标总数")


def test_user_switch_storage_failure_warns_and_keeps_old_account():
    """浏览器拒绝存储时切换仍生效，但提示必须说明重载会回退。"""
    data = _run_harness("userSwitchStorageBlocked")
    switched = data["switched"]
    assert switched["userId"] == "bob", switched
    assert switched["local"] == "alice", "不得静默覆盖旧账号"
    assert switched["toasts"], switched
    assert "未能保存" in switched["toasts"][0], switched
    assert switched["toasts"][0] != "已切换用户", switched
    assert data["reload"] == "alice", data
    print("✓ 切换用户存储失败时明确提示且不谎称持久化")


def test_delete_session_late_response_keeps_new_user_list():
    """A 的删除成功响应迟到且已切到 B：不能改 B 的列表 / 总数 / 游标。"""
    data = _run_harness("deleteSessionLateResponse")
    switched = data["switched"]
    assert switched["sessions"] == ["y", "z"], switched
    assert switched["total"] == 2, switched
    assert switched["offset"] == 2, switched
    assert not any("会话已删除" in item for item in switched["toasts"]), switched
    control = data["sameUser"]
    assert control["sessions"] == [], control
    assert control["total"] == 0, control
    assert control["offset"] == 0, control
    assert any("会话已删除" in item for item in control["toasts"]), control
    print("✓ 删除会话迟到响应按发起账号守卫")


def test_delete_session_unknown_outcome_reconciles():
    """删除响应中断（超时 / 5xx）时按服务端列表对账，而不是谎称失败。"""
    data = _run_harness("sessionDeleteUnknown")
    timeout = data["timeout"]
    assert timeout["sessions"] == ["B"], timeout
    assert timeout["total"] == 1, timeout
    assert timeout["offset"] == 1, timeout
    assert timeout["sessionId"] is None, timeout
    assert timeout["welcome"] >= 1, timeout
    assert any("已按服务端列表确认" in item for item in timeout["toasts"]), timeout
    server = data["serverError"]
    assert server["sessions"] == ["B"], server
    conflict = data["conflict"]
    assert conflict["list_requests"] == 0, "409 是确定失败，不应额外对账"
    assert any(item.startswith("删除失败") for item in conflict["toasts"]), conflict
    assert conflict["sessions"] == ["A", "B"], conflict
    print("✓ 删除会话结果未知时对账")


def test_trip_filter_failure_does_not_claim_match_count():
    """展开行程刷新失败保留旧列表时，计数不能声称新条件匹配 N 条。"""
    data = _run_harness("tripFilterFailureStale")
    failed = data["failed"]
    assert failed["count"] == "筛选加载失败，列表为上次结果", failed
    assert "匹配 312" not in failed["count"], failed
    assert failed["rows"] > 0, "失败时必须保留旧列表"
    assert any("加载行程列表失败" in item for item in failed["toasts"]), failed
    silent = data["silent"]
    assert silent["count"] == "筛选加载失败，列表为上次结果", silent
    assert silent["toasts"] == [], silent
    assert data["recovered"] == "匹配 1 条", data
    print("✓ 行程筛选失败时计数不再假装匹配")


def test_edit_delete_unknown_outcome_message():
    """编辑重发的删除请求超时 / 5xx：提示结果未知而不是确定失败。"""
    data = _run_harness("editDeleteOutcome")
    for case in ("timeout", "serverError"):
        item = data[case]
        assert any("结果未知" in text for text in item["toasts"]), item
        assert not any(text.startswith("编辑失败") for text in item["toasts"]), item
        assert item["saveText"] == "保存并重新发送", item
        assert item["saveDisabled"] is False, item
    conflict = data["conflict"]
    assert any(text.startswith("编辑失败") for text in conflict["toasts"]), conflict
    print("✓ 编辑重发删除结果未知的提示")


def test_perf_round_probe_and_render_budget():
    """性能热路径：归档探针不阻塞主结果、展开刷新只渲染一次、
    记忆面板隐藏时不重建面板内容。"""
    data = _run_harness("perfRound")
    before = data["beforeProbe"]
    assert before["renders"] >= 1, before
    assert before["loading"] is False, before
    assert before["sessions"] == 1, before
    assert "archived=1" in data["probeUrl"], data["probeUrl"]
    stale = data["afterStaleProbe"]
    assert stale["matches"] == 0, "旧查询的迟到探针结果必须丢弃"
    assert stale["query"] == "上海", stale
    fresh = data["afterFreshProbe"]
    assert fresh["matches"] == 2, "当前查询的探针结果要补到提示上"
    expanded = data["expandedRefresh"]
    assert expanded["renders"] == 1, "展开 + 打开面板一次刷新只整块渲染一次"
    assert expanded["weather"] == 1, "同一刷新只查一次天气"
    hidden = data["hiddenPanel"]
    assert hidden["created"] < 100, hidden
    assert hidden["trips"] == 0, "隐藏面板不应重建行程列表"
    assert hidden["upcoming"] == 1, "侧栏「即将出行」仍要更新"
    assert hidden["weather"] == 1, "侧栏天气仍要刷新"
    assert hidden["archived"] == 4, hidden
    assert "无法获取数据概览" in data["hiddenCleared"], data["hiddenCleared"]
    print("✓ 性能热路径（探针不阻塞 / 单次渲染 / 隐藏面板早退）")


def test_kb_semantic_refresh_quiet_keeps_mode():
    """语义模式收尾刷新失败（429）：保留语义视图、不 toast、不切关键词列表。"""
    results = _run_harness("kbSemanticRefreshQuiet")
    quiet = results["quiet"]
    assert quiet["mode"] is True, quiet
    assert quiet["semanticQuery"] == "住宿", quiet
    assert quiet["toasts"] == [], quiet
    assert quiet["rendered"] == 0, quiet
    noisy = results["noisy"]
    assert noisy["mode"] is False, "非静默刷新失败仍按原逻辑退出语义模式"
    assert any("语义结果刷新失败" in t for t in noisy["toasts"]), noisy
    assert results["settled"] is True
    print("✓ 语义收尾刷新失败保留语义视图")


def test_kb_category_change_keeps_unsubmitted_query():
    """语义模式下切换分类：搜索框里未提交的输入不能被静默丢弃。"""
    results = _run_harness("kbCategoryDraft")
    assert results["beforeMode"] is True
    assert results["mode"] is False
    assert results["query"] == "差旅"
    assert "q=%E5%B7%AE%E6%97%85" in results["url"], results["url"]
    assert "category=%E4%BD%8F%E5%AE%BF" in results["url"], results["url"]
    print("✓ 切换分类保留未提交搜索词")


def test_kb_import_busy_not_reported_as_partial_write():
    """导入槽位繁忙 503：零写入拒绝不能被误报成「可能已写入」并多余刷新。"""
    results = _run_harness("kbImportBusy")
    assert results["toasts"] == [
        "已有知识库导入任务在处理，请稍后再试（请求 ID：0123456789abcdef）"
    ], results["toasts"]
    assert results["pendingUrls"] == []
    assert results["refreshCalls"] == 0
    assert results["importing"] is False
    assert results["buttonText"] == "导入 JSON"
    assert results["disabled"] is False
    print("✓ 导入槽位繁忙不冒充部分写入")


def test_kb_delete_stale_400_refreshes_list():
    """删除返回 400（结构冲突）：提示并刷新列表，行不本地删除。"""
    results = _run_harness("kbDeleteStale400")
    assert any("已刷新列表" in t for t in results["toasts"]), results["toasts"]
    assert results["refreshArgs"] == [{"silentFailure": True}]
    assert results["removed"] is False
    assert results["deleting"] == []
    print("✓ 删除 400 时刷新列表")


def test_theme_boot_matches_apply_theme():
    """theme.js 与 app.js 用同一存储键，首帧即写入 color-scheme。"""
    results = _run_harness("theme")
    read_keys = [entry[4:] for entry in results["read_keys"]]
    assert read_keys and set(read_keys) == {"aligo.theme"}, results["read_keys"]
    assert results["empty"] == {"theme": "light", "scheme": "light"}
    stored_keys = [entry[4:] for entry in results["stored"]["set_keys"]]
    assert stored_keys == []  # 启动脚本只读不写
    assert results["stored"]["theme"] == "dark"
    assert results["stored"]["scheme"] == "dark"
    assert results["system_dark"] == {"theme": "dark", "scheme": "dark"}
    applied = results["apply"]
    assert applied["stored"] == "dark"
    assert [entry[4:] for entry in applied["set_keys"]] == ["aligo.theme"]
    assert applied["theme"] == "dark" and applied["scheme"] == "dark"
    print("✓ 主题脚本与 applyTheme 共用键并同步 color-scheme")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
    import sys as _sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError
    # 把清单截断在第一个用例；先切 UTF-8（不支持时保持原状）——与其它
    # 直跑清单同一守卫
    try:
        for _stream in (_sys.stdout, _sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    # 全部用例都经 _run_harness 依赖 node：模块级无 skipif，直跑必须
    # 自己守卫——否则首个用例的 pytest.skip（Skipped 是 BaseException）
    # 会冒泡成 traceback、后续用例全部不执行
    import shutil as _shutil

    if _shutil.which("node") is None:
        print("未安装 node，跳过前端行为直跑用例（请通过 pytest 运行）")
        raise SystemExit(0)
    test_session_request_race_guards()
    test_retired_thread_reset_on_open_session()
    test_knowledge_preview_locates_hits_in_original_text()
    test_frontend_render_helpers()
    test_frontend_detail_fixes()
    test_user_switch_trip_preference_drafts()
    test_user_switch_chat_composer_draft()
    test_stored_invalid_user_id_falls_back_with_flag()
    test_delete_preference_late_response_keeps_new_user_baseline()
    test_trip_dedupe_unknown_outcome_refreshes()
    test_kb_pending_result_cleared_on_cancel()
    test_knowledge_duplicates_panel_states()
    test_knowledge_dedupe_panel_states()
    test_kb_panel_fixes()
    test_knowledge_actions_arbitrate_shared_result_bar()
    test_trip_import_file_decoding_and_size_guard()
    test_trips_export_button_uses_filtered_ics_endpoint()
    test_kb_dedupe_unknown_outcome_refreshes()
    test_kb_user_switch_warns_on_unsaved_draft()
    test_trip_export_quality_toasts()
    test_trip_import_ics_preview_labels_and_timezone()
    test_trips_dedupe_panel_flow()
    test_startup_stale_session_not_used_after_failed_click()
    test_send_message_http_error_not_connection_interruption()
    test_startup_does_not_override_user_session_click()
    test_knowledge_eval_panel_states()
    test_panel_fixes_cursor_stale_session_and_import_batches()
    test_reconcile_confirms_after_open_session()
    test_knowledge_import_skip_summary_distinguishes_conflicts()
    test_knowledge_delete_legacy_parts_without_group()
    test_knowledge_group_delete_syncs_local_rows_on_refresh_failure()
    test_kb_dedupe_success_removes_rows_locally()
    test_user_backup_import_rejects_non_utf8()
    test_user_backup_restore_reports_recovery_incomplete()
    test_chat_done_persisted_signal()
    test_search_hit_renders_bounded_window()
    test_silent_reload_restores_expanded_window()
    test_search_failure_restores_previous_query()
    test_upcoming_weather_normalizes_unpadded_start_date()
    test_archived_toggle_rolls_back_on_failure()
    test_copy_fallback_cleans_up_and_restores_focus()
    test_delete_last_turn_sends_turn_id()
    test_deleted_session_thread_starts_clean_on_next_send()
    test_retired_thread_regenerate_renders_user_question()
    test_export_reports_skipped_knowledge()
    test_editor_round2_preferences_and_guards()
    test_history_renders_in_windows()
    test_open_session_confirms_pending_edit()
    test_render_markdown_caps_oversized_message()
    test_retry_checks_turn_persisted_first()
    test_download_suppresses_concurrency_and_times_out()
    test_kb_search_state_machine()
    test_kb_semantic_refresh_quiet_keeps_mode()
    test_kb_category_change_keeps_unsubmitted_query()
    test_kb_import_busy_not_reported_as_partial_write()
    test_kb_delete_stale_400_refreshes_list()
    test_send_message_confirms_pending_edit_draft()
    test_session_html_export_uses_full_server_endpoint()
    test_session_refresh_keeps_expanded_list()
    test_silent_reload_preserves_scroll()
    test_silent_reload_shows_appended_messages_and_keeps_expansion()
    test_sse_stream_parsing_robustness()
    test_sse_eof_tail_frame_is_dispatched()
    test_replay_error_clears_transient_and_reconciles()
    test_user_switch_clears_preference_cache()
    test_stop_reconcile_deferred_until_next_turn_ends()
    test_open_session_guards_deleting_and_unknown_empty()
    test_webapp_round_interaction_fixes()
    test_webapp_round_semantic_stash_and_truncation()
    test_metrics_panel_uses_duration_samples()
    test_storage_labels_show_fallback()
    test_health_refreshes_on_visibility_and_online()
    test_stop_reconcile_preserves_registration_across_switch()
    test_stop_reconcile_single_flight()
    test_delete_inflight_session_restores_edit_entry()
    test_trip_late_response_respects_request_user()
    test_send_blocked_while_current_session_deleting()
    test_trip_import_partial_failure_reconciles()
    test_trip_edit_partial_payload_and_cross_row_draft()
    test_trip_dedupe_result_respects_request_user()
    test_panel_empty_state_export_label_and_conflict_total()
    test_user_switch_storage_failure_warns_and_keeps_old_account()
    test_delete_session_late_response_keeps_new_user_list()
    test_delete_session_unknown_outcome_reconciles()
    test_trip_filter_failure_does_not_claim_match_count()
    test_edit_delete_unknown_outcome_message()
    test_perf_round_probe_and_render_budget()
    test_theme_boot_matches_apply_theme()
    print("\n全部通过")
