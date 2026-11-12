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

from utils.json_parser import (
    extract_json_from_async_response,
    first_json_array_with_keys,
    first_json_object_with_keys,
    last_json_object_with_keys_strict,
)

logger = logging.getLogger(__name__)


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
    """取出结果对象；兼容 ``{"data": {preferences...}}`` 包装。"""
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    candidate = (
        data
        if isinstance(data, dict)
        and ("preferences" in data or "has_preferences" in data)
        else obj
    )
    if "preferences" in candidate or "has_preferences" in candidate:
        return candidate
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
                user_query = content
        else:
            user_query = str(content)

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

            # 清理文本，移除markdown代码块标记
            text = text.strip()
            text = text.removeprefix('```json')
            text = text.removeprefix('```')
            text = text.removesuffix('```')
            text = text.strip()

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
                        "preferences": items,
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
                        "preferences": items,
                        "has_preferences": bool(items),
                    }
            if result is None:
                raise ValueError("模型输出中没有可解析的偏好结果对象")
        except Exception as e:
            logger.error(f"Preference collection failed: {e}")
            result = {"has_preferences": False, "error": str(e)}

        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
