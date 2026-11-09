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


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_escaped_quotes_in_string_do_not_break_scan()
        test_iter_json_objects_empty_text()
        test_robust_first_json_object_without_object()
        test_robust_json_parse_empty_text()
        test_robust_json_parse_without_json5(mp)
        test_robust_json_parse_uses_json5_when_available()
        test_extract_response_text_object_with_content_string()
    finally:
        mp.undo()
    print("\n全部通过")
