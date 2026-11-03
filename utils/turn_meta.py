"""
对话轮次元数据（Turn Metadata）

助手消息在长期记忆中保存的是编排结果 JSON。为了让会话历史也能回看
「执行路线」（意图、调度计划、各智能体状态、用量与耗时），把元数据以
``_meta`` 键附加在同一份 JSON 中：

- 渲染回复时忽略该键（``result_to_markdown`` 只读取 results）；
- 读取历史时通过 :func:`extract_turn_meta` 还原执行路线；
- 生成长期记忆总结前通过 :func:`strip_turn_meta` 去掉元数据，避免污染提示词。

旧数据没有 ``_meta`` 键，读取方按“无执行信息”处理，保持向后兼容。
"""
import json
from typing import Any

META_KEY = "_meta"


def build_turn_meta(
    intention_data: dict[str, Any] | None,
    result_data: dict[str, Any] | None,
    usage: dict[str, Any] | None = None,
    elapsed: float | None = None,
) -> dict[str, Any]:
    """构建一条助手消息的执行元数据。"""
    intention = intention_data if isinstance(intention_data, dict) else {}
    result = result_data if isinstance(result_data, dict) else {}

    schedule = []
    for item in intention.get("agent_schedule") or []:
        if not isinstance(item, dict):
            continue
        schedule.append(
            {
                "agent_name": item.get("agent_name", ""),
                "priority": item.get("priority", 0),
                "reason": item.get("reason", ""),
            }
        )

    agents = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        agents.append(
            {
                "name": item.get("agent_name", ""),
                "status": item.get("status", "unknown"),
            }
        )

    return {
        "intents": intention.get("intents") or [],
        "reasoning": intention.get("reasoning") or "",
        "schedule": schedule,
        "agents": agents,
        "usage": usage,
        "elapsed": round(float(elapsed), 2) if elapsed is not None else None,
    }


def attach_turn_meta(result_data: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """返回附加了元数据的结果副本（不修改调用方传入的对象）。"""
    payload = dict(result_data)
    payload[META_KEY] = meta
    return payload


def extract_turn_meta(message_content: str) -> dict[str, Any] | None:
    """从已存储的助手消息中取出执行元数据（无则返回 None）。"""
    try:
        data = json.loads(message_content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    meta = data.get(META_KEY)
    return meta if isinstance(meta, dict) else None


def strip_turn_meta(message_content: str) -> str:
    """去掉助手消息中的元数据，用于 LLM 总结等场景。"""
    try:
        data = json.loads(message_content)
    except (TypeError, json.JSONDecodeError):
        return message_content
    if not isinstance(data, dict) or META_KEY not in data:
        return message_content
    return json.dumps({key: value for key, value in data.items() if key != META_KEY}, ensure_ascii=False)
