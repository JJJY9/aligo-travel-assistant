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
import re
from datetime import date
from pathlib import Path
from typing import Any

from agentscope.agent import AgentBase
from agentscope.message import Msg

from utils.access_log import _safe_log_text
from utils.agent_names import SKILL_AGENT_NAMES, canonical_agent_name
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

# key_entities 输出模板里的占位说明：模型完整回显模板时会把这些字符串
# 当真实实体返回，编排层会据此写入「出发地（如果有）」之类的行程
_ENTITY_PLACEHOLDER_MARKERS = ("（如果有）", "(如果有)", "其他关键信息")
_DATE_TEXT_RE = re.compile(r"^(\d{4})[年/.](\d{1,2})[月/.](\d{1,2})日?$")


def _normalize_date_text(text: str) -> str | None:
    """key_entities.date 的中文 / 斜杠写法归一成 ISO；非法日历日返回 None。"""
    match = _DATE_TEXT_RE.fullmatch(text)
    if not match:
        return text
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _clean_key_entities(raw) -> dict:
    """丢弃模板占位值、归一日期：key_entities 会被编排层直接落库成行程。"""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text or any(
                marker in text for marker in _ENTITY_PLACEHOLDER_MARKERS
            ):
                continue
            if key.strip() == "date":
                normalized = _normalize_date_text(text)
                if normalized is None:
                    continue
                text = normalized
            cleaned[key.strip()] = text
        else:
            cleaned[key.strip()] = value
    return cleaned


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


_PERMANENT_ERROR_CODES = {
    # OpenAI 兼容网关的字符串错误码（HTTP 200 + 错误体）：鉴权 / 权限 /
    # 配额类均为永久故障，映射为 401 让熔断器与重试层如实分类
    "invalid_api_key": 401,
    "authentication_error": 401,
    "permission_error": 403,
    "insufficient_quota": 402,
    "billing_hard_limit_reached": 402,
    "invalid_request_error": 400,
    "context_length_exceeded": 400,
}

# 字符串型瞬时错误码（同上，HTTP 200 + 错误体）：必须映射成 5xx/429，
# 否则 is_retriable_error / is_permanent_llm_error 都判 False，一次本可
# 重试恢复的上游抖动会被静默改写成默认 information_query 轮次
_TRANSIENT_ERROR_CODES = {
    "server_error": 500,
    "internal_server_error": 500,
    "api_error": 500,
    "service_unavailable": 503,
    "server_unavailable": 503,
    "overloaded_error": 503,
    "rate_limit_exceeded": 429,
    "rate_limit_error": 429,
}


# 错误文本（而非结构化 code）里的永久故障标记
_PERMANENT_ERROR_MARKERS = (
    ("invalid api key", 401),
    ("incorrect api key", 401),
    ("invalid_api_key", 401),
    ("incorrect_api_key", 401),
    ("authentication", 401),
    ("unauthorized", 401),
    ("permission_error", 403),
    ("insufficient_quota", 402),
    ("insufficient quota", 402),
    ("billing_hard_limit_reached", 402),
    # OpenAI 规范配额错误 message（网关归一成只有 message 的 200 错误体时，
    # 上面三个 marker 都命中不了；与 utils/llm_resilience 保持对齐）
    ("exceeded your current quota", 402),
    ("exceeded your quota", 402),
    ("check your plan and billing details", 402),
    ("model_not_found", 404),
)

# 错误文本里的瞬时故障标记：不映射成 5xx/429 时 is_retriable_error 只按
# timeout / rate limit 字样判断，多数 200 错误体的瞬时文案会被判成确定性
# 失败、静默降级为默认 information_query
_TRANSIENT_ERROR_MARKERS = (
    ("the server had an error", 500),
    ("server had an error", 500),
    ("internal server error", 500),
    ("overloaded", 503),
    ("service temporarily unavailable", 503),
    ("service unavailable", 503),
    ("temporarily unavailable", 503),
    ("rate limit", 429),
    ("too many requests", 429),
)


def _status_from_error_text(text: str) -> int | None:
    """错误体只有 message（无 code / status）时按关键词映射状态码。

    网关常见的 200 + ``{"error": "Incorrect API key provided..."}`` / 纯文本
    错误体没有结构化字段：映射为 401/402/403/404 后熔断器才能按永久故障
    记账；瞬时文案映射成 5xx/429，才能走重试而不是被静默降级。只作用于
    错误体文本（调用方已确认这是错误信封），不会误伤正常回答。
    """
    lowered = text.lower()
    for marker, code in _PERMANENT_ERROR_MARKERS:
        if marker in lowered:
            return code
    for marker, code in _TRANSIENT_ERROR_MARKERS:
        if marker in lowered:
            return code
    return None


def _error_status_code(raw_code: Any, provider_error: dict) -> int | None:
    """从错误体的 code / status / type 提取状态码（int 或字符串形态）。"""
    for candidate in (
        raw_code,
        provider_error.get("status"),
        provider_error.get("type"),
    ):
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str):
            text = candidate.strip().lower()
            if text.isdigit():
                return int(text)
            if text in _PERMANENT_ERROR_CODES:
                return _PERMANENT_ERROR_CODES[text]
            if text in _TRANSIENT_ERROR_CODES:
                return _TRANSIENT_ERROR_CODES[text]
    return None


def _error_envelope_payload(data: dict[str, Any]) -> Any:
    """识别 200 响应里的错误信封；非错误信封返回 None。

    除顶层 ``error`` 为真（既有形态）外，兼容网关归一成
    ``{"status": "error", "code": 401, "message": …}`` /
    ``{"success": false, "code": …}`` / ``{"code": 503, "message": …}``
    的错误信封——只认 ``error`` 键时这些形态会滑进 JSON 解析失败分支、
    被静默降级成默认 information_query。带 ``agent_schedule`` 的对象
    永远是正常结果，不判错误。
    """
    if "agent_schedule" in data:
        return None
    error = data.get("error")
    if error:
        return error
    if not any(key in data for key in ("code", "message", "msg")):
        return None
    status_text = str(data.get("status") or "").strip().lower()
    if status_text in {"error", "failed", "fail"}:
        return data
    if data.get("success") is False:
        return data
    code = _error_status_code(data.get("code"), data)
    if code is not None and 400 <= code < 600 and (
        "message" in data or "msg" in data
    ):
        return data
    return None


def _redact_error_text(error: BaseException) -> str:
    """错误文本脱敏：去掉密钥 / URL 凭据 / 本机路径后写进 reasoning。"""
    try:
        from utils.log_redact import redact_paths

        return redact_paths(str(error))
    except Exception:  # noqa: BLE001 - 脱敏失败也不能阻断降级
        return str(error)


# 降级结果的 reasoning 前缀：评测（utils.agent_eval）据它识别「模型输出
# 故障被兜底成默认调度」，不能把兜底结果当成真实预测计分
DEFAULT_INTENT_REASONING_PREFIX = "意图识别出错，使用默认策略"


def _default_intent_result(user_query: str, error: str) -> dict:
    """意图识别失败 / 结果形状非法时的降级结果：默认走信息查询。"""
    return {
        "reasoning": f"{DEFAULT_INTENT_REASONING_PREFIX}。错误: {error}",
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
        # 只宣传可被 LazyAgentRegistry 真正调度的技能：纯文档技能展示给
        # 模型后会永远「未注册」，整轮调度失败
        self.skill_loader = SkillLoader(dispatchable_only=True)
        # 技能目录名 → 旧标准名（意图 prompt 展示后者，下游按标准名处理）
        self._skill_mapping = {
            "memory-query": "memory_query",
            "plan-trip": "itinerary_planning",
            "preference": "preference",
            "query-info": "information_query",
            "ask-question": "rag_knowledge",
            "event-collection": "event_collection",
        }
        # 可调度的智能体名（目录名 / 标准名 / frontmatter 名，大小写不敏感）：
        # 模型回显 prompt 模板时条目名是占位说明，不能被当成真实决策
        skill_names = list(self.skill_loader.load_skills())
        # 真实存在 script/agent.py 的技能目录（与 LazyAgentRegistry 同一来源）：
        # 自定义技能目录 / wheel 缺技能时，过滤与静态优先级块都不能宣传注册表
        # 加载不到的名字，否则模型按宣传调度必然落到「智能体未注册」
        from utils.skill_paths import skill_scripts

        self._available_skill_dirs = {
            str(skill_dir).strip()
            for skill_dir in skill_scripts(Path(self.skill_loader.skills_dir))
            if str(skill_dir).strip()
        }
        valid_names: list[str] = []
        for skill_dir, standard_name in (
            *self._skill_mapping.items(),
            *SKILL_AGENT_NAMES.items(),
            ("preference", "preference"),
        ):
            if skill_dir in self._available_skill_dirs:
                valid_names.extend((skill_dir, standard_name))
        valid_names.extend(str(name) for name in skill_names)
        # 目录名同样可调度（LazyAgentRegistry._resolve_agent_name 第一步就按
        # 目录名解析）：自定义技能 name != 目录名 / 缺 SKILL.md 时，优先级
        # 分组按目录名宣传，过滤层若只认 frontmatter 名会把整轮调度降级
        valid_names.extend(sorted(self._available_skill_dirs))
        self._valid_agent_names = {
            str(skill_name).strip().casefold()
            for skill_name in valid_names
            if str(skill_name).strip()
        }
        # 连字符 ↔ 下划线变体也归一（模型按 JSON snake_case 习惯输出
        # trip_budget / plan_trip / packing_list 时不能被当成未知智能体名，
        # 否则单项计划整轮降级、多项计划静默丢条）；值优先取先登记的
        # 技能目录名，编排层的去重与依赖排序才能按目录名口径继续工作
        self._agent_name_lookup: dict[str, str] = {}
        for valid_name in valid_names:
            text = str(valid_name).strip()
            if not text:
                continue
            self._agent_name_lookup.setdefault(text.casefold(), text)
            swapped = (
                text.replace("-", "_") if "-" in text else text.replace("_", "-")
            )
            if swapped != text:
                self._agent_name_lookup.setdefault(swapped.casefold(), text)

    def _valid_schedule(self, schedule: list) -> list[dict]:
        """过滤占位 / 未知智能体名的调度条目（大小写与旧名变体均接受）。"""
        valid: list[dict] = []
        for entry in schedule:
            if not isinstance(entry, dict):
                continue
            agent_name = str(entry.get("agent_name") or "").strip()
            if not agent_name:
                continue
            canonical = self._agent_name_lookup.get(agent_name.casefold())
            if (
                canonical is None
                and agent_name.casefold() not in self._valid_agent_names
                and canonical_agent_name(agent_name).casefold()
                not in self._valid_agent_names
            ):
                logger.warning(
                    "忽略未知智能体名的调度条目：%r", agent_name[:40]
                )
                continue
            if (
                canonical is not None
                and canonical != agent_name
                and agent_name.casefold() not in self._valid_agent_names
            ):
                # 互换变体（trip_budget / plan_trip）重写为真实技能名：
                # 后续去重 / 依赖排序 / 注册表查找都按既有路径工作。
                # 大小写变体本身已合法（下游 canonical 归一口径不变），
                # 不在这里改写，保持历史输出形状
                entry = {**entry, "agent_name": canonical}
            valid.append(entry)
        return valid

    def _priority_groups_prompt(self) -> str:
        """按实际可调度技能生成优先级分组说明（默认安装下与旧文案逐字一致）。"""
        available = self._available_skill_dirs

        def render_group(title: str, lines: list[str]) -> list[str]:
            rendered = [line for line in lines if line]
            if not rendered:
                return []
            return [title, *rendered]

        group1 = render_group(
            "**Priority 1（并行执行）- 信息收集类：**",
            [
                (
                    "- memory_query: 记忆查询智能体"
                    if "memory-query" in available
                    else ""
                ),
                (
                    "- event_collection: 事项收集智能体"
                    if "event-collection" in available
                    else ""
                ),
                "- preference: 偏好管理智能体" if "preference" in available else "",
                (
                    "- information_query: 信息查询智能体（联网搜索）"
                    if "query-info" in available
                    else ""
                ),
                (
                    "- rag_knowledge: RAG知识库智能体（查询企业知识库）"
                    if "ask-question" in available
                    else ""
                ),
            ],
        )
        simple_dirs = [
            name
            for name in (
                "packing-list",
                "travel-checklist",
                "safety-tips",
                "trip-notes",
            )
            if name in available
        ]
        group2 = render_group(
            "**Priority 2（依赖 Priority 1）- 行程规划与行前信息类：**",
            [
                (
                    "- itinerary_planning: 行程规划智能体（需要事项收集的结果）"
                    if "plan-trip" in available
                    else ""
                ),
                (
                    "- "
                    + " / ".join(simple_dirs)
                    + ": 行李 / 行前 / 安全 / 贴士（需要目的地、日期等事项信息）"
                    if simple_dirs
                    else ""
                ),
            ],
        )
        group3 = render_group(
            "**Priority 3（依赖行程规划）：**",
            [
                (
                    "- trip-budget: 预算建议（需要行程规划结果）"
                    if "trip-budget" in available
                    else ""
                ),
                (
                    "- local-transport: 市内交通（需要行程规划结果）"
                    if "local-transport" in available
                    else ""
                ),
            ],
        )
        known_dirs = set(self._skill_mapping) | set(SKILL_AGENT_NAMES) | {
            "preference",
            *simple_dirs,
            "trip-budget",
            "local-transport",
        }
        others = sorted(available - known_dirs)
        other_lines = (
            ["- 其他技能（Priority 1）：" + ", ".join(others)] if others else []
        )
        blocks = [
            block
            for block in (group1, group2, group3, other_lines)
            if block
        ]
        if not blocks:
            return ""
        return "\n\n".join(
            ["**所有智能体优先级分组：**", *("\n".join(block) for block in blocks)]
        )

    def _schedule_has_known_agent(self, result: dict) -> bool:
        schedule = result.get("agent_schedule")
        if not isinstance(schedule, list):
            return False
        return bool(self._valid_schedule(schedule))

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

        # 获取当前时间（请求时区：Web 面板的「明天 / 今天」按浏览器本地）
        from utils.clock import now as clock_now

        current = clock_now()
        current_time = current.strftime("%Y年%m月%d日 %H:%M")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][current.weekday()]

        # 动态获取 Skills 描述
        skill_mapping = self._skill_mapping

        dynamic_skills_prompt = self.skill_loader.get_skill_prompt(skill_mapping)
        # 优先级分组只宣传真实可调度的技能：与动态清单同一口径，避免模型
        # 按静态块调度到注册表加载不到的技能（custom 目录 / wheel 缺技能）
        priority_groups_prompt = self._priority_groups_prompt()
        
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
        "origin": null,
        "destination": null,
        "date": null,
        "duration": null,
        "other": null
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

【字段要求】
- key_entities 中没有的字段必须填 null（不要填「出发地（如果有）」之类的说明文字）；date 必须是 YYYY-MM-DD 格式（根据当前时间把「明天」等相对时间换算成完整日期）。
- agent_schedule 的 agent_name 必须是上方「可调度的子智能体」列表中的名称（或其后括号外的标准名）；不要原样输出模板占位值，否则该条目会被丢弃。
- agent_schedule 仅当用户是纯问候、闲聊或致谢时可以为空；凡涉及出行规划、信息查询、偏好或记忆的请求必须至少调度一个智能体，无法判断时调度 information_query。

【重要提示 - 优先级设置规则】
优先级数字相同的智能体会**并行执行**，不同优先级按顺序批次执行。

{priority_groups_prompt}

**说明：**
- Priority 1 的智能体都是信息获取，互不依赖，可并行执行提升速度
- Priority 2 的智能体需要使用 Priority 1 收集的信息
- Priority 3 的智能体需要行程规划结果（未调度行程规划时可与 Priority 2 同批）
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
                # 网关 200 + 错误体（限流 / 鉴权失败等）：状态码交给熔断器
                # 分类（429/5xx 重试、4xx 记为永久故障）。除顶层 error 外，
                # status/success/code 形态的兼容错误信封同样识别，不能再
                # 滑进「缺 agent_schedule」被降级成默认意图
                provider_error = (
                    _error_envelope_payload(result)
                    if isinstance(result, dict)
                    else None
                )
                if provider_error is not None:
                    code: int | None = None
                    if isinstance(provider_error, dict):
                        raw_code = provider_error.get("code") or provider_error.get(
                            "status"
                        )
                        code = _error_status_code(raw_code, provider_error)
                        if code is None:
                            detail = (
                                provider_error.get("message")
                                or provider_error.get("msg")
                                or str(provider_error)
                            )
                            code = _status_from_error_text(str(detail))
                    else:
                        # 纯字符串错误体：按错误文本识别永久 / 瞬时故障
                        code = _status_from_error_text(str(provider_error))
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
                fallback_result = None
                fallback_end = -1
                for candidate, end in iter_json_objects_with_repair(text):
                    if "agent_schedule" in candidate:
                        if self._schedule_has_known_agent(candidate):
                            # 优先最后一个带已知智能体名的完整结果：模型
                            # 尾随说明里的示例对象（占位名称）不会顶掉真实决策
                            result = candidate
                            selected_end = end
                        elif fallback_result is None:
                            fallback_result = candidate
                            fallback_end = end
                if result is None and fallback_result is not None:
                    result = fallback_result
                    selected_end = fallback_end
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
                logger.error(
                    "Intent recognition failed (retriable): %s",
                    _redact_error_text(e),
                )
                raise
            if is_permanent_llm_error(e):
                # 非可重试的模型调用失败（401/402/403/404 等 4xx）：吞成默认
                # 意图会让外层把「LLM 完全不可用」记为成功，熔断器与健康
                # 检查永远看不到故障，必须同样上抛
                logger.error(
                    "Intent recognition failed (permanent LLM error): %s",
                    _redact_error_text(e),
                )
                raise
            logger.error(
                "Intent recognition failed: %s", _redact_error_text(e)
            )
            # 返回默认结果；reasoning 会随 _meta 落库与 SSE 下发，错误文本
            # 可能夹带上游网关的密钥 / 内部地址，必须先脱敏
            result = _default_intent_result(
                user_query, _redact_error_text(e)
            )

        # JSONDecodeError 扫描分支选中的对象同样可能带非列表 schedule：
        # 与缺键 / 类型非法一致，降级为默认 information_query 调度
        if not isinstance(result.get("agent_schedule"), list):
            logger.error("意图识别结果 agent_schedule 不是列表，降级为默认调度")
            result = _default_intent_result(user_query, "agent_schedule 类型非法")

        # 过滤占位 / 未知智能体名的调度条目：模型完整回显 prompt 模板（本身是
        # 合法 JSON）时，占位条目此前会被当作真实决策交给编排层执行；
        # key_entities 会被编排层直接落库成行程，同样清理模板占位值与非法日期
        raw_schedule = result.get("agent_schedule") or []
        filtered_schedule = self._valid_schedule(raw_schedule)
        if not filtered_schedule and raw_schedule:
            # 调度列表非空但过滤后为空：未知 / 占位名、字符串条目、缺
            # agent_name、空名字等全部形态都算——与编排层「全部非法 →
            # 默认信息查询」同一口径
            # 调度条目全部未知 / 占位（如 weather_query 等下划线变体或模板
            # 回显）：上游过滤成空后，编排层再也看不到「全部非法」这一状态，
            # 「降级默认信息查询」永不触发，用户请求会被静默吞成 no_agents
            # （前端渲染「已记录」）。与编排层同一口径降级。
            logger.warning("意图调度条目全部未知 / 占位，降级为默认信息查询")
            result = _default_intent_result(
                user_query, "调度条目均为未知 / 占位智能体"
            )
        else:
            result["agent_schedule"] = filtered_schedule
        result["key_entities"] = _clean_key_entities(result.get("key_entities"))

        # 将结果转换为JSON字符串，因为Msg的content必须是字符串
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
