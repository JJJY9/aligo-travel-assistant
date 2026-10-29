"""兼容导出：信息查询智能体已迁到 `skills/query-info` 插件。"""
from agents._skill_compat import load_skill_agent

InformationQueryAgent = load_skill_agent("query-info", "InformationQueryAgent")

__all__ = ["InformationQueryAgent"]
