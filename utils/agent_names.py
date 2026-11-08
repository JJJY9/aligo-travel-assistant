"""智能体名称归一（技能目录名 ↔ 标准智能体名）。

注册器同时接受技能目录名与旧智能体名（plan-trip / event-collection 等）；
下游的聚合、记忆写入、执行路线元数据与前端展示统一使用标准名称。
放在 utils 下供编排器与轮次元数据共用，避免两边各自维护映射。
"""
SKILL_AGENT_NAMES = {
    "plan-trip": "itinerary_planning",
    "event-collection": "event_collection",
    "ask-question": "rag_knowledge",
    "memory-query": "memory_query",
    "query-info": "information_query",
}


def canonical_agent_name(agent_name: str) -> str:
    """把技能目录名统一为标准智能体名（大小写不敏感；未知名称原样返回）。

    模型偶尔输出 ``Plan-Trip`` / ``EVENT-COLLECTION``：只做精确匹配会当成
    未注册名称，整轮调度失败。目录名与标准名都是 ASCII，casefold 足够。
    """
    name = str(agent_name or "").strip()
    mapped = SKILL_AGENT_NAMES.get(name.casefold())
    return mapped if mapped is not None else name
