"""扩展技能：safety-tips（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class SafetyTipsAgent(SimplePromptSkill):
    skill_name = "safety-tips"
    result_key = "safety_tips"
    system_prompt = "你是差旅安全顾问。根据目的地与行程给出天气、饮食、财物、夜间出行与紧急电话提醒，语气务实、不制造恐慌；不确定的信息提示用户核实官方渠道。"
