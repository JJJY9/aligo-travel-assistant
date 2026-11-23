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


if __name__ == "__main__":
    test_tokenize_chinese_bigrams_and_ascii_words()
    test_bm25_ranks_keyword_match()
    test_rrf_prefers_documents_in_both_lists()
    test_annotate_hybrid_scores_uses_max_of_vector_and_normalized_bm25()
    print("\n全部通过")
