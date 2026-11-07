"""
RAG 检索评测指标测试（无需 API Key、不访问网络）

覆盖：
1. 全部命中时的 hit@K / MRR；
2. 混合排名与未命中的计算；
3. top_k 截断；
4. 空用例集。

运行：python tests/test_rag_eval.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.rag_eval import EvalCase, evaluate_retrieval


def test_all_hits_at_one():
    cases = (EvalCase("问题一", ("alpha",)), EvalCase("问题二", ("beta",)))

    def search(query, top_k):
        return [{"content": "alpha beta"}, {"content": "无关"}]

    result = evaluate_retrieval(search, cases=cases, top_k=5)
    assert result["total"] == 2
    assert result["hit1"] == 1.0
    assert result["hit3"] == 1.0
    assert result["hit5"] == 1.0
    assert result["mrr"] == 1.0
    print("✓ 全部命中：hit@K = 100%，MRR = 1")


def test_mixed_ranks_and_misses():
    cases = (
        EvalCase("问题一", ("alpha",)),
        EvalCase("问题二", ("beta",)),
        EvalCase("问题三", ("gamma",)),
    )
    docs = {
        "问题一": ["无关", "alpha"],
        "问题二": ["无关", "无关", "beta"],
        "问题三": ["无关", "无关", "无关"],
    }

    def search(query, top_k):
        return [{"content": text} for text in docs[query][:top_k]]

    result = evaluate_retrieval(search, cases=cases, top_k=5)
    assert abs(result["hit1"] - 0.0) < 1e-9
    assert abs(result["hit3"] - 2 / 3) < 1e-9
    assert abs(result["hit5"] - 2 / 3) < 1e-9
    assert abs(result["mrr"] - (1 / 2 + 1 / 3) / 3) < 1e-9
    assert [row["rank"] for row in result["rows"]] == [2, 3, None]
    print("✓ 混合排名 / 未命中：hit@K 与 MRR 计算正确")


def test_top_k_truncation():
    cases = (EvalCase("问题", ("alpha",)),)

    def search_full(query, top_k):
        documents = [{"content": f"片段{i}"} for i in range(1, 5)] + [{"content": "alpha"}]
        return documents[:top_k]

    result = evaluate_retrieval(search_full, cases=cases, top_k=3)
    assert result["hit3"] == 0.0, "第 5 个片段不应计入 top_k=3"
    assert result["hit5"] == 0.0, "search 只返回 top_k=3 个片段时不可能命中第 5 名"

    full = evaluate_retrieval(search_full, cases=cases, top_k=5)
    assert full["hit5"] == 1.0 and full["hit3"] == 0.0
    print("✓ top_k 截断生效")


def test_empty_cases():
    result = evaluate_retrieval(lambda query, top_k: [], cases=(), top_k=5)
    assert result == {
        "total": 0,
        "hit1": 0.0,
        "hit3": 0.0,
        "hit5": 0.0,
        "mrr": 0.0,
        "rows": [],
    }
    print("✓ 空用例集不报错")


def test_min_score_filters_low_similarity_and_warns():
    """评测应用问答链路的相似度阈值；全未命中时给出题库不匹配告警。"""
    cases = (EvalCase("问题一", ("关键词",)),)

    def search_low(query, top_k):
        return [{"content": "关键词", "distance": 0.2}]

    result = evaluate_retrieval(search_low, cases=cases, top_k=5, min_score=0.5)
    assert result["hit5"] == 0.0 and result["mrr"] == 0.0
    assert "warning" in result and "题库" in result["warning"]

    def search_high(query, top_k):
        return [{"content": "关键词", "distance": 0.8}]

    ok = evaluate_retrieval(search_high, cases=cases, top_k=5, min_score=0.5)
    assert ok["hit5"] == 1.0 and "warning" not in ok
    print("✓ 评测阈值过滤与题库不匹配告警")


if __name__ == "__main__":
    test_min_score_filters_low_similarity_and_warns()
    test_all_hits_at_one()
    test_mixed_ranks_and_misses()
    test_top_k_truncation()
    test_empty_cases()
    print("\n全部通过")
