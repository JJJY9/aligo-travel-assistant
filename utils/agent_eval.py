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
    """把评测用例包装成意图智能体的输入消息。

    必须与线上入口同形：CLI / Web / plan-trip 技能喂给 ``IntentionAgent``
    的都是用户原话（自然语言最后一条 user 消息），``reply`` 对单条 Msg
    直接把 content 当【用户Query】。此前这里包装成子智能体契约的 JSON
    信封（context / previous_results），评测 prompt 与生产不一致，Pass@1
    不可比。
    """
    query = str(case.get("query") or "")
    return Msg(name="user", content=query, role="user")


def prediction_from_intention_content(content: object) -> dict[str, Any]:
    """从意图智能体输出解析预测：标准名去重列表 + 空答案。

    解析失败 / 结构非法时带 ``error`` 标记：空意图集合与问候类用例的
    期望集合相等，若继续按「空意图」计分会把模型输出故障误判为通过。
    """
    try:
        data = json.loads(str(content or ""))
    except (json.JSONDecodeError, ValueError):
        data = None
    schedule = data.get("agent_schedule") if isinstance(data, dict) else None
    if not isinstance(schedule, list):
        return {
            "intents": [],
            "answer": "",
            "error": "意图输出解析失败",
        }
    intents: list[str] = []
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
    failed = 0
    for case in selected:
        try:
            response = await intention_agent.reply(case_message(case))
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中止整批评测
            failed += 1
            predictions[str(case.get("id"))] = {
                "intents": [],
                "answer": "",
                "error": f"预测异常：{type(exc).__name__}",
            }
            continue
        predictions[str(case.get("id"))] = prediction_from_intention_content(
            getattr(response, "content", None)
        )
    if selected and failed == len(selected):
        # 全部失败（密钥失效 / 网关整体不可用）不是「模型能力 0 分」：
        # 交给 CLI 的既有失败分支，避免 min_pass=0.0 时把故障算成达标
        raise RuntimeError(f"全部 {failed} 条评测预测失败")
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
    missing_prediction = {
        "intents": [],
        "answer": "",
        "error": "未收集到预测",
    }
    return run_eval(
        intent_cases,
        lambda case: predictions.get(str(case.get("id")))
        or dict(missing_prediction),
    )
