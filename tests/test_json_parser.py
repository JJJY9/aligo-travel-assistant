"""JSON 解析器边界测试（无需 API Key、不访问网络）。

覆盖：
1. 字符串内转义引号不干扰顶层对象扫描（_top_level_object_starts）；
2. iter_json_objects 空文本 / 无对象；
3. robust_first_json_object 找不到对象时报错；
4. robust_json_parse 空文本的 fallback 与报错；
5. json5 不可用（CI 环境未安装）时回退到 fallback；
6. extract_response_text 对「非 dict 对象、仅有 content 字符串」的兼容。

运行：python tests/test_json_parser.py
"""
import logging
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.json_parser import (
    extract_first_json_object,
    extract_response_text,
    iter_json_objects,
    robust_first_json_object,
    robust_json_parse,
)


def test_escaped_quotes_in_string_do_not_break_scan():
    text = '{"a": "x\\"y", "b": 1}'  # 值里带转义引号
    assert extract_first_json_object(text) == {"a": 'x"y', "b": 1}
    # 转义引号包裹的花括号仍在字符串内，不能当作顶层候选
    tricky = '{"msg": "say \\"{x}\\"", "ok": 1}'
    assert extract_first_json_object(tricky) == {"msg": 'say "{x}"', "ok": 1}
    print("✓ 字符串内转义引号不干扰顶层扫描")


def test_top_level_scan_skips_broken_prefix():
    """第一个候选对象损坏时，扫描要正确跳过转义引号与数组，命中后面的对象。"""
    text = '{oops} 说明 "带\\"引号" 还有 [1, 2] {"ok": 1}'
    assert extract_first_json_object(text) == {"ok": 1}
    with pytest.raises(ValueError, match="Empty text"):
        extract_first_json_object("")
    print("✓ 损坏前缀后的顶层对象扫描（转义引号 / 数组）")


def test_starts_with_json_array_detection():
    """顶层数组判定：数组 / 对象优先 / 前言小数组后跟对象 / 非 JSON 括号。"""
    from utils.json_parser import starts_with_json_array

    assert starts_with_json_array('[{"a": 1}]') is True
    assert starts_with_json_array('结果：[{"a": 1}]') is True
    assert starts_with_json_array('{"a": [1, 2]}') is False
    assert starts_with_json_array('示例 {"a": 1} 然后 [1, 2]') is False
    assert starts_with_json_array('引用标记 [1] 后面是 {"a": 1}') is False
    assert starts_with_json_array('说明 [注] 不是 JSON') is False
    print("✓ 顶层数组判定")


def test_iter_json_objects_empty_text():
    assert list(iter_json_objects("")) == []
    assert list(iter_json_objects("没有对象")) == []
    print("✓ iter_json_objects 空文本 / 无对象")


def test_robust_first_json_object_without_object():
    with pytest.raises(ValueError, match="No JSON"):
        robust_first_json_object("完全不是 JSON")
    print("✓ robust_first_json_object 无对象报错")


def test_robust_json_parse_empty_text():
    assert robust_json_parse("", fallback="fb") == "fb"
    with pytest.raises(ValueError, match="Empty text"):
        robust_json_parse("")
    print("✓ 空文本 fallback / 报错")


def test_robust_json_parse_without_json5(monkeypatch):
    """json5 不可用时宽松解析依次失败，最终走 fallback。"""
    monkeypatch.setitem(sys.modules, "json5", None)
    assert robust_json_parse('{"a": 1 "b": 2}', fallback={"fb": True}) == {"fb": True}
    print("✓ json5 缺失时回退 fallback")


def test_robust_json_parse_uses_json5_when_available():
    pytest.importorskip("json5")
    # 无引号键：标准 JSON 与顶层对象提取都失败，json5 可解析
    assert robust_json_parse('{foo: "bar", num: 1, ok: true}') == {
        "foo": "bar",
        "num": 1,
        "ok": True,
    }
    # json5 也解析不了时必须走 fallback，而不是抛出 json5 的异常
    assert robust_json_parse("{foo: }", fallback={"fb": True}) == {"fb": True}
    print("✓ json5 可用时的成功 / 失败回退")


def test_extract_response_text_object_with_content_string():
    class _Resp:
        content = '{"ok": true}'

    assert extract_response_text(_Resp()) == '{"ok": true}'
    print("✓ 非 dict 对象的 content 字符串")


def test_last_object_with_keys_repairs_trailing_comma():
    """正式结果带尾逗号时不能被严格扫描跳过（否则更早的示例对象被当结果）。"""
    from utils.json_parser import last_json_object_with_keys

    text = (
        '示例：{"preferences":[{"type":"hotel_brands","value":"汉庭"}],'
        '"has_preferences":true}\n正式：{"preferences":[{"type":"hotel_brands",'
        '"value":"如家","action":"append"}],"has_preferences":true,}'
    )
    selected = last_json_object_with_keys(text, ("preferences",))
    assert selected is not None
    assert selected["preferences"] == [
        {"type": "hotel_brands", "value": "如家", "action": "append"}
    ], selected
    print("✓ 尾逗号正式结果经修复后胜出（示例不被写库）")


def test_last_object_with_keys_refuses_unparsable_tail():
    """末尾结果被截断无法解析时返回 None（走错误降级，不回退到示例）。"""
    from utils.json_parser import last_json_object_with_keys

    text = (
        '示例：{"preferences":[{"type":"hotel_brands","value":"汉庭"}]}\n'
        '正式：{"preferences":[{"type":"hotel_brands","value":"如家"'
    )
    assert last_json_object_with_keys(text, ("preferences",)) is None
    print("✓ 截断的正式结果不回落为示例")


def test_last_object_with_keys_still_picks_last_valid():
    """无小瑕疵时行为不变：取最后一个含目标键的对象。"""
    from utils.json_parser import last_json_object_with_keys

    text = (
        '{"answer": "先回显", "preferences": [{"type": "a"}]}\n'
        '{"answer": "真实结果", "preferences": [{"type": "b"}]}'
    )
    selected = last_json_object_with_keys(text, ("preferences",))
    assert selected["answer"] == "真实结果", selected
    print("✓ 无瑕疵时仍取最后一个对象")


def test_strict_and_first_object_with_keys_helpers():
    """strict 版本把「末尾截断」与「没有含键对象」区分开；first 只兜底含键对象。"""
    from utils.json_parser import (
        first_json_object_with_keys,
        last_json_object_with_keys_strict,
    )

    truncated = (
        '示例：{"preferences":[{"type":"hotel_brands","value":"汉庭"}]}\n'
        '正式：{"preferences":[{"type":"hotel_brands","value":"如家"'
    )
    with pytest.raises(ValueError):
        last_json_object_with_keys_strict(truncated, ("preferences",))

    # 没有截断、也没有含目标键的完整对象：strict 返回 None（调用方可再兜底）
    text = '{"note": "x"} {"other": 1}'
    assert last_json_object_with_keys_strict(text, ("preferences",)) is None
    assert first_json_object_with_keys(text, ("other",)) == {"other": 1}
    assert first_json_object_with_keys(text, ("preferences",)) is None

    # 第一个含键对象优先（尾逗号等小瑕疵由修复路径兜底）
    echo_then = '{"preferences": [{"type": "a"}],} 说明 {"note": 1}'
    assert first_json_object_with_keys(echo_then, ("preferences",)) == {
        "preferences": [{"type": "a"}]
    }
    print("✓ strict / first 辅助函数（截断抛错、首个含键对象兜底）")


def test_truncation_detected_before_result_keys():
    """正式结果在写出任何结果键之前被截断：不能回退到 prompt 回显示例。"""
    from utils.json_parser import (
        last_json_object_with_keys,
        last_json_object_with_keys_strict,
    )

    example = (
        '{"origin": "北京", "destination": "北京", "start_date": "2026-02-27", '
        '"missing_info": [], "extracted_count": 7}'
    )
    text = example + '\n正式结果：{"summary": "下周一从北京去杭州'
    with pytest.raises(ValueError):
        last_json_object_with_keys_strict(text, ("destination", "missing_info"))
    assert last_json_object_with_keys(text, ("destination", "missing_info")) is None
    print("✓ 正式结果在写出键之前被截断也能识别（不回退示例）")


def test_result_with_raw_newlines_beats_echoed_example():
    """正式结果字符串含裸换行也能修复并选中，不回退到 prompt 示例。"""
    from utils.json_parser import last_json_object_with_keys_strict

    text = (
        '示例：{"origin": "北京", "destination": "北京", "start_date": "2026-02-27", "duration_days": 1}\n'
        '正式结果：{"origin": "上海", "destination": "杭州", "start_date": "2026-03-11", '
        '"summary": "3月11日出发\n3月18日返回", "missing_info": []}'
    )
    selected = last_json_object_with_keys_strict(text, ("destination",))
    assert selected is not None and selected["destination"] == "杭州", selected
    print("✓ 裸控制字符结果修复后胜出")


def test_unrepairable_result_does_not_fall_back_to_example():
    """含 NaN 的正式结果无法修复：严格扫描拒绝回退到示例对象。"""
    import pytest

    from utils.json_parser import last_json_object_with_keys_strict

    text = (
        '示例：{"destination": "北京", "start_date": "2026-02-27"}\n'
        '正式：{"destination": "杭州", "start_date": "2026-03-11", "score": NaN}'
    )
    with pytest.raises(ValueError):
        last_json_object_with_keys_strict(text, ("destination",))
    print("✓ 不可修复结果不回退示例")


def test_json_array_start_skips_prose_and_prefixed_brackets():
    """裸数组定位跳过前言方括号；引用标记前缀的数组可由键扫描找回。"""
    from utils.json_parser import first_json_array_with_keys, json_array_start

    prose = '提取到以下偏好[共1条]：\n[{"type": "hotel_brands", "value": "汉庭"}]'
    start = json_array_start(prose)
    assert start is not None, "前言里的 [共1条] 不能阻断数组定位"
    assert prose[start:] == '[{"type": "hotel_brands", "value": "汉庭"}]'
    prefixed = '参考[1]\n[{"type": "hotel_brands", "value": "如家"}]'
    assert json_array_start(prefixed) is None, "前言小数组后跟对象块按对象输出处理"
    items = first_json_array_with_keys(prefixed, ("type", "value"))
    assert items and items[0]["value"] == "如家"
    print("✓ 裸数组定位跳过前言方括号")


def test_balanced_explanation_after_result_is_not_truncation():
    """完整结果后的平衡说明片段（带键名）不再误判截断。"""
    from utils.json_parser import last_json_object_with_keys_strict

    full = (
        '{"destination": "杭州", "start_date": "2026-12-01", '
        '"extracted_count": 1}'
    )
    text = full + '\n返回 {"destination": 城市名} 字段'
    result = last_json_object_with_keys_strict(text, ("destination",))
    assert result == {
        "destination": "杭州",
        "start_date": "2026-12-01",
        "extracted_count": 1,
    }, result
    print("✓ 完整结果后的平衡说明片段不再误判截断")


def test_prefer_strong_keys_helper():
    """强键（itinerary/daily_plans）优先，弱键仅在无强键时兜底。"""
    import pytest as pytest_module

    from utils.json_parser import last_json_object_prefer_keys_strict

    full = '{"itinerary": {"title": "北京3日游", "daily_plans": [{"day": 1}]}}'
    weak = '{"duration": "3天", "note": "可调整"}'
    result = last_json_object_prefer_keys_strict(
        full + '\n备注：' + weak,
        ("itinerary", "daily_plans"),
        ("duration", "schedule", "title"),
    )
    assert result == {
        "itinerary": {"title": "北京3日游", "daily_plans": [{"day": 1}]}
    }, result

    bare = '{"title": "北京3日游", "duration": "3天", "daily_plans": []}'
    assert last_json_object_prefer_keys_strict(
        bare, ("itinerary", "daily_plans"), ("duration", "schedule", "title")
    ) == {"title": "北京3日游", "duration": "3天", "daily_plans": []}

    weak_only = '{"title": "故宫", "duration": "3天"}'
    assert last_json_object_prefer_keys_strict(
        weak_only, ("itinerary", "daily_plans"), ("duration", "schedule", "title")
    ) == {"title": "故宫", "duration": "3天"}

    with pytest_module.raises(ValueError):
        last_json_object_prefer_keys_strict(
            full + '\n正式：{"notes', ("itinerary",), ("title",)
        )
    print("✓ 强键优先、弱键回退、末尾未闭合仍判截断")


def test_parse_failure_log_is_single_line_and_short(caplog):
    """解析失败日志不能原样打印模型输出：换行可伪造日志行，全文会放大隐私面。"""
    model_output = (
        '{"a": 1,\n2026-11-12 09:00:00 ERROR webapp.server - FORGED LOG LINE'
        '\n"b": }'
    )
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError):
            robust_json_parse(model_output)
    messages = [
        record.getMessage()
        for record in caplog.records
        if "All JSON parsing attempts failed" in record.getMessage()
    ]
    assert messages, caplog.text
    message = messages[0]
    assert "\n" not in message and "\r" not in message, repr(message)
    assert chr(0x2028) not in message and chr(0x2029) not in message
    assert len(message) <= 260, len(message)
    print("✓ 解析失败日志单行且截断")


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_escaped_quotes_in_string_do_not_break_scan()
        test_top_level_scan_skips_broken_prefix()
        test_starts_with_json_array_detection()
        test_iter_json_objects_empty_text()
        test_robust_first_json_object_without_object()
        test_robust_json_parse_empty_text()
        test_robust_json_parse_without_json5(mp)
        test_robust_json_parse_uses_json5_when_available()
        test_extract_response_text_object_with_content_string()
        test_top_level_scan_skips_broken_prefix()
        test_last_object_with_keys_repairs_trailing_comma()
        test_last_object_with_keys_refuses_unparsable_tail()
        test_result_with_raw_newlines_beats_echoed_example()
        test_unrepairable_result_does_not_fall_back_to_example()
        test_json_array_start_skips_prose_and_prefixed_brackets()
        test_last_object_with_keys_still_picks_last_valid()
        test_strict_and_first_object_with_keys_helpers()
        test_truncation_detected_before_result_keys()
        test_balanced_explanation_after_result_is_not_truncation()
        test_prefer_strong_keys_helper()
    finally:
        mp.undo()
    print("\n全部通过")