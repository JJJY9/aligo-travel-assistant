"""
偏好智能体
职责：收集用户的长期偏好
如"我的家在XXX"、"我喜欢XXX酒店"
"""
import asyncio
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
from utils.preference_values import (
    EXPLANATORY_PREF_KEYS,
    has_preference_value,
    is_manageable_pref_type,
    merge_preference_value,
)
from utils.user_query import extract_user_query
from utils.json_parser import (
    extract_json_from_async_response,
    has_unclosed_json_object,
    iter_json_object_spans_with_repair,
)

logger = logging.getLogger(__name__)

# 结果包装键与说明键：旧格式字典扫描必须跳过（{"data": {...}} 的 data
# 不是偏好类型，否则会落库一条 type="data" 的垃圾偏好）
_PREF_NON_TYPE_KEYS = frozenset(EXPLANATORY_PREF_KEYS | {"data", "result", "output", "preferences"})



def _has_unclosed_array(text: str) -> bool:
    """文本里是否存在未闭合的 JSON 数组（模型输出被 max_tokens 截断）。

    回显示例对象在前、正式裸数组在后且被截断时，示例对象自带的
    preferences 数组会绕过对象截断守卫被当成正式结果——必须先拒绝这种
    截断输出，让技能返回明确错误而不是把示例偏好写进长期记忆。
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
    return depth > 0


def _normalize_preference_items(items: list) -> list[dict]:
    """按 type 合并条目，避免同类型逐条覆盖只剩最后一条。

    同类型多条合并规则：值按出现顺序合并；只要组内出现过明确的 replace，
    整组最终就是 replace——此前「任一条是 append 就把整组改成 append」，
    用户要求替换掉的旧值会被 append 语义保留下来。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "type" not in item and "value" not in item:
            pairs = [
                (key, value)
                for key, value in item.items()
                if key not in _PREF_NON_TYPE_KEYS
                and is_manageable_pref_type(key)
                and has_preference_value(value)
            ]
            if len(pairs) == 1:
                item = {"type": pairs[0][0], "value": pairs[0][1]}
            else:
                continue
        # type 必须是字符串：此前 str() 强转让数字 / 容器类型绕过可管理
        # 校验，垃圾键（如 "5"）被落库、真实类型丢失
        pref_type = item.get("type")
        if not isinstance(pref_type, str):
            continue
        pref_type = pref_type.strip()
        value = item.get("value")
        if not is_manageable_pref_type(pref_type) or not has_preference_value(
            value
        ):
            continue
        action = str(item.get("action") or "replace").strip().lower()
        is_append = action in {"append", "add", "追加"}
        had_action = "action" in item
        if pref_type in merged:
            entry = merged[pref_type]
            entry["value"] = merge_preference_value(entry.get("value"), value)
            if is_append:
                # 此前已有 replace：保持 replace（整组替换存储，含本批值）
                if entry.get("action") != "replace":
                    entry["action"] = "append"
            elif had_action or entry.get("action") == "replace":
                # 只有「本条显式 replace」或「组内此前已显式 replace」才整组
                # 覆盖；无 action 的后续条目不是显式 replace，不能把此前
                # append 的组翻成覆盖（会删掉存储里的既有偏好）
                entry["action"] = "replace"
            elif entry.get("action") != "append":
                # 无 action 条目继承组内既有动作：append 组保持 append；
                # 此前也没有任何 action 时保持无 action（默认覆盖）
                entry.pop("action", None)
        else:
            entry = {"type": pref_type, "value": value}
            if is_append:
                entry["action"] = "append"
            elif had_action:
                entry["action"] = "replace"
            merged[pref_type] = entry
            order.append(pref_type)
    return [merged[pref_type] for pref_type in order]


def _iter_preference_arrays(text: str):
    """按位置产出「元素形如偏好」的完整 JSON 数组（起始, 结束, 元素列表）。

    空数组 ``[]`` 也是候选（正式结果表示「没有新偏好」）；元素全部非
    对象或不含 type / value 的数组（引用标记 ``[1]``、说明 ``[注]``）
    不是候选。
    """
    decoder = json.JSONDecoder()
    index = text.find("[")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("[", index + 1)
            continue
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if not value or (
                items and any("type" in item or "value" in item for item in items)
            ):
                yield index, end, items
        index = text.find("[", max(end, index + 1))


def _result_from_preferences(candidate: dict, preferences: object) -> dict | None:
    """把 preferences 字段的几种形态规范成列表结果；无法解析返回 None。"""
    if isinstance(preferences, dict):
        preferences = [preferences]
    elif isinstance(preferences, str):
        # 模型可能把数组序列化成字符串：能解析就恢复，否则按无结果处理
        try:
            parsed = json.loads(preferences)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return None
        preferences = parsed
    elif preferences is None:
        return {**candidate, "preferences": [], "has_preferences": False}
    elif not isinstance(preferences, list):
        return None
    items = [item for item in preferences if isinstance(item, dict)]
    normalized = _normalize_preference_items(items)
    result = {**candidate, "preferences": normalized}
    if not normalized:
        # 条目全不可管理时不能宣称「有偏好」：编排层会按空列表展示成功
        result["has_preferences"] = False
    return result


def _as_preference_result(obj: object, _depth: int = 0) -> dict | None:
    """取出结果对象；兼容 data 包装、单对象与旧格式字典。

    找不到可解析的偏好形状时返回 None（调用方据此继续找其它候选或报错），
    不能把形状不对的对象当成功结果返回——否则非法 preferences 值会以
    「成功」流进编排层，用户偏好被静默丢弃。
    """
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if (
        isinstance(data, dict)
        and _depth < 2
        and "preferences" not in data
        and "has_preferences" not in data
    ):
        # {"data": {"home_location": "上海"}}：data 里是旧格式 / 单对象形状
        inner = _as_preference_result(data, _depth + 1)
        if inner is not None:
            return inner
    candidate = (
        data
        if isinstance(data, dict)
        and ("preferences" in data or "has_preferences" in data)
        else obj
    )
    if "preferences" in candidate:
        return _result_from_preferences(candidate, candidate.get("preferences"))
    if "has_preferences" in candidate:
        return candidate
    # 模型常见单对象偏差：{"type": ..., "value": ..., "action": ...}
    if "type" in obj and "value" in obj:
        items = _normalize_preference_items([obj])
        if items:
            return {"preferences": items, "has_preferences": True}
        return None
    # 旧格式 {类型: 值} 字典（只挑可管理的偏好键，包装 / 说明键除外）
    legacy = [
        {"type": key, "value": value}
        for key, value in obj.items()
        if key not in _PREF_NON_TYPE_KEYS
        and is_manageable_pref_type(key)
        and has_preference_value(value)
    ]
    if legacy:
        return {
            "preferences": _normalize_preference_items(legacy),
            "has_preferences": True,
        }
    return None


def _looks_result_keyed(value: dict) -> bool:
    """对象是否「形如偏好结果」（含 preferences / has_preferences 键）。"""
    data = value.get("data")
    if isinstance(data, dict) and (
        "preferences" in data or "has_preferences" in data
    ):
        return True
    return "preferences" in value or "has_preferences" in value


# 正式空结果前的标签：取数组前最后一个非空、非围栏行；行内只允许
# 「一个短语」（可带引导词「以下是/本次」等），剔除加粗 / 标题 / 方括号
# 装饰。结果短语白名单覆盖常见终稿写法（结果如下 / 偏好 / 输出结果…）；
# 说明 / 指令句（返回：若无偏好时输出：）不算。
_RESULT_LABELS = frozenset(
    {
        "正式结果",
        "正式",
        "结果",
        "结果如下",
        "结论",
        "偏好",
        "本次结果",
        "本次输出结果",
        "最终结果",
        "最终结果如下",
        "最终输出",
        "输出结果",
        "最终",
    }
)
_LEAD_INS = ("以下是", "以下为", "下面是", "如下是", "这里是", "这是", "本次")


def _label_phrase(line: str) -> str | None:
    decoration_free = re.sub(r"[#*`【】\[\]（）>\-•\s]", "", line)
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


_RESULT_SUFFIXES = ("结果", "偏好", "结论")


def _is_result_phrase(phrase: str) -> bool:
    """短语是否结果标签：白名单，或以「结果 / 偏好 / 结论」收尾的短短语
    （容忍「如下 / 为 / 是」后缀）——「提取结果」「正式结果如下」「无新
    偏好」都算，「返回」「输出」等指令词不算。"""
    if phrase in _RESULT_LABELS:
        return True
    stripped = phrase
    for suffix in ("如下", "为", "是"):
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return len(stripped) <= 8 and stripped.endswith(_RESULT_SUFFIXES)


def _empty_array_is_formal(text: str, start: int, end: int) -> bool:
    """空数组是本轮正式空结果吗？

    只认两种形态：整个输出就是这个数组，或数组前最后一个非空、非围栏
    行的唯一短语是结果标签（``正式结果：[]`` / ``结果如下：[]``）。标签行
    里混入任何其它短语（条件说明、指令、「返回：」「输出：」等）即判为
    示例，避免说明散文里的 ``[]`` 按位置顶掉已解析的真实偏好。正式空
    结果之后的说明散文（换行括号注释 / 句号）不影响判定。"""
    prefix = text[:start]
    if not prefix.strip():
        return True
    cleaned_lines: list[str] = []
    for item in prefix.splitlines():
        cleaned = re.sub(r"```+\s*\w*\s*$", "", item).strip()
        if cleaned:
            cleaned_lines.append(cleaned)
    if not cleaned_lines:
        return True
    phrase = _label_phrase(cleaned_lines[-1])
    if not phrase or not _is_result_phrase(phrase):
        return False
    if len(cleaned_lines) >= 2 and cleaned_lines[-2].rstrip().endswith(
        ("：", ":")
    ):
        # 折行说明：「（…按格式返回：」换行后跟「结果：[]）」——上一行
        # 以冒号收尾，标签行是条件说明的续行，不是独立的结果标签
        return False
    return True


def _select_preference_result(text: str) -> dict | None:
    """在完整输出里按位置选出偏好结果（后出现的正式结果胜过回显示例）。

    候选包括显式结果对象（preferences / has_preferences）、data 包装、
    裸单对象 {type, value} 与旧格式字典——此前只扫描含结果键的对象，
    后置的其它形状永远选不中、回显示例反而被当成正式结果写入长期记忆。
    裸数组与对象按起始位置比较先后；数组若位于某个对象内部（示例对象
    自带的 preferences 数组）不算独立候选。
    """
    spans: list[tuple[int, int]] = []
    objects: list[tuple[int, int, dict]] = []
    unparsable_keyed: list[int] = []
    failed_tail = False
    last_end = -1
    for start, value, end, looks_like in iter_json_object_spans_with_repair(text):
        if value is None:
            if looks_like and start >= last_end:
                # 「像结果但解析不了」的尾片段：正式结果可能被截断，不能
                # 回退到更早的示例对象
                failed_tail = True
            continue
        spans.append((start, end))
        last_end = max(last_end, end)
        normalized = _as_preference_result(value)
        if normalized is not None:
            objects.append((start, end, normalized))
            failed_tail = False
        elif _looks_result_keyed(value):
            # 形如结果却解析不了（preferences 是非法字符串等）：记录位置，
            # 选择时不回退到它之前的回显示例
            unparsable_keyed.append(start)
    if failed_tail:
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    arrays = [
        (start, end, items)
        for start, end, items in _iter_preference_arrays(text)
        if not any(span_start <= start < span_end for span_start, span_end in spans)
    ]
    array = None
    for start, end, items in arrays:
        if not items and not _empty_array_is_formal(text, start, end):
            continue
        array = (start, end, items)
    chosen: tuple[dict, int, int] | None = None
    if objects:
        obj_start, obj_end, obj = objects[-1]
        chosen = (obj, obj_start, obj_end)
        if array is not None and array[0] > obj_start:
            chosen = None
    if chosen is None and array is not None:
        chosen = (
            {
                "preferences": _normalize_preference_items(array[2]),
                "has_preferences": bool(array[2]),
            },
            array[0],
            array[1],
        )
    if chosen is None:
        return None
    result, result_start, result_end = chosen
    if any(start > result_start for start in unparsable_keyed):
        # 正式结果（形如偏好结果的对象）解析失败：报错而不是把更早的
        # 回显示例写进长期记忆
        raise ValueError("模型输出中的偏好结果无法解析，拒绝回退到更早的候选")
    if has_unclosed_json_object(text[result_end:]):
        raise ValueError("模型输出在结果对象处被截断，拒绝回退到更早的候选")
    return result



class PreferenceAgent(AgentBase):
    """偏好智能体"""

    def __init__(self, name: str = "PreferenceAgent", model=None, memory_manager=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.memory_manager = memory_manager
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

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
            # 顶层 / context 可能不是对象（数组 / 数字 / null）：直接 .get 会
            # AttributeError 绕过上面的兜底，按纯文本输入处理
            if isinstance(data, dict):
                context = data.get("context")
                if not isinstance(context, dict):
                    context = {}
                # 优先使用用户本轮原话：只从用户自己的表述中抽取偏好，
                # 避免把上下文或助手建议（如行程里提到的舱位）误记为用户偏好
                user_query = (
                    context.get("user_query")
                    or context.get("rewritten_query", "")
                    or str(data)
                )
            else:
                user_query = extract_user_query(content)
        else:
            user_query = extract_user_query(content)

        # 获取当前已保存的偏好
        current_preferences = {}
        if self.memory_manager:
            # 偏好查询是阻塞 IO（JSON 文件 / PostgreSQL 网络往返）：
            # 放到线程池执行，留在事件循环线程会冻结同进程所有 SSE 流
            try:
                current_preferences = await asyncio.to_thread(
                    self.memory_manager.long_term.get_preference
                )
            except Exception as e:  # noqa: BLE001 - 存储抖动按空偏好继续抽取
                logger.warning(f"Failed to load current preferences: {e}")
                current_preferences = {}

        # 格式化当前偏好，便于展示
        current_prefs_str = json.dumps(current_preferences, ensure_ascii=False, indent=2)

        # 动态读取 Prompt 指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("preference")
        if not skill_instruction:
            skill_instruction = "请分析用户的偏好。"

        prompt = f"""你是用户偏好分析专家，负责提取用户的长期偏好信息。

【当前已保存的用户偏好】
{current_prefs_str}

【新的用户输入】
{user_query}

【任务说明】
{skill_instruction}

请直接输出JSON：
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

            if _has_unclosed_array(text):
                # 正式裸数组被截断：不能让回显示例对象里的数组顶替
                raise ValueError("模型输出被截断（数组未闭合）")

            # 模型可能先复述示例对象 / 输出示例再给正式结果：按位置取
            # 「最后一个可解析的偏好结果」，示例对象 / 示例数组不会顶掉
            # 后置的正式结果；没有候选时报错走错误降级
            result = _select_preference_result(text)
            if result is None:
                raise ValueError("模型输出中没有可解析的偏好结果对象")
        except Exception as e:
            if is_retriable_error(e) or is_permanent_llm_error(e):
                # 瞬时错误交给编排层重试；401/402/403/404 等永久错误
                # 同样穿透，由外层上抛并计入熔断——吞成 error JSON 会让
                # 密钥失效 / 余额耗尽时熔断器永远打不开
                raise
            logger.error(f"Preference collection failed: {e}")
            result = {"has_preferences": False, "error": str(e)}

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
