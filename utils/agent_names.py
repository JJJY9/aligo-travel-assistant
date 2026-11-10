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
# 标准名的大小写变体（模型常把标识符写成 Itinerary_Planning / RAG_Knowledge）：
# 只归一目录名会让遗留名变体击穿去重与规划依赖排序
_STANDARD_AGENT_NAMES = {value.casefold(): value for value in SKILL_AGENT_NAMES.values()}


def canonical_agent_name(agent_name: str) -> str:
    """把技能目录名 / 标准名的任意大小写统一为标准智能体名（未知名称原样返回）。

    模型偶尔输出 ``Plan-Trip`` / ``Itinerary_Planning``：只做精确匹配会
    当成未注册名称，整轮调度失败；遗留标准名的大小写变体还会让去重与
    「事项收集 / 偏好 → 行程规划」的依赖排序失效。名称都是 ASCII，
    casefold 足够。
    """
    name = str(agent_name or "").strip()
    key = name.casefold()
    mapped = SKILL_AGENT_NAMES.get(key)
    if mapped is not None:
        return mapped
    return _STANDARD_AGENT_NAMES.get(key, name)
