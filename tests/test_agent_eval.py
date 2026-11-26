"""意图评测（eval-agent）与 CLI 测试（无需真实模型）。"""
import asyncio
import contextlib
import io
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from agentscope.message import Msg
from utils.agent_eval import (
    case_message,
    collect_intent_predictions,
    evaluate_intent_predictions,
    prediction_from_intention_content,
)
from utils.eval_cases import build_cases
from utils.eval_harness import evaluate_case


class _KeywordIntention:
    """按问题关键词返回固定调度的假意图智能体。"""

    async def reply(self, message):
        # 评测消息与线上同形：content 就是用户原话
        query = str(message.content)
        if "规划" in query or "行程" in query:
            schedule = [{"agent_name": "plan-trip"}]
        elif query.strip() in {"你好", "早上好，在吗", "嗨"}:
            schedule = []
        else:
            schedule = [{"agent_name": "query-info"}]
        return Msg(
            name="Intention",
            content=json.dumps(
                {"agent_schedule": schedule}, ensure_ascii=False
            ),
            role="assistant",
        )


class _AlwaysPlan(_KeywordIntention):
    async def reply(self, message):
        return Msg(
            name="Intention",
            content=json.dumps(
                {"agent_schedule": [{"agent_name": "plan-trip"}]},
                ensure_ascii=False,
            ),
            role="assistant",
        )


def test_prediction_parses_and_canonicalizes_schedule():
    prediction = prediction_from_intention_content(
        json.dumps(
            {
                "agent_schedule": [
                    {"agent_name": "plan-trip"},
                    {"agent_name": "Plan-Trip"},
                    {"agent_name": ""},
                    "bad",
                ]
            }
        )
    )
    assert prediction["intents"] == ["itinerary_planning"], prediction
    assert prediction["answer"] == ""
    assert prediction_from_intention_content("not json")["intents"] == []
    assert prediction_from_intention_content(
        json.dumps({"agent_schedule": {}})
    )["intents"] == []
    print("✓ 意图预测解析与标准名归一")


def test_collect_and_evaluate_intent_predictions():
    from utils.eval_cases import build_cases

    cases = build_cases(2, 7)
    predictions = asyncio.run(
        collect_intent_predictions(cases, _KeywordIntention())
    )
    assert set(predictions) == {case["id"] for case in cases}
    summary = evaluate_intent_predictions(cases, predictions)
    assert summary["pass_at_1"] == 1.0, summary
    wrong = {
        case["id"]: {"intents": ["itinerary_planning"], "answer": ""}
        for case in cases
    }
    failing = evaluate_intent_predictions(cases, wrong)
    assert failing["pass_at_1"] < 1.0
    print("✓ 意图评测收集与规则验收")


def test_case_message_matches_production_shape():
    """评测消息与线上同形：content 就是用户原话，不是子技能 JSON 信封。"""
    msg = case_message({"query": "帮我规划杭州 3 天行程"})
    assert msg.content == "帮我规划杭州 3 天行程"
    assert msg.role == "user"
    print("✓ 评测输入与线上一致（用户原话）")


def test_parse_failure_is_marked_error_not_empty_intents():
    """解析失败不能退化成空意图集合——问候类期望也是空集合，会白送通过。"""
    prediction = prediction_from_intention_content("模型崩了")
    assert prediction["intents"] == []
    assert prediction["error"] == "意图输出解析失败"
    greeting = {
        "id": "g1",
        "category": "greeting",
        "query": "你好",
        "expected_intents": [],
        "required_keywords": [],
        "forbidden_keywords": [],
    }
    result = evaluate_case(greeting, prediction)
    assert result["passed"] is False
    assert "预测不可用" in " ".join(result["reasons"])
    print("✓ 解析失败带 error 标记并按失败计分")


def test_other_cases_expect_no_scheduled_agent():
    """无关问题按空意图集合计分：管线没有可调度的 other 智能体。"""
    others = [case for case in build_cases(500, 7) if case["category"] == "other"]
    assert others
    assert all(case["expected_intents"] == [] for case in others)
    print("✓ 无关问题期望空调度")


def test_collect_isolates_single_failure_and_raises_when_all_fail():
    """单条 429/超时不能中止整批评测；全部失败仍要显式报错。"""

    class _Flaky:
        def __init__(self):
            self.calls = 0

        async def reply(self, message):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("429")
            return Msg(
                name="Intention",
                content=json.dumps({"agent_schedule": []}),
                role="assistant",
            )

    cases = build_cases(3, 7)
    predictions = asyncio.run(collect_intent_predictions(cases, _Flaky()))
    assert set(predictions) == {case["id"] for case in cases}
    assert predictions[cases[1]["id"]]["error"] == "预测异常：RuntimeError"
    assert "error" not in predictions[cases[0]["id"]]

    class _AlwaysBoom:
        async def reply(self, message):
            raise RuntimeError("401")

    with pytest.raises(RuntimeError):
        asyncio.run(collect_intent_predictions(cases, _AlwaysBoom()))
    print("✓ 单条失败隔离，全部失败显式报错")


def test_parse_eval_agent_args():
    assert cli._parse_eval_agent_args([]) == (20, 7, 0.0, False)
    assert cli._parse_eval_agent_args(
        ["--count", "50", "--seed=3", "--min-pass", "0.8", "--json"]
    ) == (50, 3, 0.8, True)
    assert cli._parse_eval_agent_args(["--count", "0"]) is None
    assert cli._parse_eval_agent_args(["--count", "abc"]) is None
    assert cli._parse_eval_agent_args(["--min-pass", "1.5"]) is None
    assert cli._parse_eval_agent_args(["--unknown"]) is None
    print("✓ eval-agent 参数解析")


def test_run_agent_eval_with_fake_intention():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.run_agent_eval_standalone(
            count=2,
            seed=7,
            min_pass=1.0,
            as_json=True,
            intention_factory=_KeywordIntention,
        )
    assert code == 0, output.getvalue()
    payload = json.loads(output.getvalue())
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["pass_at_1"] == 1.0
    print("✓ eval-agent 达标退出 0")

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.run_agent_eval_standalone(
            count=2,
            seed=7,
            min_pass=1.0,
            as_json=False,
            intention_factory=_AlwaysPlan,
        )
    assert code == 1
    assert "低于阈值" in output.getvalue()
    print("✓ eval-agent 未达标退出 1")


def test_eval_agent_dispatch(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(
        cli,
        "run_agent_eval_standalone",
        lambda **kwargs: calls.update(kwargs) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "eval-agent", "--count", "12", "--min-pass", "0.5", "--json"],
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == {
        "count": 12,
        "seed": 7,
        "min_pass": 0.5,
        "as_json": True,
    }
    print("✓ eval-agent dispatch")


def test_eval_agent_dispatch_invalid_args(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "eval-agent", "--count", "abc"]
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "用法：python cli.py eval-agent" in output.getvalue()
    print("✓ eval-agent 非法参数退出 2")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_prediction_parses_and_canonicalizes_schedule()
    test_collect_and_evaluate_intent_predictions()
    test_case_message_matches_production_shape()
    test_parse_failure_is_marked_error_not_empty_intents()
    test_other_cases_expect_no_scheduled_agent()
    test_collect_isolates_single_failure_and_raises_when_all_fail()
    test_parse_eval_agent_args()
    test_run_agent_eval_with_fake_intention()
    mp = pytest.MonkeyPatch()
    try:
        test_eval_agent_dispatch(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_eval_agent_dispatch_invalid_args(mp)
    finally:
        mp.undo()
    print("\n全部通过")
