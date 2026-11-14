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
import re
import sys

from agentscope.agent import AgentBase
from agentscope.message import Msg

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.llm_resilience import is_permanent_llm_error, is_retriable_error
from utils.user_query import extract_user_query
from utils.json_parser import (
    extract_json_from_async_response,
    has_unclosed_json_object,
    iter_json_object_spans_with_repair,
)

logger = logging.getLogger(__name__)

# 形如「提取结果」的字段（任一命中即视为结果形状的对象）。data 只作为
# 包装键参与识别，不参与「实质结果」打分
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
    "data",
)

_EVENT_FIELDS = tuple(key for key in _RESULT_KEYS if key != "data")

# 实质提取字段：尾注小对象（只含 summary / missing_info 等元信息）不含
# 其中任何有值字段，不能顶掉前面的完整提取结果
_CORE_FIELDS = (
    "origin",
    "destination",
    "start_date",
    "end_date",
    "duration_days",
    "return_location",
    "trip_purpose",
)

# 中文 / 斜杠 / 点号写法：模型偶发不遵循 ISO 要求，原样落库会被下游
# 日期归一判成非法，整条行程静默不建档
_DATE_TEXT_RE = re.compile(
    r"^(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?$"
)


def _has_event_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _plan_score(obj: dict) -> int:
    """结果对象的实质字段数量（区分正式结果与尾注元信息对象）。"""
    return sum(1 for key in _CORE_FIELDS if _has_event_value(obj.get(key)))


def _is_explicit_empty(obj: dict) -> bool:
    """显式「没有提取到信息」的结果：extracted_count == 0，或（计数缺失、
    没有任何核心字段、missing_info 非空）。

    注意把「附在完整结果之后的元信息尾注」（missing_info 非空但
    extracted_count > 0）排除在外：那是补充说明，不是否决提取结果。
    """
    count = obj.get("extracted_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count == 0
    if _plan_score(obj) > 0:
        return False
    missing = obj.get("missing_info")
    return isinstance(missing, list) and any(
        isinstance(item, str) and item.strip() for item in missing
    )


# 候选对象前的标签：只有「整行就是一个短标签」才参与判定（示例：/尾注：），
# 叙述句里偶然出现「示例」「补充」不算。形状相同的 missing_info 对象既可能
# 是本轮「信息不足」终稿、也可能是附在结果后的尾注，标签与位置共同决定它
# 能否否决更早的实质候选。
_EVENT_EXAMPLE_LABEL_RE = re.compile(
    r"^(示例|例如|样例)(输出|结果|内容|格式)?\s*[:：]?\s*$"
)
_EVENT_SUPPLEMENT_LABEL_RE = re.compile(
    r"^(补充|尾注|备注|附注|提示|注意|说明|须知)"
    r"(说明|信息|内容|事项|结果|行程|安排)?\s*[:：]?\s*$"
)


def _event_label_kind(label: str) -> str:
    """候选对象前的标签：example（回显示例）/ supplement（补充尾注）/ neutral。

    先剔除 Markdown 围栏（对象常被 ```json 包住，标签与对象之间隔着
    围栏行），再要求最后一个非空行本身就是一个短标签。
    """
    lines = []
    for item in label.splitlines():
        cleaned = re.sub(r"```+\w*\s*$", "", item).strip()
        if cleaned:
            lines.append(cleaned)
    if not lines:
        return "neutral"
    line = lines[-1]
    if _EVENT_EXAMPLE_LABEL_RE.match(line):
        return "example"
    if _EVENT_SUPPLEMENT_LABEL_RE.match(line):
        return "supplement"
    return "neutral"


def _normalize_event_date(text: str) -> str | None:
    """中文 / 斜杠日期归一为 ISO；非法日历日返回 None（字段置 null）。"""
    match = _DATE_TEXT_RE.fullmatch(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            from datetime import date

            return date(year, month, day).isoformat()
        except ValueError:
            return None
    try:
        from datetime import datetime

        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        # 非日期文本（「下周一」等）原样保留：下游同样按不可解析处理，
        # 这里不臆造日期
        return text


def _normalize_result_dates(result: dict) -> dict:
    for key in ("start_date", "end_date"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = _normalize_event_date(value.strip())
    return result



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


def _select_event_result(text: str) -> dict | None:
    """按位置选事项结果：最后一个实质结果（或显式缺省结果），尾注不顶替。

    此前取「最后一个含结果键的对象」：正式提取之后的尾注小对象
    （summary / missing_info / extracted_count）会整体顶掉完整结果；
    且最后一个含结果键的对象解包失败时（如末尾 ``{"data": null}``）会
    回退到第一个命中对象——prompt 回显的示例（北京 / 2026-02-27）被当成
    真实提取写进 trip_history。这里只接受真正可用的结果候选；末尾出现
    「像结果但解析不了」的片段（截断）时抛错，不回退示例。
    """
    last_any: dict | None = None
    last_any_end = -1
    substantive: dict | None = None
    substantive_end = -1
    substantive_kind = "neutral"
    explicit_empty: dict | None = None
    explicit_empty_end = -1
    explicit_empty_kind = "neutral"
    failed_tail = False
    prev_end = 0
    for start, value, end, looks_like in iter_json_object_spans_with_repair(text):
        if value is None:
            if looks_like and start >= last_any_end:
                failed_tail = True
            continue
        label_kind = _event_label_kind(text[prev_end:start])
        prev_end = end
        candidate = _unwrap_result(value)
        if candidate is None:
            continue
        last_any, last_any_end = candidate, end
        failed_tail = False
        if _plan_score(candidate) > 0:
            substantive, substantive_end, substantive_kind = candidate, end, label_kind
        elif _is_explicit_empty(candidate):
            explicit_empty, explicit_empty_end, explicit_empty_kind = (
                candidate,
                end,
                label_kind,
            )
    if failed_tail:
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    if substantive is not None and explicit_empty is not None:
        if explicit_empty_end > substantive_end:
            # 后置的显式空结果按位置胜出——除非它是带「尾注：/补充：」标签
            # 的局部说明且更早的实质候选不是回显示例：真实提取保留。形状
            # 相同（计数有无、字段表是否完整）不再参与判定，避免示例回显
            # 与「信息不足」终稿在不同变体下反复互相顶掉
            if explicit_empty_kind == "supplement" and substantive_kind != "example":
                chosen, chosen_end = substantive, substantive_end
            else:
                chosen, chosen_end = explicit_empty, explicit_empty_end
        else:
            chosen, chosen_end = substantive, substantive_end
    elif substantive is not None:
        chosen, chosen_end = substantive, substantive_end
    elif explicit_empty is not None:
        chosen, chosen_end = explicit_empty, explicit_empty_end
    elif last_any is not None:
        chosen, chosen_end = last_any, last_any_end
    else:
        return None
    if has_unclosed_json_object(text[chosen_end:]):
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    return chosen



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
        if isinstance(x, list):
            content = x[-1].content if x else "{}"
        else:
            content = x.content

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

            # 模型可能先回显 prompt 里的示例对象再给正式结果：按位置选最后
            # 一个「实质结果」（尾注元信息对象不顶替完整提取），末尾截断时
            # 按错误降级而不是把回显示例当成真实事项写成行程
            result = _select_event_result(text)
            if result is None:
                raise ValueError("模型输出中没有可解析的事项结果对象")
            result = _normalize_result_dates(result)
        except Exception as e:
            if is_retriable_error(e) or is_permanent_llm_error(e):
                # 瞬时模型错误交给编排层重试；401/402/403/404 等永久错误
                # 同样穿透并计入熔断，不能吞成 error JSON
                raise
            logger.error(f"Event collection failed: {e}")
            result = {
                "missing_info": ["所有信息"],
                "extracted_count": 0,
                "error": str(e)
            }

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
