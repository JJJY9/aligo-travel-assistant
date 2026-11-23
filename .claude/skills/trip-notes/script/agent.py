"""扩展技能：trip-notes（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class TripNotesAgent(SimplePromptSkill):
    skill_name = "trip-notes"
    result_key = "trip_notes"
    system_prompt = "你是差旅贴士助手。围绕报销、发票、时差、插座、网络、当地礼仪给出 3-6 条实用贴士，每条一句话，避免泛泛而谈。"
