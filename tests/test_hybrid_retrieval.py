"""混合检索工具测试（纯 Python，无需 Milvus / 嵌入模型）。

覆盖：中文二元组切词、BM25 排序与零分过滤、RRF 融合、混合相关性标注。

运行：python tests/test_hybrid_retrieval.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.hybrid_retrieval import (
    BM25Index,
    annotate_hybrid_scores,
    rrf_fuse,
    tokenize,
)


def test_tokenize_chinese_bigrams_and_ascii_words():
    """中文按二元组切词，英文 / 数字按词切，大小写归一。"""
    tokens = tokenize("住宿标准 600 元/晚")
    assert "600" in tokens
    assert "住宿" in tokens and "宿标" in tokens and "标准" in tokens
    assert tokenize("WiFi 免费")[0] == "wifi"
    assert "住" in tokenize("住"), "单字中文保留"
    print("✓ tokenize 中文二元组 + 英文/数字")


def test_bm25_ranks_keyword_match():
    """关键词命中的片段排在前面，零分片段不返回。"""
    docs = [
        {"id": 1, "content": "住宿标准：一线城市每晚 600 元"},
        {"id": 2, "content": "市内交通标准：每天 100 元"},
        {"id": 3, "content": "差旅报销需要提供发票"},
    ]
    index = BM25Index(docs)
    hits = index.search("住宿标准", top_k=3)
    assert hits, "关键词命中应有结果"
    assert hits[0][0]["id"] == 1
    assert all(score > 0 for _, score in hits)
    assert index.search("完全无关的词汇", top_k=3) == []
    print("✓ BM25 关键词排序与零分过滤")


def test_rrf_prefers_documents_in_both_lists():
    """两路都命中的文档 RRF 分数最高，单路文档也保留。"""
    vector = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    bm25 = [{"id": "b"}, {"id": "d"}]
    fused = rrf_fuse([vector, bm25], k=60)
    assert [doc["id"] for doc in fused[:2]] == ["b", "a"]
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"]
    assert {doc["id"] for doc in fused} == {"a", "b", "c", "d"}
    print("✓ RRF 融合：双路命中优先")


def test_annotate_hybrid_scores_uses_max_of_vector_and_normalized_bm25():
    """distance = max(向量余弦, 归一化 BM25)，并保留可解释的原始分数。"""
    fused = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    annotated = annotate_hybrid_scores(
        fused,
        vector_distances={"a": 0.62},
        bm25_scores={"b": 10.0, "c": 5.0},
    )
    by_id = {doc["id"]: doc for doc in annotated}
    assert by_id["a"]["distance"] == 0.62, "向量分高时保留余弦"
    assert by_id["b"]["distance"] == 1.0, "BM25 最高分归一为 1"
    assert by_id["c"]["distance"] == 0.5
    assert by_id["b"]["vector_distance"] == 0.0
    print("✓ 混合相关性标注")


def test_bm25_query_reference_uses_unique_query_terms():
    """归一化基准是查询词 IDF 之和；重复词不抬高基准。"""
    index = BM25Index(
        [
            {"id": 1, "content": "住宿标准：一线城市每晚 600 元"},
            {"id": 2, "content": "市内交通标准：每天 100 元"},
        ]
    )
    reference = index.query_reference("住宿标准")
    assert reference > 0
    repeated = index.query_reference("住宿标准 住宿标准")
    assert abs(repeated - reference) < 1e-9, "重复查询词不能重复累计 IDF"
    print("✓ BM25 归一化基准按唯一查询词")


def test_unseen_query_terms_do_not_collapse_reference():
    """内容词全部 OOV 时，命中唯一在库疑问词的片段不能接近满分。"""
    docs = [
        {"id": 1, "content": "出差申请被驳回怎么办：联系行政重新提交"},
        {"id": 2, "content": "航班延误怎么办：联系航司改签"},
        {"id": 3, "content": "住宿标准：一线城市每晚 600 元"},
    ]
    index = BM25Index(docs)
    normalized = index.normalized_score("结婚请假怎么办", docs[0]["content"])
    assert normalized < 0.5, normalized
    # 在库查询词仍可正常拿到高分
    assert index.normalized_score("住宿标准", docs[2]["content"]) > 0.9
    print("✓ OOV 内容词不坍缩归一化基准")


def test_normalized_score_is_length_neutral():
    """覆盖全部查询词的长片段不因长度惩罚跌破阈值，短片段不因长度抬分。"""
    filler = {"id": 100, "content": "统计报表"}
    docs = [
        {"id": 1, "content": "住宿"},
        {"id": 2, "content": "住宿和发票报销说明" + "统计报表" * 80},
    ] + [dict(filler, id=100 + i) for i in range(20)]
    index = BM25Index(docs)
    long_score = index.normalized_score("住宿发票", docs[1]["content"])
    short_score = index.normalized_score("住宿发票", docs[0]["content"])
    assert long_score > 0.5, long_score
    assert short_score < 0.5, short_score
    assert long_score > short_score, (long_score, short_score)
    print("✓ 归一化对长度中性（长片段不误拒、短片段不误放）")


def test_single_token_query_does_not_score_full_marks():
    """单 token 查询：含该词的片段不能恒得 1.0（否则 min_score 完全失效）。"""
    docs = [
        {"id": "1", "content": "出差住宿标准与报销流程"},
        {"id": "2", "content": "其他无关内容"},
    ]
    index = BM25Index(docs)
    score = index.normalized_score("出差", docs[0]["content"])
    assert 0.0 < score < 0.75, score


def test_partial_coverage_cannot_reach_full_marks_by_tf_saturation():
    """只命中一部分查询词时，重复该词不能把归一化分抬到满分。"""
    docs = [
        {"id": "1", "content": "发票发票发票发票发票发票发票发票"},
        {"id": "2", "content": "审批单与增值税发票：报销需要准备哪些材料"},
    ]
    index = BM25Index(docs)
    query = "报销需要准备哪些发票材料"
    saturated = index.normalized_score(query, docs[0]["content"])
    full = index.normalized_score(query, docs[1]["content"])
    assert saturated < 0.9, saturated
    assert full > saturated, (full, saturated)


def test_repeated_query_terms_do_not_inflate_bm25():
    """重复查询词不能把 BM25 原始分翻倍（否则归一后截断成 1.0）。"""
    index = BM25Index([{"id": 1, "content": "住宿标准每晚 600 元"}])
    single = index.search("住宿", 5)
    repeated = index.search("住宿住宿", 5)
    assert single and repeated
    assert abs(single[0][1] - repeated[0][1]) < 1e-9
    print("✓ 重复查询词不放大 BM25 分")


def test_annotate_uses_per_document_normalized_scores():
    """提供按文档长度归一的分值时，distance 直接采用它。"""
    fused = [{"id": "a"}, {"id": "b"}]
    annotated = annotate_hybrid_scores(
        fused,
        {},
        {"a": 10.0, "b": 1.0},
        bm25_normalized={"a": 0.2, "b": 0.9},
    )
    by_id = {doc["id"]: doc for doc in annotated}
    assert abs(by_id["a"]["distance"] - 0.2) < 1e-9
    assert abs(by_id["b"]["distance"] - 0.9) < 1e-9
    print("✓ 混合标注优先使用按文档长度归一的分值")


def test_annotate_with_query_reference_does_not_auto_pass_top_hit():
    """按查询覆盖面归一：弱命中 top-1 不再自动变成 1.0 绕过阈值。"""
    fused = [{"id": "a"}]
    weak = annotate_hybrid_scores(
        fused, {}, {"a": 10.0}, bm25_reference=100.0
    )
    assert abs(weak[0]["distance"] - 0.1) < 1e-9, weak[0]
    strong = annotate_hybrid_scores(
        fused, {}, {"a": 10.0}, bm25_reference=5.0
    )
    assert strong[0]["distance"] == 1.0, "超过基准时截断为 1"
    print("✓ 混合标注按查询覆盖面归一")


def test_cross_word_bigrams_do_not_dilute_keyword_evidence():
    """跨词二元组（浮怎 / 么坐）不计入覆盖度分母：真实命中不再被稀释。"""
    docs = [
        {"id": "1", "content": "磁悬浮（8分钟到龙阳路）"},
        {"id": "2", "content": "高铁与飞机是常见出差交通方式"},
        {"id": "3", "content": "一线城市住宿标准为每晚 500 元"},
        # 填充片段把库规模抬到 10：df=1 的目标词 IDF 高于 unseen/2，
        # 与内置库（141 分片）的稀有词量级一致
        {"id": "4", "content": "报销需要发票与行程单，怎么办理见指南"},
        {"id": "5", "content": "出差审批流程由直属领导确认"},
        {"id": "6", "content": "酒店预订优先协议价"},
        {"id": "7", "content": "机场贵宾厅使用规则"},
        {"id": "8", "content": "火车票改签手续费说明"},
        {"id": "9", "content": "差旅保险覆盖范围"},
        {"id": "10", "content": "国际航班行李额度"},
    ]
    index = BM25Index(docs)
    score = index.normalized_score("磁悬浮怎么坐", docs[0]["content"])
    decoy = index.normalized_score("磁悬浮怎么坐", docs[1]["content"])
    assert score > 0.5, score
    assert decoy < 0.5
    assert score > decoy
    # 与在库查询词不共享字符的真实 OOV 内容词仍按 unseen IDF 计入
    # （「结婚请假」的内容词都不在库）：防坍缩语义保持不变
    assert index.normalized_score("结婚请假流程", docs[0]["content"]) < 0.1
    print("✓ 跨词二元组不再稀释 BM25 证据分")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_tokenize_chinese_bigrams_and_ascii_words()
    test_bm25_ranks_keyword_match()
    test_rrf_prefers_documents_in_both_lists()
    test_annotate_hybrid_scores_uses_max_of_vector_and_normalized_bm25()
    test_bm25_query_reference_uses_unique_query_terms()
    test_unseen_query_terms_do_not_collapse_reference()
    test_normalized_score_is_length_neutral()
    test_single_token_query_does_not_score_full_marks()
    test_partial_coverage_cannot_reach_full_marks_by_tf_saturation()
    test_repeated_query_terms_do_not_inflate_bm25()
    test_annotate_uses_per_document_normalized_scores()
    test_annotate_with_query_reference_does_not_auto_pass_top_hit()
    test_cross_word_bigrams_do_not_dilute_keyword_evidence()
    print("\n全部通过")
