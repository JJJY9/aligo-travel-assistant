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
    # 字符串里的裸换行属于控制字符：第 2 步清理会先把它删掉（保留拼接后的文本）
    assert robust_json_parse('{"a": "line1\nline2"}') == {"a": "line1line2"}
    # 组合问题：单引号 + 尾部逗号需要连续两步修复
    assert robust_json_parse("{'a': 1, 'b': 2,}") == {"a": 1, "b": 2}
    print("✓ JSON 多级修复尝试")


def test_json_parse_no_json_without_fallback():
    """没有花括号且未提供 fallback 时抛「No JSON found」。"""
    try:
        robust_json_parse("完全不是 JSON")
    except ValueError as exc:
        assert "No JSON found" in str(exc)
    else:
        raise AssertionError("应抛 ValueError")


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


if __name__ == "__main__":
    test_json_parse_failure_raises_value_error()
    test_json_parse_fallback()
    test_json_parse_success_paths_unchanged()
    test_json_parse_empty_dict_and_prose_braces()
    test_json_parse_repair_attempts()
    test_json_parse_no_json_without_fallback()
    test_extract_response_text_shapes()
    test_extract_response_text_remaining_shapes()
    test_extract_response_text_object_text_block()
    test_robust_json_parse_fallback_after_braces()
    test_extract_first_json_object_ignores_prose_braces()
    test_extract_first_json_object_empty_text()
    test_increment_query_count_fresh_data()
    test_increment_query_count_legacy_data()
    test_delete_all_does_not_resurrect_data()
    print("\n全部通过")
