"""兼容导出：记忆查询智能体已迁到 `skills/memory-query` 插件。

SKILL.md 示例长期以 ``from agents.memory_query_agent import MemoryQueryAgent``
初始化；实现仍在技能插件里，这里按其它技能同一方式按路径动态加载，
避免文档示例 ModuleNotFoundError。
"""
from agents._skill_compat import load_skill_agent

MemoryQueryAgent = load_skill_agent("memory-query", "MemoryQueryAgent")

__all__ = ["MemoryQueryAgent"]
