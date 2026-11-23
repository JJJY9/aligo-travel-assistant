"""行程规划校验与带原因重生成测试（无需真实模型）。

覆盖：合规行程一次通过、校验失败后带原因重生成、修复仍失败保留问题列表。

运行：python tests/test_plan_validation.py
"""
import asyncio
import importlib.util
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg


def _load_plan_module():
    path = os.path.join(
        project_root, ".claude", "skills", "plan-trip", "script", "agent.py"
    )
    module_name = "_test_plan_validation_agent"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def __call__(self, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        if not self.responses:
            return ""
        return self.responses.pop(0)


def _payload(query):
    return json.dumps(
        {"context": {"rewritten_query": query}, "previous_results": []},
        ensure_ascii=False,
    )


def _plan(days, estimated_budget):
    return json.dumps(
        {"itinerary": {"title": "杭州行", "estimated_budget": estimated_budget, "daily_plans": days}},
        ensure_ascii=False,
    )


def test_plan_validation_passes_without_repair():
    """合规行程只调用一次模型，validation.passed 为真。"""
    module = _load_plan_module()
    good = _plan(
        [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 400},
                    {"time": "13:00-17:00", "title": "灵隐寺", "cost": 300},
                ],
            }
        ],
        estimated_budget=700,
    )
    model = _ScriptedModel([good])
    agent = module.ItineraryPlanningAgent(name="ItineraryPlanningAgent", model=model)
    out = asyncio.run(
        agent.reply(
            Msg(
                name="orchestrator",
                content=_payload("去杭州玩1天，预算1000元，一定要去西湖和灵隐寺"),
                role="user",
            )
        )
    )
    result = json.loads(out.content)
    assert result["validation"]["passed"] is True
    assert result["validation"]["issues"] == []
    assert len(model.prompts) == 1
    print("✓ 合规行程一次通过，无修复调用")


def test_plan_validation_regenerates_with_reasons():
    """首版违反时间/预算/必去项时，把原因拼进 prompt 重生成并采用修正版。"""
    module = _load_plan_module()
    bad = _plan(
        [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 900},
                    {"time": "11:00-13:00", "title": "雷峰塔", "cost": 900},
                ],
            }
        ],
        estimated_budget=1800,
    )
    good = _plan(
        [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 400},
                ],
            },
            {
                "day": 2,
                "activities": [
                    {"time": "09:00-12:00", "title": "灵隐寺", "cost": 300},
                ],
            },
        ],
        estimated_budget=700,
    )
    model = _ScriptedModel([bad, good])
    agent = module.ItineraryPlanningAgent(name="ItineraryPlanningAgent", model=model)
    out = asyncio.run(
        agent.reply(
            Msg(
                name="orchestrator",
                content=_payload("去杭州玩2天，预算1000元，一定要去西湖和灵隐寺"),
                role="user",
            )
        )
    )
    result = json.loads(out.content)
    assert len(model.prompts) == 2, "首版不通过时应重生成一次"
    assert "未通过校验" in model.prompts[1]
    assert "time_conflict" in model.prompts[1] or "时间重叠" in model.prompts[1]
    assert result["validation"]["passed"] is True
    assert len(result["itinerary"]["daily_plans"]) == 2
    print("✓ 校验失败带原因重生成并采用修正版")


def test_plan_validation_keeps_issues_when_repair_still_fails():
    """修复版仍不通过时保留问题列表，不抛异常、不丢原行程。"""
    module = _load_plan_module()
    bad = _plan(
        [
            {
                "day": 1,
                "activities": [
                    {"time": "09:00-12:00", "title": "西湖", "cost": 900},
                ],
            }
        ],
        estimated_budget=900,
    )
    model = _ScriptedModel([bad, bad])
    agent = module.ItineraryPlanningAgent(name="ItineraryPlanningAgent", model=model)
    out = asyncio.run(
        agent.reply(
            Msg(
                name="orchestrator",
                content=_payload("去杭州玩2天，预算500元，一定要去灵隐寺"),
                role="user",
            )
        )
    )
    result = json.loads(out.content)
    assert result["validation"]["passed"] is False
    codes = {issue["code"] for issue in result["validation"]["issues"]}
    assert {"day_count_mismatch", "budget_exceeded", "must_see_missing"} <= codes
    assert result["itinerary"]["daily_plans"], "原行程不能因修复失败被清空"
    print("✓ 修复仍失败时保留问题与行程")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_plan_validation_passes_without_repair()
    test_plan_validation_regenerates_with_reasons()
    test_plan_validation_keeps_issues_when_repair_still_fails()
    print("\n全部通过")
