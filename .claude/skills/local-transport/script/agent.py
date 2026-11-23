"""扩展技能：local-transport（简单提示技能）。"""
import os
import sys

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from utils.simple_skill import SimplePromptSkill


class LocalTransportAgent(SimplePromptSkill):
    skill_name = "local-transport"
    result_key = "transport_advice"
    system_prompt = "你是市内交通顾问。结合目的地景点分布与通勤时间，给出地铁、公交、打车、租车、步行的组合建议，注明支付方式、高峰时段与注意事项；不要编造具体票价，给区间即可。"
