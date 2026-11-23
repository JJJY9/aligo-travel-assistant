"""扩展技能：trip-budget（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class TripBudgetAgent(SimplePromptSkill):
    skill_name = "trip-budget"
    result_key = "budget_advice"
    system_prompt = "你是差旅预算助手。根据目的地、天数、人数、出行方式与用户偏好，给出交通、住宿、餐饮、门票、市内交通的分项预算与总额（人民币），注明关键假设；金额要合理，最后给一句节省建议。"
