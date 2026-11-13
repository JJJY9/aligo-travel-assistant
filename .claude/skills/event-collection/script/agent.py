"""
事项收集智能体
职责：收集用户的出发地/事项地点/事项时间/返程地

核心功能：
- 提取出发地、目的地、时间、返程地等基础信息
- 识别缺失信息并提示
"""
import json
import logging
import os
import sys

from agentscope.agent import AgentBase
from agentscope.message import Msg

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.llm_resilience import is_retriable_error
from utils.user_query import extract_user_query
from utils.json_parser import (
    extract_json_from_async_response,
    first_json_object_with_keys,
    last_json_object_with_keys_strict,
)

logger = logging.getLogger(__name__)

# 形如「提取结果」的字段（任一命中即视为候选）。只用 missing_info /
# extracted_count 会被 prompt 回显骗过：真实结果省略这两个键时，回显的
# 示例对象（含全部字段）反而是最后一个命中项，被当成真实提取写库。
_RESULT_KEYS = (
    "missing_info",
    "extracted_count",
    "destination",
    "origin",
    "start_date",
    "end_date",
    "duration_days",
    "return_location",
    "trip_purpose",
    "summary",
    # {"data": {...}} 包装：回显示例不含该键，识别后由 _unwrap_result 取出
    "data",
)

_EVENT_FIELDS = tuple(key for key in _RESULT_KEYS if key != "data")


def _unwrap_result(obj: object) -> dict | None:
    """取出事项结果对象；兼容 ``{"data": {提取字段...}}`` 包装。"""
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    candidate = (
        data
        if isinstance(data, dict)
        and any(key in data for key in _EVENT_FIELDS)
        else obj
    )
    if any(key in candidate for key in _EVENT_FIELDS):
        return candidate
    return None


def _format_pref_value(value) -> str:
    """偏好值可能是字符串或列表（见 LongTermMemory），统一为可读文本。

    直接对字符串做 ', '.join 会把内容按单个字符拆开（Issue #2）。
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


class EventCollectionAgent(AgentBase):
    """事项收集智能体"""

    def __init__(self, name: str = "EventCollectionAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model

    async def reply(self, x: Msg | list[Msg] | None = None) -> Msg:
        if x is None:
            # Msg.content 必须是 str 或 content block 列表（agentscope 断言）；
            # 传 {} 会让 reply(None) 直接 AssertionError
            return Msg(name=self.name, content="{}", role="assistant")

        # 解析输入内容
        content = x.content if not isinstance(x, list) else x[-1].content

        # 如果content是JSON字符串，解析它
        if isinstance(content, str):
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = None
            # 顶层 / context 可能不是对象（模型或调用方给了数组 / 数字 /
            # 字符串）：直接 .get 会 AttributeError 绕过上面的兜底
            if isinstance(data, dict):
                context = data.get("context")
                if not isinstance(context, dict):
                    context = {}
                # 与其它技能一致：优先用户本轮原话，其次改写查询；都没有时
                # 才退回整段上下文（含 recent_dialogue / previous_results，
                # 直接当作事项描述会带进大量无关上下文）
                user_query = (
                    context.get("user_query")
                    or context.get("rewritten_query", "")
                    or str(data)
                )
                user_preferences = context.get("user_preferences")
                if not isinstance(user_preferences, dict):
                    user_preferences = {}
            else:
                user_query = extract_user_query(content)
                user_preferences = {}
        else:
            # agentscope content block 列表 / dict：拼出文本，不能 str() 成
            # Python repr 当作用户查询
            user_query = extract_user_query(content)
            user_preferences = {}

        # 构建用户背景信息
        background_info = ""
        if user_preferences:
            bg_parts = ["【用户背景信息】（可用于推断缺失信息）"]
            if user_preferences.get("home_location"):
                bg_parts.append(f"• 家庭住址: {_format_pref_value(user_preferences['home_location'])}")
            if user_preferences.get("hotel_brands"):
                bg_parts.append(f"• 酒店偏好: {_format_pref_value(user_preferences['hotel_brands'])}")
            if user_preferences.get("airlines"):
                bg_parts.append(f"• 航空偏好: {_format_pref_value(user_preferences['airlines'])}")

            if len(bg_parts) > 1:
                background_info = "\n".join(bg_parts) + "\n\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        prompt = f"""你是事项收集专家，负责提取旅行的基础信息。

【当前时间】
{current_date} {weekday}

{background_info}【用户输入】
{user_query}

【提取要求】
请尽可能提取以下信息：
1. origin - 出发地
2. destination - 目的地
3. start_date - 出发日期（YYYY-MM-DD格式）
4. end_date - 返程日期
5. duration_days - 行程天数
6. return_location - 返程地
7. trip_purpose - 行程目的

【日期处理规则】（重要）
- 当前时间是{current_date}
- 用户说"2月27日"或"2.27"等相对时间，请根据当前时间推断完整日期（年月日）
- 用户说"明天"、"后天"、"下周"等相对时间，请根据当前时间计算具体日期
- 所有日期必须输出完整的YYYY-MM-DD格式

【特殊处理】
- 对于"北京一日游"这类：destination和origin都设为北京
- 对于"一日游"：duration_days设为1
- 如果用户没说出发地，但有家庭住址信息，可推断出发地为家庭住址

【输出格式】(严格JSON)
{{
    "origin": "北京",
    "destination": "北京",
    "start_date": "2026-02-27",
    "end_date": "2026-02-27",
    "duration_days": 1,
    "return_location": "北京",
    "trip_purpose": "旅游",
    "missing_info": [],
    "extracted_count": 7,
    "summary": "北京一日游，2月27日"
}}

缺失的信息在missing_info中列出，对应字段设为null。
"""

        try:
            # 调用模型
            response = await self.model([
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本（兼容非流式 ChatResponse / 流式生成器 / 测试 mock）
            text = await extract_json_from_async_response(response)
            if not text or not str(text).strip():
                raise ConnectionError("模型返回空响应")

            # 清理文本，移除markdown代码块标记
            text = text.strip()
            text = text.removeprefix('```json')
            text = text.removeprefix('```')
            text = text.removesuffix('```')
            text = text.strip()

            # 模型可能先回显 prompt 里的示例对象再给正式结果：优先取最后
            # 一个形如结果（任一提取字段命中）的顶层对象，避免把回显示例
            # 当成提取结果写成真实行程。正式结果通常带 destination 等字段，
            # 即使省略 missing_info / extracted_count 也会排在示例之后；
            # duration_days / summary 也算结果形状（只含时长的正式结果）。
            result = _unwrap_result(
                last_json_object_with_keys_strict(text, _RESULT_KEYS)
            )
            if result is None:
                # 没有任何含提取字段的完整对象：退到第一个含提取字段的对象
                # （尾逗号等小瑕疵由修复路径兜底）。不能回退到「第一个
                # 对象」——prompt 回显的示例排在最前，正式结果被截断时会把
                # 示例（北京 / 2026-02-27）当成真实事项写成行程
                result = _unwrap_result(
                    first_json_object_with_keys(text, _RESULT_KEYS)
                )
            if result is None:
                raise ValueError("模型输出中没有可解析的事项结果对象")
        except Exception as e:
            if is_retriable_error(e):
                # 瞬时模型错误穿透给编排层重试 / 熔断，不吞成结果 JSON
                raise
            logger.error(f"Event collection failed: {e}")
            result = {
                "missing_info": ["所有信息"],
                "extracted_count": 0,
                "error": str(e)
            }

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
