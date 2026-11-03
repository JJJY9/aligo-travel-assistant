"""
对话运行时（Conversation Runtime）

封装一轮完整对话：意图识别 → 编排执行 → 记忆更新，并返回结构化结果，
供 Web 界面等调用方使用（CLI 目前保留自己的 Rich 展示流程）。

与 cli.py 中的处理流程保持一致：
- 上下文 = 用户偏好 / 历史会话总结 + 最近对话 + 当前输入；
- 意图识别与编排两次 LLM 调用都通过 retry_with_backoff 重试，并接入熔断器；
- 结果写入短期 / 长期记忆。
"""
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from agentscope.message import Msg

from agents.intention_agent import IntentionAgent
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from config import RESILIENCE_CONFIG
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff
from utils.result_formatter import result_to_markdown

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


class ConversationRuntime:
    """一个用户会话的完整对话流水线。"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        model: Any,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.model = model
        self.circuit_breaker = circuit_breaker

        self.memory = MemoryManager(user_id=user_id, session_id=session_id, llm_model=model)
        self.intention_agent = IntentionAgent(name="IntentionAgent", model=model)
        self.agent_cache: dict[str, Any] = {}
        self.registry = LazyAgentRegistry(
            model=model,
            cache=self.agent_cache,
            memory_manager=self.memory,
        )
        self.orchestrator = OrchestrationAgent(
            agent_registry=self.registry,
            memory_manager=self.memory,
        )

    async def ask(
        self,
        user_input: str,
        on_event: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """处理一条用户消息。

        Args:
            user_input: 用户输入
            on_event: 进度回调（可选）。事件类型：
                phase / plan / agent_start / agent_done

        Returns:
            {"reply": Markdown 文本, "agents": [{name, status}],
             "usage": {calls, input_tokens, output_tokens, total_tokens, time} | None,
             "elapsed": 秒}
        """
        rc = RESILIENCE_CONFIG
        start = time.time()
        usage_before = self._usage_snapshot()

        if self.circuit_breaker:
            self.circuit_breaker.raise_if_open()

        self._emit(on_event, {"type": "phase", "phase": "context", "label": "整理记忆与上下文"})

        # 多会话写入后保持长期记忆最新（避免覆盖其它会话刚写入的数据）
        self.memory.reload_long_term()

        context = await self._build_context(user_input)
        self._emit(on_event, {"type": "phase", "phase": "intent", "label": "理解你的需求"})

        intention = await self._call_with_resilience(
            lambda: self.intention_agent.reply(context),
            max_retries=int(rc.get("max_retries", 3)),
            base_delay=float(rc.get("retry_base_delay_sec", 1.0)),
            max_delay=float(rc.get("retry_max_delay_sec", 30.0)),
        )

        try:
            intention_data = json.loads(intention.content)
        except json.JSONDecodeError as exc:
            raise ValueError("意图识别结果解析失败，请重试") from exc
        if not isinstance(intention_data, dict):
            raise ValueError("意图识别结果格式错误，请重试")

        schedule = intention_data.get("agent_schedule") or []
        self._emit(
            on_event,
            {
                "type": "plan",
                "intents": intention_data.get("intents") or [],
                "reasoning": intention_data.get("reasoning") or "",
                "schedule": [
                    {
                        "agent_name": item.get("agent_name", ""),
                        "priority": item.get("priority", 0),
                        "reason": item.get("reason", ""),
                    }
                    for item in schedule
                    if isinstance(item, dict)
                ],
            },
        )
        logger.info("Plan: %s agents", len(schedule))

        self.memory.add_message("user", user_input)
        orchestration = await self._call_with_resilience(
            lambda: self.orchestrator.reply(intention, on_event=on_event),
            max_retries=int(rc.get("max_retries", 3)),
            base_delay=float(rc.get("retry_base_delay_sec", 1.0)),
            max_delay=float(rc.get("retry_max_delay_sec", 30.0)),
        )

        try:
            result_data = json.loads(orchestration.content)
        except json.JSONDecodeError as exc:
            raise ValueError("编排结果解析失败，请重试") from exc

        self.memory.add_message("assistant", json.dumps(result_data, ensure_ascii=False))

        agents = [
            {"name": item.get("agent_name", ""), "status": item.get("status", "unknown")}
            for item in result_data.get("results", [])
        ]
        return {
            "reply": result_to_markdown(result_data),
            "agents": agents,
            "usage": self._usage_delta(usage_before),
            "elapsed": round(time.time() - start, 2),
        }

    @staticmethod
    def _emit(on_event: ProgressCallback | None, event: dict[str, Any]) -> None:
        """安全地发出进度事件（回调异常不影响主流程）。"""
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - 进度展示失败不应中断对话
            logger.debug("Progress callback failed", exc_info=True)

    async def _build_context(self, user_input: str) -> list[Msg]:
        """组装意图识别上下文：系统记忆（偏好 + 历史摘要）+ 最近对话 + 当前输入。"""
        memory_parts: list[str] = []

        preferences = {key: value for key, value in self.memory.long_term.get_preference().items() if value}
        if preferences:
            pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]
            for key, value in preferences.items():
                if isinstance(value, list):
                    value = ", ".join(str(item) for item in value)
                pref_lines.append(f"• {key}: {value}")
            memory_parts.append("\n".join(pref_lines))

        try:
            summary = await self.memory.get_long_term_summary_async(max_messages=30)
        except Exception:
            summary = ""
        if summary:
            memory_parts.append(f"【历史会话总结】\n{summary}")

        messages: list[Msg] = []
        if memory_parts:
            messages.append(Msg(name="system", content="\n\n".join(memory_parts), role="system"))
        for message in self.memory.short_term.get_recent_context(n_turns=5):
            messages.append(Msg(name=message["role"], content=message["content"], role=message["role"]))
        messages.append(Msg(name="user", content=user_input, role="user"))
        return messages

    async def _call_with_resilience(self, factory, max_retries: int, base_delay: float, max_delay: float):
        """带重试与熔断记录的单次 LLM 调用。"""
        try:
            result = await retry_with_backoff(
                factory,
                max_retries=max_retries,
                base_delay_sec=base_delay,
                max_delay_sec=max_delay,
            )
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
            raise

    def _usage_snapshot(self) -> dict[str, Any] | None:
        tracker = getattr(self.model, "usage", None)
        return tracker.snapshot() if tracker is not None else None

    def _usage_delta(self, before: dict[str, Any] | None) -> dict[str, Any] | None:
        after = self._usage_snapshot()
        if before is None or after is None:
            return None
        return {key: after[key] - before[key] for key in before}
