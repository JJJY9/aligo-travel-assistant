"""
意图识别智能体 IntentionRecognitionAgent
职责：准确识别用户意图，并进行智能体调度

核心功能：
1. 多意图识别和分类：融合上下文对模糊意图进行消歧
2. 智能体调度决策：基于预定义的触发条件和业务规则，根据识别结果决定调用哪些子智能体
3. Query改写：标准化用户口语化的query输入，补全上下文信息，提取和重组关键信息
4. 显示推理：输出的两段式结构（推理过程 + JSON决策），提升意图识别准确度

架构：
- 使用单一LLM（用户配置的模型）
- 输入：用户query（自然语言）
- 输出：推理过程生成（包含reasoning+原因） + 多意图识别（原因） + 智能Query改写 + 构建结构化决策
"""
import json
import logging

from agentscope.agent import AgentBase
from agentscope.message import Msg

from utils.access_log import _safe_log_text
from utils.json_parser import (
    extract_json_from_async_response,
    has_unclosed_json_object,
    iter_json_objects_with_repair,
    reject_json_constant,
    robust_json_parse,
)
from utils.llm_resilience import is_permanent_llm_error, is_retriable_error
from utils.skill_loader import SkillLoader

logger = logging.getLogger(__name__)

# 意图 prompt 的输入上限：CLI 等入口可以传任意长度，超长输入会触发模型
# 上下文错误并降级为携带原文的信息查询，污染后续总结
_MAX_QUERY_CHARS = 4000


class _ProviderError(RuntimeError):
    """模型网关以 200 返回错误体 / 空响应时携带状态码，供熔断器分类。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def _message_text(content) -> str:
    """把 Msg.content（字符串或 agentscope content block 列表）转成纯文本。

    agentscope 的 ``Msg.content`` 类型是 ``str | Sequence[ContentBlock]``：
    直接 ``str()`` 会把文本块变成 ``[{'type': 'text', 'text': ...}]`` 的
    Python repr 拼进 prompt，模型收到的是容器语法而非用户原话，
    ``_MAX_QUERY_CHARS`` 截断也按 repr 长度计算。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _default_intent_result(user_query: str, error: str) -> dict:
    """意图识别失败 / 结果形状非法时的降级结果：默认走信息查询。"""
    return {
        "reasoning": f"意图识别出错，使用默认策略。错误: {error}",
        "intents": [
            {
                "type": "information_query",
                "confidence": 0.5,
                "description": "默认查询意图",
                "reason": "无法解析用户意图，使用默认策略",
            }
        ],
        "key_entities": {},
        "rewritten_query": user_query,
        "agent_schedule": [
            {
                "agent_name": "information_query",
                "priority": 1,
                "reason": "默认查询",
                "expected_output": "查询结果",
            }
        ],
    }


class IntentionAgent(AgentBase):
    """意图识别智能体（IntentionRecognitionAgent）"""

    def __init__(self, name: str = "IntentionRecognitionAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.conversation_history: list[str] = []
        self.skill_loader = SkillLoader()

    async def reply(self, x: Msg | list[Msg] | None = None) -> Msg:
        """
        意图识别主流程
        1. 推理过程生成
        2. 多意图识别
        3. 智能Query改写
        4. 构建结构化决策
        """
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 获取用户查询
        if isinstance(x, list):
            user_query = _message_text(x[-1].content) if x else ""
            # 提取历史对话，保留角色信息
            self.conversation_history = []
            for msg in x[:-1]:
                if hasattr(msg, 'content') and hasattr(msg, 'role'):
                    # 区分处理不同角色的消息
                    if msg.role == "system":
                        # 长期记忆（system）- 完整保留，不截断
                        self.conversation_history.append(f"[系统记忆]\n{_message_text(msg.content)}")
                    else:
                        # 对话历史（user/assistant）- 适当截断但保留更多信息
                        role_name = "用户" if msg.role == "user" else "助手"
                        content = _message_text(msg.content)
                        if len(content) > 800:
                            content = content[:800] + "..."
                        self.conversation_history.append(f"{role_name}: {content}")
        else:
            user_query = _message_text(x.content)
            # 单条 Msg 调用没有历史：必须清空上一种输入形态（列表）遗留的
            # conversation_history，否则上一轮对话会串进本次意图 prompt
            self.conversation_history = []
        if len(user_query) > _MAX_QUERY_CHARS:
            user_query = user_query[:_MAX_QUERY_CHARS] + "…（输入过长已截断）"

        # 构建上下文
        # 策略：长期记忆始终保留，短期对话全部保留（已在 cli.py 控制数量）
        context_parts = []
        system_memory = None
        dialogue_history = []

        for item in self.conversation_history:
            if item.startswith("[系统记忆]"):
                system_memory = item  # 保存长期记忆
            else:
                dialogue_history.append(item)  # 保存对话历史

        # 组装上下文：长期记忆 + 全部对话
        if system_memory:
            context_parts.append(system_memory)
        if dialogue_history:
            context_parts.extend(dialogue_history) 

        context_str = "\n".join(context_parts) if context_parts else "无历史对话"

        # 获取当前时间
        from datetime import datetime
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 动态获取 Skills 描述
        skill_mapping = {
            "memory-query": "memory_query",
            "plan-trip": "itinerary_planning", 
            "preference": "preference",
            "query-info": "information_query",
            "ask-question": "rag_knowledge",
            "event-collection": "event_collection"
        }
        
        dynamic_skills_prompt = self.skill_loader.get_skill_prompt(skill_mapping)
        
        # 构建意图识别Prompt
        prompt = f"""你是一个高级意图识别专家（IntentionRecognitionAgent）。请分析用户查询，识别意图并输出结构化的决策。
        
【当前时间】
{current_time} {weekday}
（重要：当用户说"2月28日"或"明天"等相对时间时，请根据当前时间进行推断完整日期）

【用户Query】
{user_query}

【对话历史上下文】
{context_str}

【可调度的子智能体 (Skills)】
{dynamic_skills_prompt}

【重要 - 意图区分原则】
请基于语义理解判断意图，不要机械匹配关键词。同一个词在不同语境下可能对应不同意图：
- "我去过北京吗？" → memory_query（询问自己的历史）
- "北京怎么样？" / "北京有什么好玩的？" → information_query（询问客观信息）
- "我想去北京" → itinerary_planning（规划未来行程）

优先级规则：
- memory_query 优先于 information_query（当问题涉及用户自己的历史时）
- 如果用户明确询问"我的"、"我过去的"，必须识别为 memory_query

【任务要求】
请按以下步骤进行分析：

**第1步：推理过程生成**
- 分析用户query的核心诉求
- 识别query中的关键实体和意图信号
- 判断是否需要结合对话历史进行消歧
- 说明如何融合上下文信息进行推理

**第2步：多意图识别（原因）**
- 识别所有可能的用户意图（可以是多个）
- 为每个意图分配置信度（0-1之间）
- 说明为什么识别出该意图的原因

**第3步：智能Query改写**
- 识别口语化表达，进行标准化
- 补全省略的上下文信息
- 提取和重组关键信息

**第4步：构建结构化决策**
- 基于识别的意图，决定调用哪些子智能体
- 说明调用顺序和优先级
- 输出结构化的调用策略

【输出格式要求】
必须严格按照以下JSON格式输出（**只输出JSON，不要有其他文本**）：

{{
    "reasoning": "这里是详细的推理过程，包含第1步的分析，说明如何理解用户query，如何结合上下文，如何识别意图信号",

    "intents": [
        {{
            "type": "意图类型（如：itinerary_planning, preference_collection, information_query等）",
            "confidence": 0.95,
            "description": "该意图的具体说明",
            "reason": "为什么识别出该意图的原因"
        }}
    ],

    "key_entities": {{
        "origin": "出发地（如果有）",
        "destination": "目的地（如果有）",
        "date": "日期（如果有）",
        "duration": "时长（如果有）",
        "other": "其他关键信息"
    }},

    "rewritten_query": "标准化、补全后的查询内容",

    "agent_schedule": [
        {{
            "agent_name": "子智能体名称",
            "priority": 1,
            "reason": "调用该智能体的原因和依据",
            "expected_output": "期望该智能体提供什么输出"
        }}
    ]
}}

【重要提示 - 优先级设置规则】
优先级数字相同的智能体会**并行执行**，不同优先级按顺序批次执行。

**所有智能体优先级分组：**

**Priority 1（并行执行）- 信息收集类：**
- memory_query: 记忆查询智能体
- event_collection: 事项收集智能体
- preference: 偏好管理智能体
- information_query: 信息查询智能体（联网搜索）
- rag_knowledge: RAG知识库智能体（查询企业知识库）

**Priority 2（依赖 Priority 1）- 行程规划类：**
- itinerary_planning: 行程规划智能体（需要事项收集的结果）

**说明：**
- Priority 1 的智能体都是信息获取，互不依赖，可并行执行提升速度
- Priority 2 的智能体需要使用 Priority 1 收集的信息
- 示例：用户说"我要从天津去北京，喜欢住汉庭"
  → Priority 1: preference + event_collection（并行）
  → Priority 2: itinerary_planning（使用 Priority 1 的结果）

请开始分析，直接输出JSON：
"""

        # 调用LLM进行意图识别
        try:
            # 构建符合OpenAI格式的messages
            messages = [
                {"role": "system", "content": "你是一个高级意图识别专家。只输出JSON格式的结果，不要输出其他文本。"},
                {"role": "user", "content": prompt}
            ]
            response = await self.model(messages)

            # 获取响应文本（兼容非流式 ChatResponse / 流式生成器 / 测试 mock）
            text = await extract_json_from_async_response(response)

            # 清理文本
            text = text.strip()
            text = text.removeprefix('```json')
            text = text.removeprefix('```')
            text = text.removesuffix('```')
            text = text.strip()
            if not text:
                # 网关以 200 返回空错误体 / choices 为空：上游实际不可用，
                # 按可重试错误上抛（重试可能恢复），熔断器也能记账；不能
                # 落进「JSON 解析失败 → 默认意图」被当成成功
                raise ConnectionError("模型服务返回空响应")

            # 解析JSON（拒绝 NaN / Infinity；顶层必须是对象）
            try:
                result = json.loads(text, parse_constant=reject_json_constant)
                if (
                    isinstance(result, dict)
                    and "agent_schedule" not in result
                    and "error" in result
                ):
                    # 网关 200 + 错误体（限流 / 鉴权失败等）：状态码交给
                    # 熔断器分类（429/5xx 重试、4xx 记为永久故障）
                    provider_error = result.get("error")
                    code: int | None = None
                    if isinstance(provider_error, dict):
                        raw_code = provider_error.get("code") or provider_error.get(
                            "status"
                        )
                        if isinstance(raw_code, int):
                            code = raw_code
                    raise _ProviderError(
                        f"模型服务返回错误：{str(provider_error)[:200]}", code
                    )
                if not isinstance(result, dict):
                    raise ValueError("意图识别结果不是 JSON 对象")
                if "agent_schedule" not in result:
                    raise ValueError("意图识别结果缺少 agent_schedule")
                if not isinstance(result["agent_schedule"], list):
                    # 模型可能把列表误写成字符串 / null：与「缺少该键」走同一
                    # 降级（默认 information_query），不能静默变成 no_agents
                    # 让用户的规划 / 查询请求被当成「已记录」
                    raise ValueError("意图识别结果 agent_schedule 不是列表")
            except json.JSONDecodeError as e1:
                # 直接解析失败时扫描文本中的全部 JSON 对象：说明文字 / 推理
                # 过程里可能先出现一个合法的小对象（如单个 key_entities
                # 片段），只取第一个会丢弃后面完整的意图结果并整体降级为
                # information_query。优先采用最后一个带 agent_schedule 的
                # 对象——模型回显 prompt 模板时占位对象在前、真实结果在后
                result = None
                selected_end = -1
                truncated = False
                for candidate, end in iter_json_objects_with_repair(text):
                    if "agent_schedule" in candidate:
                        result = candidate
                        selected_end = end
                if result is not None and has_unclosed_json_object(
                    text[selected_end:]
                ):
                    # 回显的模板对象在前、正式意图结果被 max_tokens 截断：
                    # 模板占位调度不能当成真实决策执行（技能侧已有同等守卫）
                    logger.warning("意图识别结果在对象处被截断，回退默认调度")
                    truncated = True
                    result = None
                if truncated:
                    # 不能走 robust_json_parse 兜底：它按「首个 { 到最后一个
                    # }」截取，会把前面的模板对象解析成功、重新当成结果
                    raise ValueError("意图识别结果在对象处被截断")
                if result is None:
                    # 尾逗号 / 裸控制字符等小瑕疵：走修复解析再试一次，
                    # 否则规划请求会被整体降级成 information_query 搜索
                    try:
                        repaired = robust_json_parse(text, fallback=None)
                    except ValueError:
                        repaired = None
                    if isinstance(repaired, dict) and "agent_schedule" in repaired:
                        result = repaired
                if result is None:
                    logger.error(
                        "JSON parse failed. Text sample: %s",
                        _safe_log_text(text[:100]),
                    )
                    raise ValueError(f"Failed to parse JSON. Error: {e1}") from e1

        except Exception as e:
            # 可重试错误（超时/连接失败/429/5xx）向上抛出，
            # 交由 cli.py 的 retry_with_backoff 与熔断器处理（Issue #1）
            if is_retriable_error(e):
                logger.error(f"Intent recognition failed (retriable): {e}")
                raise
            if is_permanent_llm_error(e):
                # 非可重试的模型调用失败（401/402/403/404 等 4xx）：吞成默认
                # 意图会让外层把「LLM 完全不可用」记为成功，熔断器与健康
                # 检查永远看不到故障，必须同样上抛
                logger.error(f"Intent recognition failed (permanent LLM error): {e}")
                raise
            logger.error(f"Intent recognition failed: {e}")
            # 返回默认结果
            result = _default_intent_result(user_query, str(e))

        # JSONDecodeError 扫描分支选中的对象同样可能带非列表 schedule：
        # 与缺键 / 类型非法一致，降级为默认 information_query 调度
        if not isinstance(result.get("agent_schedule"), list):
            logger.error("意图识别结果 agent_schedule 不是列表，降级为默认调度")
            result = _default_intent_result(user_query, "agent_schedule 类型非法")

        # 将结果转换为JSON字符串，因为Msg的content必须是字符串
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
