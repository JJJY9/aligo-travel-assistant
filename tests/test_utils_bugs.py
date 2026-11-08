import pytest
"""
工具类回归测试（无需 API Key）

覆盖 Issue #6：
1. robust_json_parse 所有解析尝试失败时抛 ValueError（修复前抛 NameError），fallback 生效；
2. LongTermMemory.increment_query_count 在新建数据 / 旧版数据（无 total_queries 字段）上均可用。

运行：python tests/test_utils_bugs.py
"""
import json
import os
import sys
import tempfile
import uuid

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.long_term_memory import LongTermMemory
from utils.json_parser import robust_json_parse


def test_json_parse_failure_raises_value_error():
    """全部解析尝试失败时应抛 ValueError（而不是 NameError）"""
    try:
        robust_json_parse('{"a": 1 "b": 2}')  # 缺逗号，连 json5 也解析不了的非法 JSON
    except ValueError as e:
        assert "Last error" in str(e) or "Failed to parse" in str(e)
        print("✓ 全部失败时抛 ValueError")
        return
    except NameError as e:
        raise AssertionError(f"仍抛 NameError（Issue #6a 复现）: {e}")
    raise AssertionError("应抛 ValueError")


def test_json_parse_fallback():
    """提供 fallback 时返回 fallback"""
    value = robust_json_parse("完全不是 JSON", fallback={"ok": True})
    assert value == {"ok": True}
    print("✓ fallback 生效")


def test_json_parse_success_paths_unchanged():
    """正常解析路径不受影响"""
    assert robust_json_parse('{"a": 1}') == {"a": 1}
    assert robust_json_parse('```json\n{"b": 2}\n```') == {"b": 2}
    print("✓ 正常解析路径不受影响")


def test_json_parse_empty_dict_and_prose_braces():
    """空字典原样返回；前后说明文字带花括号时取第一个完整对象。"""
    assert robust_json_parse({}) == {}
    prose = '结果如下：{"a": 1, "b": [2, 3]}，如 {日期} 未提供可默认明天'
    assert robust_json_parse(prose) == {"a": 1, "b": [2, 3]}
    print("✓ 空字典与夹带花括号的说明文字")


def test_extract_response_text_shapes():
    """响应文本提取兼容 dict / 列表块 / 对象 / 其他类型。"""
    from utils.json_parser import extract_response_text

    assert extract_response_text(None) == ""
    assert extract_response_text("hello") == "hello"
    assert extract_response_text({"content": "abc"}) == "abc"
    assert (
        extract_response_text(
            {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        )
        == "ab"
    )
    assert extract_response_text({"content": None}) == ""
    assert extract_response_text({"content": 3}) == "3"

    class _Obj:
        text = "obj-text"

    assert extract_response_text(_Obj()) == "obj-text"

    class _ContentList:
        content = ["x", {"type": "text", "text": "y"}]

    assert extract_response_text(_ContentList()) == "xy"
    print("✓ 响应文本提取的各形态")


def test_increment_query_count_fresh_data():
    """新建数据上 increment_query_count 可用（修复前 KeyError）"""
    d = tempfile.mkdtemp(prefix="chailv_stats_")
    memory = LongTermMemory(f"stats-probe-{uuid.uuid4().hex[:8]}", d)
    memory.increment_query_count()
    memory.increment_query_count()
    assert memory.get_statistics().get("total_queries") == 2, "计数应为 2"
    print("✓ increment_query_count 正常（新建数据）")


def test_increment_query_count_legacy_data():
    """旧版数据（statistics 中无 total_queries）迁移后可用"""
    d = tempfile.mkdtemp(prefix="chailv_stats_")
    legacy_user_id = f"legacy-profile-{uuid.uuid4().hex[:8]}"
    legacy_path = os.path.join(d, f"{legacy_user_id}.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump({
            "user_id": legacy_user_id,
            "preferences": [],
            "chat_history": [],
            "trip_history": [],
            "statistics": {"total_trips": 0, "total_messages": 0, "frequent_destinations": {}},
        }, f)
    memory = LongTermMemory(legacy_user_id, d)
    memory.increment_query_count()
    assert memory.get_statistics()["total_queries"] == 1
    print("✓ 旧数据迁移后 increment_query_count 正常")


def test_extract_first_json_object_ignores_prose_braces():
    """说明文字 / 示例里的花括号不能截进 JSON（find/rfind 的旧缺陷）。"""
    from utils.json_parser import extract_first_json_object

    text = (
        '好的，结果如下：\n{"status": "ok", "agent_schedule": []}\n'
        "提示：如 {日期} 未提供可默认明天。"
    )
    assert extract_first_json_object(text) == {"status": "ok", "agent_schedule": []}
    assert extract_first_json_object("示例 {不是JSON} 然后：{\"a\": 1}") == {"a": 1}
    try:
        extract_first_json_object("没有对象")
    except ValueError:
        pass
    else:
        raise AssertionError("应抛 ValueError")
    print("✓ extract_first_json_object 忽略说明文字中的花括号")


def test_json_parse_repair_attempts():
    """各修复尝试：控制字符 / 单引号 / 尾部逗号 / 字符串内换行 / 组合问题。"""
    assert robust_json_parse('{"a": "bad\x01char"}') == {"a": "badchar"}
    assert robust_json_parse("{'a': 'b'}") == {"a": "b"}
    assert robust_json_parse('{"a": 1, "b": [2,],}') == {"a": 1, "b": [2]}
    # 尾逗号修复只处理字符串之外：字符串值里的 ",}"/",]" 必须原样保留，
    # 否则引用 JSON / 代码片段的正文字段会被静默改写
    assert robust_json_parse('{"a": "x,}", "b": [1,],}') == {
        "a": "x,}",
        "b": [1],
    }
    assert robust_json_parse('{"note": "use [1,]", "b": [1,],}') == {
        "note": "use [1,]",
        "b": [1],
    }
    assert robust_json_parse('{"code": "if (a, b) { return, }", "b": 1,}') == {
        "code": "if (a, b) { return, }",
        "b": 1,
    }
    # 字符串里的裸换行属于控制字符：第 2 步清理会先把它删掉（保留拼接后的文本）
    assert robust_json_parse('{"a": "line1\nline2"}') == {"a": "line1line2"}
    # 组合问题：单引号 + 尾部逗号需要连续两步修复
    assert robust_json_parse("{'a': 1, 'b': 2,}") == {"a": 1, "b": 2}
    print("✓ JSON 多级修复尝试（字符串内的 ,} / ,] 保持原样）")


def test_strip_trailing_commas_preserves_escaped_quoted_strings():
    """剥离器跟踪转义状态：两个转义引号之间的 ,} 原样保留，字符串外尾逗号删除。"""
    from utils.json_parser import _strip_trailing_commas

    raw = '{"a": "say \\" ,} x \\" end", "b": [1,],}'
    stripped = _strip_trailing_commas(raw)
    assert stripped == '{"a": "say \\" ,} x \\" end", "b": [1]}'
    assert robust_json_parse(raw) == {"a": 'say " ,} x " end', "b": [1]}
    print("✓ 尾逗号剥离保留转义引号之间的 ,} 内容")


def test_json_parse_no_json_without_fallback():
    """没有花括号且未提供 fallback 时抛「No JSON found」。"""
    try:
        robust_json_parse("完全不是 JSON")
    except ValueError as exc:
        assert "No JSON found" in str(exc)
    else:
        raise AssertionError("应抛 ValueError")


def test_extract_first_json_object_escape_state_machine():
    """字符串里的转义引号 / 花括号不能干扰对象边界识别。"""
    from utils.json_parser import extract_first_json_object

    bs = chr(92)
    text = (
        "前缀 {"
        + chr(34) + "a" + chr(34) + ": " + chr(34)
        + "含 } 与 " + bs + chr(34) + " 引号" + chr(34)
        + ", " + chr(34) + "b" + chr(34) + ": {"
        + chr(34) + "c" + chr(34) + ": 1}} 后缀"
    )
    obj = extract_first_json_object(text)
    assert obj == {"a": "含 } 与 " + chr(34) + " 引号", "b": {"c": 1}}

    # 未闭合字符串里的 { 不能开启新对象：状态机保持字符串态直到结尾，
    # 不把后面的片段误当作可解析对象（整体报「未找到」）
    text2 = (
        "{" + chr(34) + "a" + chr(34) + ": "
        + chr(34) + "未闭合 { 这里" + chr(10)
        + "真正的 {" + chr(34) + "ok" + chr(34) + ": 1}"
    )
    with pytest.raises(ValueError, match="No JSON object"):
        extract_first_json_object(text2)
    print("✓ JSON 对象提取的转义 / 未闭合字符串状态机")


def test_extract_first_json_object_empty_text():
    from utils.json_parser import extract_first_json_object

    try:
        extract_first_json_object("")
    except ValueError:
        pass
    else:
        raise AssertionError("空文本应抛 ValueError")


def test_extract_response_text_remaining_shapes():
    """对象形态的 content 为数字 / 无属性对象兜底。"""
    from utils.json_parser import extract_response_text

    class _NumericContent:
        content = 7

    assert extract_response_text(_NumericContent()) == "7"

    class _Plain:
        def __str__(self):
            return "plain"

    assert extract_response_text(_Plain()) == "plain"
    print("✓ 响应文本提取的对象兜底形态")


def test_extract_response_text_object_text_block():
    """非 dict 对象形态的文本块（type/text 属性）也参与拼接。"""
    from utils.json_parser import extract_response_text

    class _TextBlock:
        type = "text"
        text = "块"

    class _Other:
        type = "image"

    response = {"content": [_TextBlock(), {"type": "image"}, _Other()]}
    assert extract_response_text(response) == "块"

    class _ObjContentList:
        content = [_TextBlock()]

    assert extract_response_text(_ObjContentList()) == "块"


def test_robust_json_parse_fallback_after_braces():
    """有花括号但所有解析尝试都失败时，fallback 生效。"""
    assert (
        robust_json_parse("示例 {不是JSON} 结束", fallback={"ok": True})
        == {"ok": True}
    )


def test_delete_all_does_not_resurrect_data():
    """delete_all 之后同一实例的写入不能把已删除的数据写回。"""
    d = tempfile.mkdtemp(prefix="chailv_delete_")
    user = f"delete-all-{uuid.uuid4().hex[:8]}"
    memory = LongTermMemory(user, d)
    memory.save_preference("home_location", "北京")
    memory.add_chat_message("user", "hi", "s1")
    memory.delete_all()
    assert memory.get_preference() == {}
    assert memory.get_chat_history() == []

    memory.save_preference("hotel_brands", ["汉庭"])
    fresh = LongTermMemory(user, d)
    assert fresh.get_preference() == {"hotel_brands": ["汉庭"]}
    assert fresh.get_chat_history() == []
    print("✓ delete_all 后数据不会被后续写入复活")


def test_extract_first_json_object_rejects_nested_fragments():
    """嵌套在畸形外层对象 / 数组里的子对象不能当作完整结果。"""
    from utils.json_parser import extract_first_json_object

    malformed_outer = '{"preferences": [{"type": "x", "value": "y"}], "ok": true,}'
    try:
        extract_first_json_object(malformed_outer)
    except ValueError:
        pass
    else:
        raise AssertionError("尾逗号外层对象的子片段不应被返回")

    try:
        extract_first_json_object('[{"origin": "北京"}, {"origin": "上海"}]')
    except ValueError:
        pass
    else:
        raise AssertionError("数组里的元素不应被当作完整对象")

    # 说明文字里的成对花括号之后，合法对象仍能取到
    prose = '分析：{不是JSON}，最终：{"ok": 1}'
    assert extract_first_json_object(prose) == {"ok": 1}
    print("✓ 首个对象扫描拒绝嵌套片段")


def test_extract_first_json_object_skips_nan_candidate():
    """NaN 候选只跳过当前对象，不能中止扫描（后面还有合法结果）。"""
    from utils.json_parser import extract_first_json_object

    text = '推理：{"score": NaN}\n最终：{"has_preferences": true}'
    assert extract_first_json_object(text) == {"has_preferences": True}
    print("✓ NaN 候选被跳过而不是中止扫描")


def test_robust_json_parse_rejects_nan():
    """robust_json_parse 与其它入口一致：NaN / Infinity 不是合法数值。"""
    from utils.json_parser import robust_json_parse

    try:
        robust_json_parse('{"a": NaN}')
    except ValueError:
        pass
    else:
        raise AssertionError("NaN 应解析失败")
    try:
        robust_json_parse('{"a": Infinity}')
    except ValueError:
        pass
    else:
        raise AssertionError("Infinity 应解析失败")
    print("✓ robust_json_parse 拒绝 NaN / Infinity")


def test_merge_preference_value_empty_list():
    """append 空列表返回 []，不能产生嵌套列表 [[]]。"""
    from utils.preference_values import merge_preference_value

    assert merge_preference_value(None, []) == []
    assert merge_preference_value([], []) == []
    assert merge_preference_value(["东航"], []) == ["东航"]
    assert merge_preference_value(None, ["国航"]) == ["国航"]
    assert merge_preference_value(["东航"], ["国航"]) == ["东航", "国航"]
    assert merge_preference_value(None, 0) == [0], "0 是合法偏好"
    print("✓ 空列表 append 不产生嵌套列表")


def test_preference_blank_items_filtered():
    """模型输出的 null / 空白项不能绕过门禁落库，也不能混进合并结果。"""
    from utils.preference_values import has_preference_value, merge_preference_value

    assert not has_preference_value([None])
    assert not has_preference_value([""])
    assert not has_preference_value(["  "])
    assert not has_preference_value("   ")
    assert has_preference_value(["东航", None])
    assert has_preference_value(0) and has_preference_value(False)
    assert merge_preference_value(["东航"], [None]) == ["东航"]
    assert merge_preference_value(None, [""]) == []
    assert merge_preference_value(["东航", None], ["国航"]) == ["东航", "国航"]
    print("✓ 偏好空值门禁与合并过滤（null / 空白串）")


def test_append_preference_empty_list_not_persisted_as_nested():
    d = tempfile.mkdtemp(prefix="chailv_pref_empty_")
    memory = LongTermMemory(f"pref-empty-{uuid.uuid4().hex[:8]}", d)
    assert memory.append_preference("airlines", []) == []
    assert memory.get_preference().get("airlines") in (None, [])
    merged = memory.append_preference("airlines", "国航")
    assert merged == ["国航"]
    print("✓ 空列表 append 不落库为嵌套列表")


def test_extract_user_query_handles_structured_inputs():
    """dict / content block 列表输入不再变成 Python repr。"""
    from utils.user_query import extract_user_query

    assert (
        extract_user_query({"context": {"rewritten_query": "住宿标准"}})
        == "住宿标准"
    )
    assert (
        extract_user_query({"content": [{"type": "text", "text": "杭州天气"}]})
        == "杭州天气"
    )
    assert (
        extract_user_query([{"type": "text", "text": "报销流程"}]) == "报销流程"
    )
    assert extract_user_query({"context": {"user_query": "出差申请"}}) == "出差申请"
    print("✓ 结构化用户提问输入归一为纯文本")


def test_robust_json_parse_smart_escaping_control_chars():
    # 字符串值里出现裸换行 / 裸制表符（非法 JSON）：清洗尝试会移除控制字符后
    # 成功解析；合法的转义反斜杠（\\）必须原样保留
    text = '{"reply": "第一行\n第二行", "note": "A\tB", "path": "x\\\\y"}'
    result = robust_json_parse(text)
    assert result["reply"] == "第一行第二行"
    assert result["note"] == "AB"
    assert result["path"] == "x\\y"
    print("✓ 裸控制字符的清洗修复与转义反斜杠保真")


def test_robust_json_parse_fenced_inputs():
    assert robust_json_parse('```json\n{"a": 1}\n```') == {"a": 1}
    assert robust_json_parse('```\n{"b": 2}\n```') == {"b": 2}
    print("✓ Markdown 代码块包裹的 JSON")


def test_extract_response_text_object_attribute():
    from utils.json_parser import extract_response_text

    class _Obj:
        text = "对象文本"

    assert extract_response_text(_Obj()) == "对象文本"
    print("✓ extract_response_text 读取对象 text 属性")


def test_sanitize_json_escapes_surrogates():
    """孤立代理项与 NaN 都归一：响应序列化不再 UnicodeEncodeError。"""
    from utils.json_safe import sanitize_json

    cleaned = sanitize_json(
        {"a": "x\ud800y", "b": [float("nan")], "k\ud800": 1}
    )
    dumped = json.dumps(cleaned)
    assert "ud800" in dumped.lower()
    assert cleaned["b"] == [None]
    assert cleaned["a"] != "x\ud800y", "孤立代理项必须被转义"
    print("✓ sanitize_json 转义孤立代理项并归一 NaN")


def test_robust_first_json_object_repairs_small_faults():
    """尾逗号等小瑕疵不能整段放弃：修复后仍取出首个对象。"""
    from utils.json_parser import robust_first_json_object

    assert robust_first_json_object('前缀 {"a": 1,} 后缀') == {"a": 1}
    assert robust_first_json_object('{"a": [1, 2,], "b": 3}') == {
        "a": [1, 2],
        "b": 3,
    }
    with pytest.raises(ValueError):
        robust_first_json_object("没有 JSON")
    print("✓ 首个 JSON 对象修复解析（尾逗号）")


if __name__ == "__main__":
    test_json_parse_failure_raises_value_error()
    test_sanitize_json_escapes_surrogates()
    test_robust_first_json_object_repairs_small_faults()
    test_json_parse_fallback()
    test_json_parse_success_paths_unchanged()
    test_json_parse_empty_dict_and_prose_braces()
    test_json_parse_repair_attempts()
    test_strip_trailing_commas_preserves_escaped_quoted_strings()
    test_json_parse_no_json_without_fallback()
    test_extract_response_text_shapes()
    test_extract_response_text_remaining_shapes()
    test_extract_response_text_object_text_block()
    test_robust_json_parse_fallback_after_braces()
    test_extract_first_json_object_ignores_prose_braces()
    test_extract_first_json_object_empty_text()
    test_extract_first_json_object_rejects_nested_fragments()
    test_extract_first_json_object_skips_nan_candidate()
    test_robust_json_parse_rejects_nan()
    test_robust_json_parse_smart_escaping_control_chars()
    test_robust_json_parse_fenced_inputs()
    test_extract_response_text_object_attribute()
    test_merge_preference_value_empty_list()
    test_preference_blank_items_filtered()
    test_append_preference_empty_list_not_persisted_as_nested()
    test_extract_user_query_handles_structured_inputs()
    test_increment_query_count_fresh_data()
    test_increment_query_count_legacy_data()
    test_delete_all_does_not_resurrect_data()
    print("\n全部通过")
