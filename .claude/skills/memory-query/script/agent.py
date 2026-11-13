"""
记忆查询智能体 MemoryQueryAgent
职责：基于用户的长期记忆回答历史相关问题

核心功能：
1. 查询用户旅行历史（trip_history）
2. 查询用户偏好（preferences）
3. 查询历史对话记录（chat_history）
4. 使用LLM基于记忆生成自然语言回答

适用场景：
- 用户询问："我去过哪些地方？"
- 用户询问："我之前说过什么偏好？"
- 用户询问："我上次去北京是什么时候？"
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

from utils.log_redact import redact_paths
from utils.llm_resilience import is_permanent_llm_error, is_retriable_error
from utils.json_parser import extract_json_from_async_response
from utils.preference_values import has_preference_value
from utils.user_query import extract_user_query

logger = logging.getLogger(__name__)

# 记忆注入 prompt 的字符预算：长期用户可能有上千条行程，历史字段也可能
# 被备份导入写得很长；无预算拼接会让 prompt 超出模型上下文、查询永久
# 失败。按「最近优先」装入预算，被省略的条目明确注记。
_MAX_TRIP_TEXT_CHARS = 12000
_MAX_TRIP_LINE_CHARS = 300
_MAX_PREF_TEXT_CHARS = 4000
_MAX_PREF_VALUE_CHARS = 500


class MemoryQueryAgent(AgentBase):
    """记忆查询智能体 - 基于长期记忆回答用户问题"""

    def __init__(
        self,
        name: str = "MemoryQueryAgent",
        model=None,
        memory_manager=None,
        **kwargs
    ):
        super().__init__()
        self.name = name
        self.model = model
        self.memory_manager = memory_manager
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

    async def reply(self, x: Msg | list[Msg] | None = None) -> Msg:
        """
        处理记忆查询请求

        Args:
            x: 输入消息，包含用户查询和上下文

        Returns:
            Msg: 基于记忆的回答
        """
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 解析输入
        if isinstance(x, list):
            input_content = x[-1].content if x else "{}"
        else:
            input_content = x.content

        try:
            input_data = json.loads(input_content) if isinstance(input_content, str) else input_content
        except json.JSONDecodeError:
            input_data = None
        if not isinstance(input_data, dict):
            # 顶层数组 / 数字 / null / content block 列表：拼出可读文本作为
            # 查询（str() 会得到 Python repr，把 block 结构当成问题原文）
            input_data = {"context": {"rewritten_query": extract_user_query(input_content)}}

        # 获取用户查询
        context = input_data.get("context")
        if not isinstance(context, dict):
            context = {}
        user_query = context.get("rewritten_query", "")
        if not isinstance(user_query, str) or not user_query.strip():
            user_query = ""
            # 尝试从 recent_dialogue 获取最后一条用户消息
            recent_dialogue = context.get("recent_dialogue")
            if not isinstance(recent_dialogue, list):
                recent_dialogue = []
            for msg in reversed(recent_dialogue):
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                content_text = msg.get("content")
                if isinstance(content_text, str) and content_text.strip():
                    user_query = content_text
                    break

        # 同会话追问需要当前会话的最近对话，不能只看其它会话的长期摘要
        raw_dialogue = context.get("recent_dialogue")
        current_dialogue = (
            [
                item
                for item in raw_dialogue[-6:]
                if isinstance(item, dict)
                and str(item.get("content") or "").strip()
            ]
            if isinstance(raw_dialogue, list)
            else []
        )
        def _trim_dialogue(value: object, limit: int = 1200) -> str:
            text = str(value or "")
            return text if len(text) <= limit else text[:limit] + "…"

        # 助手消息实际是整轮编排结果 JSON（daily_plans / 各技能 data /
        # _meta）：与历史总结路径同一口径逐条截断，避免 prompt 膨胀
        dialogue_text = (
            "\n".join(
                f"{'用户' if item.get('role') == 'user' else '助手'}: "
                f"{_trim_dialogue(item.get('content'))}"
                for item in current_dialogue
            )
            or "（暂无当前会话上下文）"
        )

        if not user_query:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "error",
                    "message": "无法获取用户查询"
                }),
                role="assistant"
            )

        # 获取长期记忆
        trip_history = []
        preferences = {}
        chat_summary = ""

        if self.memory_manager:
            # 行程 / 偏好是阻塞查询（JSON 文件、PostgreSQL 网络往返）：
            # 放到线程池执行，留在事件循环线程会冻结同进程所有 SSE 流
            try:
                trip_history, preferences = await asyncio.to_thread(
                    self._load_long_term_context
                )
            except Exception as e:  # noqa: BLE001 - 存储抖动按空记忆继续回答
                logger.warning(f"Failed to load long-term memory: {e}")
                trip_history, preferences = [], {}

            # 获取历史对话摘要（如果有LLM的话）
            try:
                chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=30)
            except Exception as e:
                logger.warning(f"Failed to get chat summary: {e}")
                chat_summary = ""

        # 格式化旅行历史
        trip_text = self._format_trip_history(trip_history)

        # 格式化偏好
        pref_text = self._format_preferences(preferences)

        # 动态读取 Prompt 指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("memory-query")
        if not skill_instruction:
            skill_instruction = "请基于用户的历史记忆回答问题，如无相关记录请诚实说明。"

        # 构建 prompt：带上当前日期，规划层会提前把未来行程写入历史，
        # 没有日期模型无法区分「已去过」与「计划中的行程」
        from datetime import datetime

        current_date = datetime.now().strftime("%Y-%m-%d")
        prompt = f"""你是一个个人记忆助手，请基于用户的历史记忆回答问题。

【当前日期】
{current_date}
（用户旅行历史里出发日期晚于当前日期的行程只是计划，不代表已经出行）

【用户问题】
{user_query}

【当前会话最近对话】
{dialogue_text}

【用户旅行历史】
{trip_text}

【用户偏好】
{pref_text}

【历史对话摘要】
{chat_summary if chat_summary else "（暂无历史对话摘要）"}

【任务说明】
{skill_instruction}
"""

        try:
            # 调用LLM生成回答
            response = await self.model([
                {"role": "system", "content": "你是一个个人记忆助手，帮助用户查询和理解他们的历史记录。"},
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本（兼容非流式 ChatResponse / 流式生成器 / 测试 mock）
            answer = await extract_json_from_async_response(response)

            if not answer or not str(answer).strip():
                # 网关 200 空 choices / 纯空白：按可重试错误上抛，交给编排
                # 层重试与熔断记账，不能伪装成一条「成功」的空回答
                raise ConnectionError("模型返回空响应")

            logger.info(f"Memory query answered: {user_query[:50]}")

            result = {
                "status": "success",
                "query": user_query,
                "answer": answer,
                "memory_sources": {
                    "trip_count": len(trip_history),
                    "has_preferences": any(
                        has_preference_value(v) for v in preferences.values()
                    ),
                    "has_chat_summary": bool(chat_summary)
                }
            }

            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        except Exception as e:
            if is_retriable_error(e) or is_permanent_llm_error(e):
                # 瞬时错误交给编排层重试；401/402/403/404 等永久错误
                # 同样穿透并计入熔断，不能吞成 error JSON
                raise
            logger.error(f"Memory query failed: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "error",
                    "message": f"记忆查询失败: {redact_paths(e)}",
                    "query": user_query
                }),
                role="assistant"
            )

    def _load_long_term_context(self) -> tuple[list[dict], dict]:
        """同步读取行程与偏好（供 ``asyncio.to_thread`` 调用）。

        取全部行程：截断会让「我去过昆明吗」在超过 50 条时误答没有记录。
        """
        return (
            self.memory_manager.long_term.get_trip_history(limit=None),
            self.memory_manager.long_term.get_preference(),
        )

    def _format_trip_history(self, trip_history: list[dict]) -> str:
        """格式化旅行历史（按最近优先装入字符预算，省略条目明确注记）。"""
        if not trip_history:
            return "（暂无旅行记录）"

        lines: list[str] = []
        used = 0
        omitted = 0
        total = len(trip_history)
        for index in range(total - 1, -1, -1):
            trip = trip_history[index]
            if not isinstance(trip, dict):
                continue
            # 事件收集会对缺失字段显式写 null（key 存在），必须用 or 取默认值
            origin = trip.get("origin") or "未知"
            destination = trip.get("destination") or "未知"
            start_date = trip.get("start_date") or ""
            end_date = trip.get("end_date") or ""
            purpose = trip.get("purpose") or "旅游"
            timestamp = trip.get("timestamp") or ""

            if start_date and end_date:
                line = f"{index + 1}. {origin} → {destination} ({start_date} 至 {end_date}) - {purpose}"
            elif start_date:
                line = f"{index + 1}. {origin} → {destination} ({start_date}) - {purpose}"
            else:
                line = f"{index + 1}. {origin} → {destination} - {purpose} (记录时间: {timestamp})"
            if len(line) > _MAX_TRIP_LINE_CHARS:
                line = line[:_MAX_TRIP_LINE_CHARS] + "…"
            if used + len(line) + 1 > _MAX_TRIP_TEXT_CHARS:
                omitted = index + 1
                break
            lines.append(line)
            used += len(line) + 1

        lines.reverse()
        text = "\n".join(lines) or "（暂无旅行记录）"
        if omitted:
            text = f"（更早的 {omitted} 条行程已省略）\n{text}"
        return text

    def _format_preferences(self, preferences: dict) -> str:
        """格式化用户偏好（单项截断 + 总量预算）。"""
        if not preferences or not any(
            has_preference_value(v) for v in preferences.values()
        ):
            return "（暂无偏好记录）"

        lines = []
        # 与 cli.py 的 type_names 及系统实际存储的偏好类型保持一致（Issue #3）
        pref_names = {
            "home_location": "常驻地",
            "transportation_preference": "交通偏好",
            "hotel_brands": "酒店偏好",
            "airlines": "航空公司偏好",
            "seat_preference": "座位偏好",
            "meal_preference": "餐食偏好",
            "budget_level": "预算等级",
        }

        used = 0
        truncated = False
        for key, value in preferences.items():
            if not has_preference_value(value):
                continue
            # 未知/自定义偏好类型不再丢弃，直接使用原始类型名
            name = pref_names.get(key, key)
            if isinstance(value, (list, tuple)):
                # 历史数据可能含 [null] / ["  "]（旧版本写入）：逐项过滤，
                # 不能让 "None" 作为偏好值拼进 prompt
                value = ", ".join(
                    str(v) for v in value if has_preference_value(v)
                )
            value_text = str(value)
            if len(value_text) > _MAX_PREF_VALUE_CHARS:
                value_text = value_text[:_MAX_PREF_VALUE_CHARS] + "…"
            line = f"- {name}: {value_text}"
            if used + len(line) + 1 > _MAX_PREF_TEXT_CHARS:
                truncated = True
                break
            lines.append(line)
            used += len(line) + 1

        if truncated:
            lines.append("（更多偏好已省略）")
        return "\n".join(lines) if lines else "（暂无偏好记录）"
