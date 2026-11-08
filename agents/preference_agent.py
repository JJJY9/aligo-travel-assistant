"""兼容导出：偏好智能体已迁到 `skills/preference` 插件。

SKILL.md 示例长期以 ``from agents.preference_agent import PreferenceAgent``
初始化；实现仍在技能插件里，这里按其它技能同一方式按路径动态加载，
避免文档示例 ModuleNotFoundError。
"""
from agents._skill_compat import load_skill_agent

PreferenceAgent = load_skill_agent("preference", "PreferenceAgent")

__all__ = ["PreferenceAgent"]
