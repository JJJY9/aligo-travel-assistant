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


def test_title_only_run_respects_max_chars():
    """连续几十行目录式标题累计后必须受 max_chars 约束，不能整块超长。"""
    text = "\n\n".join(f"目录标题{i}" for i in range(60)) + "\n\n正文内容。"
    chunks = split_text(text, max_chars=100)
    assert all(len(c) <= 100 for c in chunks), max(len(c) for c in chunks)
    joined = "".join(chunks)
    assert "目录标题0" in joined and "目录标题59" in joined
    assert "正文内容" in joined
    print(f"✓ 连续标题块受上限约束（{len(chunks)} 块）")


def test_heading_attached_to_long_body():
    """标题后跟超长正文时，标题应并入第一段正文，不能单独成块。"""
    text = "一、住宿标准\n\n" + "住宿标准为500元。" * 100
    chunks = split_text(text)
    assert chunks[0].startswith("一、住宿标准"), chunks[0][:30]
    assert "500" in chunks[0], "标题应与正文首段在同一块"
    # 标题（≤30 字）并入首段，允许首块超出上限至多一个标题长度
    assert len(chunks[0]) <= 430
    assert all(len(c) <= 400 for c in chunks[1:])
    print("✓ 标题并入超长正文首段")


def test_heading_attached_to_unpunctuated_body():
    """标题后是无句读的长段落（hard-split）时，标题仍并入第一块。"""
    chunks = split_text("一、住宿标准\n\n" + "住" * 900 + "。")
    assert chunks[0].startswith("一、住宿标准"), chunks[0][:20]
    assert "住" * 100 in chunks[0], "标题块必须包含正文内容"
    assert len(chunks[0]) <= 430
    assert all(len(chunk) <= 400 for chunk in chunks[1:])
    print("✓ 标题并入无句读长段落首块")


def test_empty_and_whitespace_text():
    assert split_text("") == []
    assert split_text("   \n\n  ") == []


def test_unpunctuated_sentence_hard_split():
    """没有句读的超长单句按硬切分，长度受控且不丢字。"""
    chunks = split_text("住" * 950, max_chars=100)
    assert len(chunks) >= 9
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == "住" * 950


def test_title_only_text_becomes_single_chunk():
    """整篇都是标题行时，合并结果仍是一块且内容完整。"""
    chunks = split_text("商务差旅\n\n报销规定\n\n住宿标准")
    assert len(chunks) == 1
    joined = chunks[0]
    for token in ("商务差旅", "报销规定", "住宿标准"):
        assert token in joined


def test_numbered_colon_headings_merged_with_body():
    """带编号 / 结尾冒号的短标题不是正文：要并入下一段，不能成为纯标题块。"""
    from utils.text_chunker import _is_title_like, split_text

    assert _is_title_like("1. 一线城市住宿标准")
    assert _is_title_like("3. 住宿标准：")
    assert not _is_title_like("允许预订：二等座及以下")

    chunks = split_text("1. 住宿标准：\n\n" + "正文内容" * 40 + "\n")
    assert chunks
    assert all("住宿标准" in chunk or "正文" in chunk for chunk in chunks)
    assert chunks[0].startswith("1. 住宿标准："), "标题应并入正文块而不是独立成块"
    assert "正文内容" in chunks[0]
    print("✓ 编号 / 冒号标题并入正文")


if __name__ == "__main__":
    test_sections_not_mixed()
    test_faq_questions_separated()
    test_long_paragraph_split()
    test_content_preserved()
    test_consecutive_titles_merge_into_body()
    test_short_title_merged()
    test_title_only_run_respects_max_chars()
    test_heading_attached_to_long_body()
    test_heading_attached_to_unpunctuated_body()
    test_empty_and_whitespace_text()
    test_unpunctuated_sentence_hard_split()
    test_title_only_text_becomes_single_chunk()
    test_numbered_colon_headings_merged_with_body()
    print("\n全部通过")
