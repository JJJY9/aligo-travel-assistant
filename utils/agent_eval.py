"""Personal Agent 意图评测（v2.0）。

把 ``IntentionAgent`` 跑在分层评测用例上：每条用例只做意图识别，预测
``agent_schedule`` 归一为标准智能体名集合，再用 ``utils.eval_harness``
的规则验收（意图集合完全匹配；答案关键词不参与，纯意图评测）。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from agentscope.message import Msg

from utils.agent_names import canonical_agent_name
from utils.eval_harness import run_eval


def case_message(case: dict[str, Any]) -> Msg:
    """把评测用例包装成意图智能体的输入消息。"""
    query = str(case.get("query") or "")
    payload = {
        "context": {
            "rewritten_query": query,
            "user_query": query,
        },
        "previous_results": [],
    }
    return Msg(
        name="Evaluator",
        content=json.dumps(payload, ensure_ascii=False),
        role="user",
    )


def prediction_from_intention_content(content: object) -> dict[str, Any]:
    """从意图智能体输出解析预测：标准名去重列表 + 空答案。"""
    try:
        data = json.loads(str(content or ""))
    except (json.JSONDecodeError, ValueError):
        data = {}
    schedule = data.get("agent_schedule") if isinstance(data, dict) else None
    intents: list[str] = []
    if isinstance(schedule, list):
        for item in schedule:
            if not isinstance(item, dict):
                continue
            name = str(item.get("agent_name") or "").strip()
            if not name:
                continue
            canonical = canonical_agent_name(name)
            if canonical and canonical not in intents:
                intents.append(canonical)
    return {"intents": intents, "answer": ""}


async def collect_intent_predictions(
    cases: Sequence[dict[str, Any]],
    intention_agent: Any,
    limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    """逐条调用意图智能体，返回 ``{case_id: prediction}``。"""
    selected = list(cases) if limit is None else list(cases)[: max(0, limit)]
    predictions: dict[str, dict[str, Any]] = {}
    for case in selected:
        response = await intention_agent.reply(case_message(case))
        predictions[str(case.get("id"))] = prediction_from_intention_content(
            getattr(response, "content", None)
        )
    return predictions


def evaluate_intent_predictions(
    cases: Sequence[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """只按意图集合验收（答案关键词清空，不做 Judge）。"""
    intent_cases: list[dict[str, Any]] = []
    for case in cases:
        intent_case = dict(case)
        intent_case["required_keywords"] = []
        intent_case["forbidden_keywords"] = []
        intent_cases.append(intent_case)
    return run_eval(
        intent_cases,
        lambda case: predictions.get(str(case.get("id")), {}),
    )
