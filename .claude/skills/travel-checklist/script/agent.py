"""扩展技能：travel-checklist（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class TravelChecklistAgent(SimplePromptSkill):
    skill_name = "travel-checklist"
    result_key = "checklist"
    system_prompt = "你是行前检查清单助手。生成证件、订票、酒店、报销凭证、保险、天气与紧急联系人清单，按出发前一周、前一天、当天分组；只列可执行事项。"
