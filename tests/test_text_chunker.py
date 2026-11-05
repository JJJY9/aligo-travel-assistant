"""
文本切分（RAG Chunking）测试（无需 API Key、不访问网络）

覆盖：
1. 章节边界（一、二、三 / Q1:）会强制开启新块，不同主题不混块；
2. 超长段落按句子切分，块长度受控；
3. 所有原文内容都被保留（不丢字）。

运行：python tests/test_text_chunker.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.text_chunker import split_text


def test_sections_not_mixed():
    """「住宿标准」不应与「交通标准」混进同一个块"""
    text = (
        "二、交通标准\n\n"
        "1. 高铁/动车标准\n"
        "   - 允许预订：二等座及以下\n\n"
        "三、住宿标准\n\n"
        "1. 一线城市住宿标准\n"
        "   - 适用城市：北京、上海、广州、深圳\n"
        "   - 标准：不超过500元/晚\n"
    )
    chunks = split_text(text)
    lodging = [c for c in chunks if "住宿标准" in c]
    assert lodging, "应存在住宿标准块"
    assert any("500" in c for c in lodging), "住宿费用应出现在住宿相关块中"
    for c in chunks:
        if "住宿标准" in c and "500" in c:
            assert "高铁" not in c, "住宿块不应混入交通内容"
    print(f"✓ 章节边界切分正常（共 {len(chunks)} 块）")


def test_faq_questions_separated():
    """FAQ 的每个 Q 应独立成块"""
    text = (
        "Q1: 如何报销？\n"
        "A1: 通过系统提交。\n\n"
        "Q2: 可以带家属吗？\n"
        "A2: 不可以。\n"
    )
    chunks = split_text(text)
    assert any("如何报销" in c for c in chunks)
    assert any("带家属" in c for c in chunks)
    for c in chunks:
        assert not ("如何报销" in c and "带家属" in c), "两个问题不应混在同一块"
    print("✓ FAQ 题号边界正常")


def test_long_paragraph_split():
    """超长段落按句子切分，块长受控"""
    long_text = "。".join(f"这是第{i}句用于测试切分" for i in range(80)) + "。"
    chunks = split_text(long_text, max_chars=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    print(f"✓ 超长段落切分正常（{len(chunks)} 块）")


def test_content_preserved():
    """切分后内容不丢失（忽略空白差异）"""
    text = (
        "一、总则\n\n"
        "这是第一段内容。\n\n"
        "二、细则\n\n"
        + "。".join(f"细则第{i}条" for i in range(30)) + "。\n"
    )
    chunks = split_text(text, max_chars=120)
    joined = "".join(chunks)
    for token in ["一、总则", "这是第一段内容", "二、细则", "细则第0条", "细则第29条"]:
        assert token in joined, f"内容丢失: {token}"
    print("✓ 切分后内容完整保留")


def test_consecutive_titles_merge_into_body():
    """总标题 + 节标题的连续标题块要并入正文，不能单独成块。"""
    text = (
        "商旅常见问题解答（FAQ）\n\n"
        "一、出差申请相关\n\n"
        "Q1: 出差申请需要提前几天？\n"
        "一般提前 3 个工作日提交。\n"
    )
    chunks = split_text(text)
    assert len(chunks) == 1, chunks
    assert "出差申请需要提前几天" in chunks[0]
    assert "3 个工作日" in chunks[0]
    print("✓ 连续标题行并入正文，不再产生纯标题块")


def test_short_title_merged():
    """孤立的文档标题不应单独成块（会挤占检索名额）"""
    text = (
        "差旅费用报销规定\n\n"
        "一、报销时间要求\n"
        "   - 出差结束后，应在30个自然日内提交报销申请\n"
    )
    chunks = split_text(text)
    assert len(chunks) == 1
    assert chunks[0].startswith("差旅费用报销规定")
    assert "30个自然日" in chunks[0]
    print("✓ 孤立标题并入内容块")


if __name__ == "__main__":
    test_sections_not_mixed()
    test_faq_questions_separated()
    test_long_paragraph_split()
    test_content_preserved()
    test_consecutive_titles_merge_into_body()
    test_short_title_merged()
    print("\n全部通过")
