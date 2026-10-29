"""兼容导出：行程规划智能体已迁到 `skills/plan-trip` 插件。"""
from agents._skill_compat import load_skill_agent

ItineraryPlanningAgent = load_skill_agent("plan-trip", "ItineraryPlanningAgent")

__all__ = ["ItineraryPlanningAgent"]
