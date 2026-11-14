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
from typing import Any

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
    # result_formatter / CLI 都兼容「missing_info 是单个字符串」的形态
    missing = obj.get("missing_info")
    if isinstance(missing, str):
        missing = [missing]
    return isinstance(missing, list) and any(
        isinstance(item, str) and item.strip() for item in missing
    )


def _is_counted_empty(obj: dict) -> bool:
    """带计数（extracted_count == 0）的显式空结果。"""
    count = obj.get("extracted_count")
    return isinstance(count, int) and not isinstance(count, bool) and count == 0


def _has_full_event_schema(obj: dict) -> bool:
    """结果对象是否带完整字段表（所有核心字段键都存在，值可为 null）。

    prompt 要求「对应字段设为 null」，模型按此输出的「信息不足」终稿
    带全套字段且 missing_info 非空——与本轮结果同形，是最终表态。
    """
    return all(key in obj for key in _CORE_FIELDS)


def _is_authoritative_empty(obj: dict) -> bool:
    """形状上可确定「本轮最终表态：没有提取到信息」的空结果：带计数
    （extracted_count == 0），或字段表完整、没有任何核心字段有值且声明了
    缺失信息（全 null 终稿）。无计数、不完整字段表的局部 missing_info
    尾注不在其列，按补充说明处理（保留更早的完整提取）。"""
    if _is_counted_empty(obj):
        return True
    return (
        _has_full_event_schema(obj)
        and _plan_score(obj) == 0
        and _is_explicit_empty(obj)
    )


# 候选标签：取候选前最后一个非空、非围栏行；行内只允许「一个短语」（可带
# 引导词「以下是/本次」等），剔除加粗 / 标题 / 方括号装饰。示例短语按前缀
# 识别（示例、示例1、示例（北京行程）、输出示例…），叙述句不算。
_EXAMPLE_LABELS = frozenset({"输出示例", "输出样例", "格式示例", "示例输出"})
_SUPPLEMENT_LABELS = frozenset(
    {
        "补充",
        "补充说明",
        "补充信息",
        "补充内容",
        "补充事项",
        "补充结果",
        "补充行程",
        "补充安排",
        "尾注",
        "备注",
        "备注说明",
        "附注",
        "注",
        "注释",
        "提示",
        "温馨提示",
        "友情提示",
        "小贴士",
        "贴士",
        "注意",
        "注意事项",
        "说明",
        "须知",
    }
)
_LEAD_INS = ("以下是", "以下为", "下面是", "如下是", "这里是", "这是", "本次")


_PAREN_GROUP_RE = re.compile(r"（[^（）]*）|\([^()]*\)")
_PAREN_EXAMPLE = frozenset(
    {"示例", "例如", "样例", "示例输出", "示例结果", "示例格式", "输出示例"}
)


def _paren_example_marker(line: str) -> bool:
    """括号里自称示例的短标签（「结果（示例）：」「（示例）：」）。

    去掉括号组后剩余文本不含句子标点（叙述句不算），且某个括号组的
    内容精确命中示例短语——「按照（示例）格式，结果如下：」不算示例。"""
    outside = _PAREN_GROUP_RE.sub("", line)
    if any(ch in outside for ch in "，。；、！？,;!?…"):
        return False
    for group in _PAREN_GROUP_RE.findall(line):
        content = re.sub(r"[#*`【】\[\]（）>\-•\s:：=]", "", group[1:-1])
        if content in _PAREN_EXAMPLE:
            return True
    return False


def _event_label_phrase(label: str) -> str | None:
    line = ""
    for item in label.splitlines():
        cleaned = re.sub(r"```+\s*\w*\s*$", "", item).strip()
        if cleaned:
            line = cleaned
    if not line:
        return None
    cleaned_line = line
    while True:
        stripped = _PAREN_GROUP_RE.sub("", cleaned_line)
        if stripped == cleaned_line:
            break
        cleaned_line = stripped
    if not re.sub(r"[#*`【】\[\]（）>\-•\s:：=]", "", cleaned_line):
        # 整行就是括号里的标签（「（尾注）：」「(示例)：」）：取括号内容
        match = _PAREN_GROUP_RE.search(line)
        if match:
            cleaned_line = match.group(0)[1:-1]
    decoration_free = re.sub(r"[#*`【】\[\]（）>\-•\s]", "", cleaned_line)
    parts = [
        part
        for part in re.split(r"[:：=]", decoration_free.rstrip(":：="))
        if part
    ]
    if not parts:
        return None
    if any(part not in _LEAD_INS for part in parts[:-1]):
        # 行内还有其它短语（示例：正式结果、说明：…）：不是纯标签行
        return None
    phrase = parts[-1]
    changed = True
    while changed:
        changed = False
        for lead in _LEAD_INS:
            if phrase.startswith(lead) and len(phrase) > len(lead):
                phrase = phrase[len(lead):]
                changed = True
                break
    return phrase


_SENTENCE_PUNCT = "，。；、！？,;!?…"


def _is_example_phrase(phrase: str | None) -> bool:
    """示例短语的短标签约束：含句子标点的叙述句（「示例中的日期只是
    占位，真实结果如下」）不算示例标签。"""
    if not phrase:
        return False
    if any(ch in phrase for ch in _SENTENCE_PUNCT):
        return False
    if phrase in _EXAMPLE_LABELS:
        return True
    return len(phrase) <= 8 and phrase.startswith(("示例", "例如", "样例"))


def _event_label_is_example(label: str) -> bool:
    line = ""
    for item in label.splitlines():
        cleaned = re.sub(r"```+\s*\w*\s*$", "", item).strip()
        if cleaned:
            line = cleaned
    if line and _paren_example_marker(line):
        return True
    return _is_example_phrase(_event_label_phrase(label))


def _iter_array_spans(text: str):
    decoder = json.JSONDecoder()
    index = text.find("[")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("[", index + 1)
            continue
        if isinstance(value, list):
            yield index, end
        index = text.find("[", max(end, index + 1))


def _inside_example_array(text: str, start: int) -> bool:
    """对象位于带「示例：」标签的 JSON 数组内：继承示例标记（数组换行
    书写时「[」行会吞掉元素的标签）。"""
    for array_start, array_end in _iter_array_spans(text):
        if array_start < start < array_end:
            return _event_label_is_example(text[:array_start])
    return False


# missing_info 条目 → 提取字段：用于判断「信息不足」声明与实质提取是否矛盾
# （缺的字段恰恰是提取里已有值的字段 = 示例回显之后的终态，空结果才可信；
# 缺的是提取里本来就没有的字段 = 补充说明，保留提取）。
_MISSING_FIELD_RULES = (
    (("返程地", "返回地", "回程地"), ("return_location",)),
    (("返程日期", "回程日期", "返回日期", "结束日期"), ("end_date",)),
    (("出发日期", "开始日期", "出行日期", "行程日期"), ("start_date",)),
    (("出发地", "出发城市", "起点"), ("origin",)),
    (("目的地", "目的城市", "终点"), ("destination",)),
    (("事由", "目的"), ("trip_purpose",)),
    (("天数", "时长"), ("duration_days",)),
)
# 泛化时点词（具体时间 / 出发时间 / 返程航班…）：只说明「时刻/班次」未定，
# 不是字段级缺失声明，不参与矛盾判定
_GENERIC_TIME_WORDS = ("时间", "时刻", "钟点", "航班", "车次", "班次", "几点")
_ALL_FIELDS_WORDS = ("所有信息", "全部信息", "全部字段", "所有字段", "必要信息", "关键信息")


def _missing_contradicts_substantive(
    missing_items: list[Any] | str, substantive: dict
) -> bool:
    if isinstance(missing_items, str):
        missing_items = [missing_items]
    for item in missing_items:
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.strip()
        if any(word in text for word in _GENERIC_TIME_WORDS) and text not in (
            "出发日期",
            "开始日期",
            "出行日期",
            "行程日期",
            "返程日期",
            "回程日期",
            "返回日期",
            "结束日期",
        ):
            continue
        # 全量缺失声明按「整条即该短语」识别：「出发日期等关键信息」
        # 这类指向具体字段的补充说明不能被子串匹配误判
        if text in _ALL_FIELDS_WORDS:
            if _plan_score(substantive) > 0:
                return True
            continue
        for keywords, fields in _MISSING_FIELD_RULES:
            if any(word in text for word in keywords):
                if any(_has_event_value(substantive.get(field)) for field in fields):
                    return True
                break
    return False


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
    substantives: list[tuple[dict, int, bool]] = []
    explicit_empty: dict | None = None
    explicit_empty_end = -1
    last_empty_any: dict | None = None
    last_empty_any_end = -1
    authoritative_empty: dict | None = None
    authoritative_end = -1
    example_empty: dict | None = None
    example_empty_end = -1
    failed_tail = False
    prev_end = 0
    for start, value, end, looks_like in iter_json_object_spans_with_repair(text):
        if value is None:
            if looks_like and start >= last_any_end:
                failed_tail = True
            continue
        label_segment = text[prev_end:start]
        label_phrase = _event_label_phrase(label_segment)
        label_example = _event_label_is_example(label_segment)
        if not label_example:
            label_example = _inside_example_array(text, start)
        prev_end = end
        candidate = _unwrap_result(value)
        if candidate is None:
            continue
        last_any, last_any_end = candidate, end
        failed_tail = False
        if _plan_score(candidate) > 0:
            substantives.append(
                (candidate, end, label_example, label_phrase in _SUPPLEMENT_LABELS)
            )
        elif _is_explicit_empty(candidate):
            if label_example:
                # 回显的空结果示例同样不参与竞争（与实质候选的示例防护对称）
                example_empty, example_empty_end = candidate, end
                continue
            last_empty_any, last_empty_any_end = candidate, end
            if label_phrase not in _SUPPLEMENT_LABELS:
                # 带「尾注：/补充：」标签的局部说明不覆盖更早的矛盾终稿，
                # 也不参与矛盾判定；带补充标签的 extracted_count=0 空模板
                # 同样不能覆盖完整提取
                explicit_empty = candidate
                explicit_empty_end = end
                if _is_authoritative_empty(candidate):
                    authoritative_empty, authoritative_end = candidate, end
    if failed_tail:
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    # 回显示例与带补充标签的实质候选降级；只剩回显示例时宁可报错，
    # 也不把示例字段当成真实提取写库
    strong = [item for item in substantives if not item[2] and not item[3]]
    if not strong:
        strong = [item for item in substantives if not item[2]]
    substantive, substantive_end, substantive_is_example = (
        strong[-1][:3] if strong else (None, -1, False)
    )
    if substantive is not None:
        if authoritative_empty is not None and authoritative_end > substantive_end:
            # 形状上确定是本轮终稿（计数 0 / 全 null 字段表）的权威空结果
            # 不会被子孙补充尾注替换
            chosen, chosen_end = authoritative_empty, authoritative_end
        elif explicit_empty is not None and explicit_empty_end > substantive_end:
            # 后置空结果按位置胜出：缺失声明与实质提取矛盾（缺的字段提取
            # 里明明有值 = 示例回显之后的终态），或更早的实质候选是回显
            # 示例。带标签的局部说明不走到这里（见下一条兜底）
            missing_items = explicit_empty.get("missing_info") or []
            if substantive_is_example or _missing_contradicts_substantive(
                missing_items, substantive
            ):
                chosen, chosen_end = explicit_empty, explicit_empty_end
            else:
                chosen, chosen_end = substantive, substantive_end
        elif substantive_is_example and last_empty_any is not None:
            # 回显示例存在任何缺失声明（含带标签的、位置无关的）时，绝不
            # 让示例字段被当成真实提取写库
            chosen, chosen_end = last_empty_any, last_empty_any_end
        else:
            chosen, chosen_end = substantive, substantive_end
    elif explicit_empty is not None:
        chosen, chosen_end = explicit_empty, explicit_empty_end
    elif last_empty_any is not None:
        chosen, chosen_end = last_empty_any, last_empty_any_end
    elif example_empty is not None:
        chosen, chosen_end = example_empty, example_empty_end
    elif substantives:
        # 只有回显示例候选：宁可报错也不写示例数据
        return None
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
