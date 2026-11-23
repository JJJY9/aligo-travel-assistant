"""Personal Agent 评测骨架与用例生成测试（纯 Python）。

覆盖：规则验收（意图 / 关键词 / 禁用词）、Judge 分数、总体与分类汇总、
预测异常、500 条分层用例生成与 JSONL 导出。

运行：python tests/test_eval_harness.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.eval_cases import build_cases, dump_cases
from utils.eval_harness import evaluate_case, run_eval


def _case(**overrides):
    base = {
        "id": "c1",
        "category": "plan_trip",
        "query": "帮我规划杭州 3 天行程",
        "expected_intents": ["itinerary_planning"],
        "required_keywords": ["杭州"],
        "forbidden_keywords": ["无法安排"],
    }
    base.update(overrides)
    return base


def test_evaluate_case_passes():
    result = evaluate_case(
        _case(),
        {"intents": ["itinerary_planning"], "answer": "杭州三天行程如下"},
    )
    assert result["passed"] is True
    assert result["reasons"] == []
    print("✓ 合规回答通过规则验收")


def test_evaluate_case_reports_reasons():
    mismatch = evaluate_case(
        _case(),
        {"intents": ["information_query"], "answer": "无法安排"},
    )
    assert mismatch["passed"] is False
    text = " ".join(mismatch["reasons"])
    assert "意图不匹配" in text
    assert "缺少关键词" in text
    assert "禁用内容" in text
    print("✓ 意图 / 关键词 / 禁用词原因")


def test_judge_score_and_failure():
    case = _case(required_keywords=[], forbidden_keywords=[])
    good = evaluate_case(
        case,
        {"intents": ["itinerary_planning"], "answer": "ok"},
        judge=lambda case, answer: 0.8,
    )
    assert good["passed"] is True and good["judge_score"] == 0.8

    bad = evaluate_case(
        case,
        {"intents": ["itinerary_planning"], "answer": "ok"},
        judge=lambda case, answer: 0.2,
    )
    assert bad["passed"] is False
    assert "Judge 分数" in " ".join(bad["reasons"])

    def _boom(case, answer):
        raise RuntimeError("judge down")

    fallback = evaluate_case(
        case,
        {"intents": ["itinerary_planning"], "answer": "ok"},
        judge=_boom,
    )
    assert fallback["passed"] is True, "Judge 故障不应一票否决规则通过"
    assert "Judge 调用失败" in " ".join(fallback["reasons"])
    print("✓ Judge 分数与故障兜底")


def test_empty_expected_intents_still_checked():
    """空期望意图（问候）也必须完全匹配，不能被跳过。"""
    case = _case(
        category="greeting",
        expected_intents=[],
        required_keywords=[],
        forbidden_keywords=[],
    )
    wrong = evaluate_case(
        case, {"intents": ["itinerary_planning"], "answer": ""}
    )
    assert wrong["passed"] is False
    assert "意图不匹配" in " ".join(wrong["reasons"])
    right = evaluate_case(case, {"intents": [], "answer": ""})
    assert right["passed"] is True
    print("✓ 空期望意图仍严格匹配")


def test_run_eval_aggregates_and_handles_errors():
    cases = [
        _case(id="ok", required_keywords=[], forbidden_keywords=[]),
        _case(id="bad", required_keywords=["灵隐寺"], forbidden_keywords=[]),
        _case(id="boom", required_keywords=[], forbidden_keywords=[]),
    ]

    def predict(case):
        if case["id"] == "boom":
            raise RuntimeError("model down")
        answer = "杭州行程" if case["id"] == "ok" else "杭州行程"
        return {"intents": ["itinerary_planning"], "answer": answer}

    summary = run_eval(cases, predict)
    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["pass_at_1"] == 1 / 3
    bucket = summary["by_category"]["plan_trip"]
    assert bucket == {"total": 3, "passed": 1}
    failure_ids = {item["id"] for item in summary["failures"]}
    assert failure_ids == {"bad", "boom"}
    print("✓ 总体 / 分类汇总与预测异常")


def test_build_cases_covers_twelve_categories():
    cases = build_cases(count=500)
    assert len(cases) == 500
    categories = {case["category"] for case in cases}
    assert len(categories) == 12, categories
    assert len({case["id"] for case in cases}) == 500
    assert len({case["query"] for case in cases}) == 500
    for case in cases:
        assert isinstance(case["expected_intents"], list)
        if case["category"] != "greeting":
            assert case["expected_intents"], case
        assert isinstance(case["required_keywords"], list)
        assert isinstance(case["forbidden_keywords"], list)
    assert build_cases(count=500) == cases, "同参数生成必须确定性一致"
    print("✓ 500 条分层用例：12 类、唯一 id / query、确定性")


def test_dump_cases_jsonl_roundtrip():
    cases = build_cases(count=12)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.jsonl"
        dump_cases(path, cases)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 12
        assert json.loads(lines[0])["id"] == cases[0]["id"]
    print("✓ 评测用例 JSONL 导出")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_evaluate_case_passes()
    test_evaluate_case_reports_reasons()
    test_judge_score_and_failure()
    test_empty_expected_intents_still_checked()
    test_run_eval_aggregates_and_handles_errors()
    test_build_cases_covers_twelve_categories()
    test_dump_cases_jsonl_roundtrip()
    print("\n全部通过")
