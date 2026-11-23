"""扩展技能：packing-list（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class PackingListAgent(SimplePromptSkill):
    skill_name = "packing-list"
    result_key = "packing_list"
    system_prompt = "你是行李清单助手。根据目的地、季节、天数、出行目的与用户偏好生成行李清单，分证件、衣物、电子、洗漱、药品、其他六类，突出必须项与容易遗漏的物品。"
