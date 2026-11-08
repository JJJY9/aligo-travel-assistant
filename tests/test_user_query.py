"""utils/user_query.extract_user_query 的输入形态与回退（无网络 / 无模型）。

覆盖：字符串直通、非 JSON 文本、JSON 解析失败、JSON 非对象、rewritten_query
缺失回退 user_query、dict / content block 列表 / 其它标量输入。
运行：python tests/test_user_query.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.user_query import extract_user_query


def test_plain_and_json_strings():
    assert extract_user_query("  杭州天气  ") == "  杭州天气  ", (
        "普通字符串必须原样返回（不做 strip，保留用户原文）"
    )
    assert extract_user_query('{"context": {"rewritten_query": "住宿标准"}}') == (
        "住宿标准"
    )
    assert extract_user_query('{"context": {"user_query": "出差申请"}}') == "出差申请"
    # 解析失败 / 非对象 JSON 按原文处理，不能丢问题
    assert extract_user_query("{不是 JSON") == "{不是 JSON"
    assert extract_user_query('["a", "b"]') == '["a", "b"]'
    # 合法对象但没有可用字段：返回原始 JSON 文本
    assert extract_user_query('{"other": 1}') == '{"other": 1}'
    # rewritten_query 为空串 / null 时回退 user_query
    assert extract_user_query(
        '{"context": {"rewritten_query": "", "user_query": "报销流程"}}'
    ) == "报销流程"
    assert extract_user_query(
        '{"context": {"rewritten_query": null, "user_query": "报销流程"}}'
    ) == "报销流程"
    print("✓ 字符串输入（JSON / 非 JSON / 回退）")


def test_dict_and_block_inputs():
    assert extract_user_query({"context": {"rewritten_query": "住宿标准"}}) == (
        "住宿标准"
    )
    # dict 没有可用字段时递归内层 content
    assert extract_user_query(
        {"content": [{"type": "text", "text": "杭州天气"}]}
    ) == "杭州天气"
    assert extract_user_query({"content": "你好"}) == "你好"
    # dict 内层也不是文本：返回空串（不能把整段结构 str 成检索词）
    assert extract_user_query({"content": {"nested": 1}}) == ""
    # content block 列表：字符串块与 {"text": ...} 块拼接，其它块忽略
    assert extract_user_query(
        ["前缀", {"type": "text", "text": "、正文"}, {"type": "image"}]
    ) == "前缀、正文"
    # 非字符串标量
    assert extract_user_query(42) == "42"
    assert extract_user_query(None) == ""
    print("✓ dict / content block / 标量输入")


def test_content_text_helper():
    """content block 拼接辅助函数的直接输入形态。"""
    from utils.user_query import _content_text

    assert _content_text("abc") == "abc"
    assert _content_text(42) == "42"
    assert _content_text(None) == ""
    assert _content_text(["a", {"text": "b"}, 3]) == "ab"
    print("✓ content block 文本拼接辅助")


if __name__ == "__main__":
    test_plain_and_json_strings()
    test_dict_and_block_inputs()
    test_content_text_helper()
    print("\n全部通过")
