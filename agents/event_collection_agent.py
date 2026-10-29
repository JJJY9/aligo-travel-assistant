"""兼容导出：事项收集智能体已迁到 `skills/event-collection` 插件。"""
from agents._skill_compat import load_skill_agent

EventCollectionAgent = load_skill_agent("event-collection", "EventCollectionAgent")

__all__ = ["EventCollectionAgent"]
