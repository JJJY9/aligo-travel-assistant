"""Personal Agent 分层评测骨架（v2.0）。

规则验收 + 可选 LLM-as-Judge：
- 意图：预测意图集合与标注完全一致才通过（可扩展为部分匹配）；
- 答案：必须包含全部 required_keywords，且不得出现 forbidden_keywords；
- Judge：可选可调用对象 `judge(case, answer) -> float | None`，≥0.6 视为通过；
- 汇总：总体 Pass@1、分类准确率、Judge 均分、失败原因清单。

只做评测编排，不依赖具体模型 / 知识库；调用方传入预测函数。
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable, Iterable

JUDGE_PASS_SCORE = 0.6


def _normalize(text: object) -> str:
    return str(text or "").casefold()


def _intents(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).casefold() for item in value if str(item).strip()}
    return set()


def evaluate_case(
    case: dict[str, Any],
    prediction: dict[str, Any],
    judge: Callable[[dict[str, Any], str], float | None] | None = None,
) -> dict[str, Any]:
    """对单条用例做规则验收（可选 Judge），返回通过与否及原因。"""
    reasons: list[str] = []
    # 预测侧显式报错（输出无法解析 / 单条调用失败）：空意图集合与问候类
    # 的期望集合相等，不能按「意图匹配」白送通过
    prediction_error = str(prediction.get("error") or "").strip()
    if prediction_error:
        reasons.append(f"预测不可用：{prediction_error}")
    expected = _intents(case.get("expected_intents"))
    predicted = _intents(prediction.get("intents"))
    intents_ok = expected == predicted
    if not intents_ok:
        reasons.append(
            "意图不匹配：期望 "
            f"{sorted(expected)}，实际 {sorted(predicted)}"
        )

    answer = _normalize(prediction.get("answer"))
    missing = [
        str(keyword)
        for keyword in case.get("required_keywords") or []
        if _normalize(keyword) not in answer
    ]
    if missing:
        reasons.append("缺少关键词：" + "、".join(missing))
    forbidden = [
        str(keyword)
        for keyword in case.get("forbidden_keywords") or []
        if _normalize(keyword) in answer
    ]
    if forbidden:
        reasons.append("出现禁用内容：" + "、".join(forbidden))

    judge_score: float | None = None
    if judge is not None:
        try:
            score = judge(case, str(prediction.get("answer") or ""))
            if score is not None:
                # 归一化放在 try 内：非数字返回值（字符串 / 容器）此前会在
                # 保护块外抛 ValueError 让整轮评测崩溃
                score = float(score)
                if not math.isfinite(score):
                    # NaN / Infinity 混入会让 judge_mean 变成 NaN，
                    # 且 ``nan < 阈值`` 恒为 False 把用例静默判通过
                    raise ValueError(f"非有限分数 {score!r}")
        except Exception as exc:  # noqa: BLE001 - Judge 失败按未评分处理
            score = None
            reasons.append(f"Judge 调用失败：{type(exc).__name__}")
        if score is not None:
            judge_score = float(score)
            if judge_score < JUDGE_PASS_SCORE:
                reasons.append(f"Judge 分数 {judge_score:.2f} < {JUDGE_PASS_SCORE}")

    passed = (
        not prediction_error
        and intents_ok
        and not missing
        and not forbidden
        and not (judge_score is not None and judge_score < JUDGE_PASS_SCORE)
    )
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "passed": passed,
        "reasons": reasons,
        "judge_score": judge_score,
    }


def run_eval(
    cases: Iterable[dict[str, Any]],
    predict: Callable[[dict[str, Any]], dict[str, Any]],
    judge: Callable[[dict[str, Any], str], float | None] | None = None,
) -> dict[str, Any]:
    """跑完整评测集，返回总体 / 分类指标与失败清单。"""
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            prediction = predict(case) or {}
        except Exception as exc:  # noqa: BLE001 - 预测失败计为未通过
            prediction = {}
            results.append(
                {
                    "id": case.get("id"),
                    "category": case.get("category"),
                    "passed": False,
                    "reasons": [f"预测异常：{type(exc).__name__}"],
                    "judge_score": None,
                }
            )
            continue
        results.append(evaluate_case(case, prediction, judge))

    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for item in results:
        category = str(item.get("category") or "未分类")
        bucket = by_category.setdefault(category, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(bool(item["passed"]))
    judge_scores = [
        item["judge_score"]
        for item in results
        if item.get("judge_score") is not None
    ]
    return {
        "total": total,
        "passed": passed,
        "pass_at_1": (passed / total) if total else 0.0,
        "by_category": by_category,
        "judge_mean": statistics.fmean(judge_scores) if judge_scores else None,
        "failures": [item for item in results if not item["passed"]],
        "results": results,
    }
