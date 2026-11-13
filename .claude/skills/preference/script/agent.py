"""
偏好智能体
职责：收集用户的长期偏好
如"我的家在XXX"、"我喜欢XXX酒店"
"""
import asyncio
import json
import logging
import os
import sys

from agentscope.agent import AgentBase
from agentscope.message import Msg

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from utils.llm_resilience import is_retriable_error
from utils.preference_values import (
    EXPLANATORY_PREF_KEYS,
    has_preference_value,
    is_manageable_pref_type,
    merge_preference_value,
)
from utils.user_query import extract_user_query
from utils.json_parser import (
    extract_json_from_async_response,
    first_json_array_with_keys,
    first_json_object_with_keys,
    last_json_object_with_keys_strict,
)

logger = logging.getLogger(__name__)


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
    """按 type 合并条目，避免同类型逐条覆盖只剩最后一条。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "type" not in item and "value" not in item:
            pairs = [
                (key, value)
                for key, value in item.items()
                if key not in EXPLANATORY_PREF_KEYS
                and is_manageable_pref_type(key)
                and has_preference_value(value)
            ]
            if len(pairs) == 1:
                item = {"type": pairs[0][0], "value": pairs[0][1]}
            else:
                continue
        pref_type = str(item.get("type") or "").strip()
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
            if is_append or entry.get("action") == "append":
                entry["action"] = "append"
            elif had_action or "action" in entry:
                entry["action"] = "replace"
            else:
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


def _last_preference_array(text: str) -> tuple[int, list[dict]] | None:
    """文本中最后一个「元素形如偏好」的完整 JSON 数组（起始下标 + 元素）。

    ``json_array_start`` 的散文判定（数组前出现对象即视为前言）不适用于
    「先回显示例对象、再输出正式裸数组」：这里直接扫描所有能整体解析的
    数组，取最后一个元素含 type / value 的，由调用方与对象候选比较位置。
    """
    decoder = json.JSONDecoder()
    best: tuple[int, list[dict]] | None = None
    index = text.find("[")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("[", index + 1)
            continue
        if isinstance(value, list) and value:
            items = [item for item in value if isinstance(item, dict)]
            if items and any(
                "type" in item or "value" in item for item in items
            ):
                best = (index, items)
        index = text.find("[", max(end, index + 1))
    return best


def _as_preference_result(obj: object) -> dict | None:
    """取出结果对象；兼容 ``{"data": {preferences...}}`` 包装及常见偏差形状。"""
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    candidate = (
        data
        if isinstance(data, dict)
        and ("preferences" in data or "has_preferences" in data)
        else obj
    )
    preferences = candidate.get("preferences")
    if isinstance(preferences, list):
        return {
            **candidate,
            "preferences": _normalize_preference_items(preferences),
        }
    if "preferences" in candidate or "has_preferences" in candidate:
        return candidate
    # 模型常见单对象偏差：{"type": ..., "value": ..., "action": ...}
    if "type" in obj and "value" in obj:
        return {
            "preferences": _normalize_preference_items([obj]),
            "has_preferences": True,
        }
    # 旧格式 {类型: 值} 字典（只挑可管理的偏好键）
    legacy = [
        {"type": key, "value": value}
        for key, value in obj.items()
        if key not in EXPLANATORY_PREF_KEYS
        and is_manageable_pref_type(key)
        and has_preference_value(value)
    ]
    if legacy:
        return {
            "preferences": _normalize_preference_items(legacy),
            "has_preferences": True,
        }
    return None


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
        content = x.content if not isinstance(x, list) else x[-1].content

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

            # 模型可能先复述示例对象 / 输出示例再给正式结果：优先取
            # 「位置在示例之后」的正式结果——裸数组和结果对象两种形状
            # 都可能出现，分别取各自最后一个候选再比较先后
            result = None
            array_candidate = _last_preference_array(text)
            if array_candidate is not None:
                start, items = array_candidate
                # 示例对象里的 preferences 数组同样满足形状：只有它排在
                # 所有含结果键的对象之后时才当作正式裸数组
                object_hint = max(
                    text.rfind('"has_preferences"'),
                    text.rfind('"preferences"'),
                )
                if object_hint == -1 or start > object_hint:
                    result = {
                        "preferences": _normalize_preference_items(items),
                        "has_preferences": bool(items),
                    }
            if result is None:
                result = _as_preference_result(
                    last_json_object_with_keys_strict(
                        text, ("has_preferences", "preferences", "data")
                    )
                )
            if result is None:
                # 没有任何含结果键的完整对象：退到第一个含结果键的对象
                # （尾逗号等小瑕疵由修复路径兜底）。不能回退到「第一个
                # 对象」——prompt 回显的示例通常排在最前，正式结果被
                # max_tokens 截断时会把示例偏好当成真实结果写库
                result = _as_preference_result(
                    first_json_object_with_keys(
                        text, ("has_preferences", "preferences", "data")
                    )
                )
            if result is None:
                # 前言带引用标记（参考[1]）时 starts 判定失败、对象扫描
                # 也找不到结果键：最后尝试「元素含 type/value 的数组」
                bare_items = first_json_array_with_keys(
                    text, ("type", "value")
                )
                if bare_items:
                    items = [
                        item for item in bare_items if isinstance(item, dict)
                    ]
                    result = {
                        "preferences": _normalize_preference_items(items),
                        "has_preferences": bool(items),
                    }
            if result is None:
                raise ValueError("模型输出中没有可解析的偏好结果对象")
        except Exception as e:
            if is_retriable_error(e):
                raise
            logger.error(f"Preference collection failed: {e}")
            result = {"has_preferences": False, "error": str(e)}

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
