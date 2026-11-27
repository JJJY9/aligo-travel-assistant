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
    assert result["mode"] == "vector"
    assert "纯向量" in result["note"]
    assert {k: v for k, v in result.items() if k not in ("mode", "note")} == {
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
    assert "min_score" in result["warning"], "告警需提示阈值排查，不能只引导换题库"
    # 口径说明：评测是纯向量 top-K，不含 BM25 / 混合召回（避免被当成聊天可用率）
    assert result.get("mode") == "vector", result
    assert "纯向量" in str(result.get("note")), result
    assert "BM25" in str(result.get("note")), result

    def search_high(query, top_k):
        return [{"content": "关键词", "distance": 0.8}]

    ok = evaluate_retrieval(search_high, cases=cases, top_k=5, min_score=0.5)
    assert ok["hit5"] == 1.0 and "warning" not in ok
    print("✓ 评测阈值过滤与题库不匹配告警")


def test_eval_cases_match_bundled_documents():
    """题库与随仓库提供的知识文档一致：每个关键词在声明的来源文档中真实
    出现，且每份文档都有用例覆盖——改知识库或新增文档时题库不会悄悄漂移。"""
    from pathlib import Path

    from utils.rag_eval import EVAL_CASES

    documents_dir = (
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "skills"
        / "ask-question"
        / "data"
        / "documents"
    )
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in documents_dir.glob("*.txt")
    }
    assert files, f"找不到随仓库提供的知识文档：{documents_dir}"
    covered: set[str] = set()
    for case in EVAL_CASES:
        assert case.source in files, (
            f"用例来源不存在：{case.query} -> {case.source!r}"
        )
        covered.add(case.source)
        text = files[case.source].lower()
        for keyword in case.keywords:
            assert keyword.lower() in text, (
                f"关键词未在来源文档中出现：{case.query} -> {keyword}（{case.source}）"
            )
    missing = set(files) - covered
    assert not missing, f"以下知识文档没有任何评测用例覆盖：{sorted(missing)}"
    print("✓ 评测题库与知识文档一致且全量覆盖")


def test_discount_figures_consistent_across_documents():
    """协议价优惠区间在知识文档之间必须一致。

    FAQ 的协议酒店条目曾写「低10%-20%」而平台指南写「低10%-30%」：
    两个来源给出不同答案，检索命中哪个就答哪个，评测关键词也只认其一。
    """
    import re
    from pathlib import Path

    documents_dir = (
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "skills"
        / "ask-question"
        / "data"
        / "documents"
    )
    pattern = re.compile(r"低\s*(\d+)%\s*-\s*(\d+)%")
    figures: dict[str, set[tuple[int, int]]] = {}
    for path in documents_dir.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        ranges = {
            (int(match.group(1)), int(match.group(2)))
            for match in pattern.finditer(text)
        }
        if ranges:
            figures[path.name] = ranges
    assert figures, "知识文档应至少声明一次协议价优惠区间"
    distinct = set().union(*figures.values())
    assert len(distinct) == 1, (
        f"协议价优惠区间在各文档间不一致：{figures}"
    )
    print("✓ 协议价优惠区间跨文档一致")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_min_score_filters_low_similarity_and_warns()
    test_all_hits_at_one()
    test_mixed_ranks_and_misses()
    test_top_k_truncation()
    test_empty_cases()
    test_eval_cases_match_bundled_documents()
    test_discount_figures_consistent_across_documents()
    print("\n全部通过")
