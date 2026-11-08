"""
对话运行时（Conversation Runtime）

封装一轮完整对话：意图识别 → 编排执行 → 记忆更新，并返回结构化结果，
供 Web 界面等调用方使用（CLI 目前保留自己的 Rich 展示流程）。

与 cli.py 中的处理流程保持一致：
- 上下文 = 用户偏好 / 历史会话总结 + 最近对话 + 当前输入；
- 意图识别与编排两次 LLM 调用都通过 retry_with_backoff 重试，并接入熔断器；
- 结果写入短期 / 长期记忆。
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from agentscope.message import Msg

from agents.intention_agent import IntentionAgent
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from utils.agent_names import canonical_agent_name
from config import RESILIENCE_CONFIG
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.errors import MemoryPersistenceError, UserFacingError
from utils.llm_resilience import retry_with_backoff
from utils.preference_values import has_preference_value
from utils.result_formatter import result_to_markdown
from utils.turn_meta import attach_turn_meta, build_turn_meta

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
        persist_guard: Callable[[], bool] | None = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.model = model
        self.circuit_breaker = circuit_breaker
        # 落库守卫：存储层在写入锁 / 事务内调用，返回 False（会话刚被删除）
        # 时整轮不落库，避免在途落库与删除并发时把已删除会话写回
        self._persist_guard = persist_guard
        # 本轮流式请求是否已向熔断器记录过成功 / 失败（归还半开名额用）
        self._probe_recorded = False
        # 在途的记忆落库任务：取消 ask() 只取消协程，执行器线程仍会写完；
        # Web 入口在释放用户锁前先 drain 它（见 drain_pending_persist）
        self._pending_persist: asyncio.Future | None = None

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
        turn_id: str = "",
    ) -> dict[str, Any]:
        """处理一条用户消息。

        Args:
            user_input: 用户输入
            on_event: 进度回调（可选）。事件类型：
                phase / plan / agent_start / agent_done
            turn_id: 客户端回合标识（可选）：写入助手消息的 _meta，供前端
                在停止 / 断网后精确对账「这一轮是否已落库」

        Returns:
            {"reply": Markdown 文本, "agents": [{name, status}],
             "usage": {calls, input_tokens, output_tokens, total_tokens, time} | None,
             "elapsed": 秒}
        """
        rc = RESILIENCE_CONFIG
        start = time.time()
        usage_before = self._usage_snapshot()

        # 半开状态下 raise_if_open 会占用唯一的试探名额。本轮若在真正调用
        # LLM 之前失败或被取消（用户点停止 / 断网），成功与失败都没有记录，
        # 名额会一直占到 recovery_timeout 之后，期间其它用户全部被拒绝；
        # finally 中归还（记录过成功 / 失败时 release_probe 不产生效果）
        self._probe_recorded = False
        if self.circuit_breaker:
            self.circuit_breaker.raise_if_open()
        try:
            return await self._execute_turn(
                user_input, on_event, rc, start, usage_before, turn_id
            )
        finally:
            if self.circuit_breaker and not self._probe_recorded:
                self.circuit_breaker.release_probe()

    async def _execute_turn(
        self,
        user_input: str,
        on_event: ProgressCallback | None,
        rc: Any,
        start: float,
        usage_before: dict[str, Any] | None,
        turn_id: str = "",
    ) -> dict[str, Any]:
        """一轮对话的主体（``ask`` 负责熔断名额的占用与归还）。"""
        self._emit(on_event, {"type": "phase", "phase": "context", "label": "整理记忆与上下文"})

        # 多会话写入后保持长期记忆最新（避免覆盖其它会话刚写入的数据）。
        # 读盘 / 查询 / Redis 都是阻塞调用，放到线程池，避免卡住事件循环
        # （Web 端同时服务多个 SSE 流，这里的毫秒级阻塞会放大到所有连接）
        await asyncio.to_thread(self.memory.reload_long_term)

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
            raise UserFacingError("意图识别结果解析失败，请重试") from exc
        if not isinstance(intention_data, dict):
            raise UserFacingError("意图识别结果格式错误，请重试")

        schedule = intention_data.get("agent_schedule")
        if not isinstance(schedule, list):
            # 模型可能返回非列表（如单个对象 / 数字），直接迭代会 TypeError
            schedule = []
        intents = intention_data.get("intents")
        if not isinstance(intents, list):
            intents = []
        self._emit(
            on_event,
            {
                "type": "plan",
                "intents": intents,
                "reasoning": intention_data.get("reasoning") or "",
                "schedule": self._normalized_schedule(schedule),
            },
        )
        logger.info("Plan: %s agents", len(schedule))

        # 长期 / 短期记忆写入是阻塞 IO（JSON 文件、PostgreSQL、Redis），
        # 放到线程池执行，避免阻塞事件循环。本轮提问先只进短期记忆：
        # 编排智能体从短期记忆读取「用户本轮原话」，编排失败 / 用户停止时
        # 长期记忆里不会留下没有回复的孤儿提问（CLI 同款时序）
        persist_started = False
        try:
            await asyncio.to_thread(self.memory.stage_message, "user", user_input)
            orchestration = await self._call_with_resilience(
                lambda: self.orchestrator.reply(intention, on_event=on_event),
                max_retries=int(rc.get("max_retries", 3)),
                base_delay=float(rc.get("retry_base_delay_sec", 1.0)),
                max_delay=float(rc.get("retry_max_delay_sec", 30.0)),
            )

            try:
                result_data = json.loads(orchestration.content)
            except json.JSONDecodeError as exc:
                raise UserFacingError("编排结果解析失败，请重试") from exc
            if not isinstance(result_data, dict):
                raise UserFacingError("编排结果格式错误，请重试")

            agents = [
                {"name": item.get("agent_name", ""), "status": item.get("status", "unknown")}
                for item in result_data.get("results", [])
            ]
            usage = self._usage_delta(usage_before)
            elapsed = round(time.time() - start, 2)
            meta = build_turn_meta(intention_data, result_data, usage=usage, elapsed=elapsed)
            if turn_id:
                meta["client_turn_id"] = turn_id
            # 编排成功：整轮问答在一次线程调用里原子提交（用户 + 助手）。
            # 分成两次提交时，两次之间被取消 / 第二次写失败会在长期记忆留下
            # 没有回答的孤儿提问。落库任务独立于 ask 协程：取消只让这里抛出，
            # 写线程会继续完成，调用方用 drain_pending_persist() 等它结束。
            assistant_payload = json.dumps(
                attach_turn_meta(result_data, meta), ensure_ascii=False
            )
            pending = asyncio.ensure_future(
                asyncio.to_thread(
                    self.memory.persist_turn,
                    user_input,
                    assistant_payload,
                    should_commit=self._persist_guard,
                )
            )
            self._pending_persist = pending
            pending.add_done_callback(self._clear_pending_persist)
            persist_started = True
            await asyncio.shield(pending)
        except BaseException:
            # 编排失败 / 取消：暂存的用户提问必须跟着回滚——保留的话，重试
            # 同一问题会在上下文里出现两条重复提问，而长期记忆里没有对应
            # 轮次，两层记忆从此不一致。落库线程已在途（用户停止时常常如此）
            # 就跳过：它会把整轮问答完整写入长期记忆。
            if not persist_started or self._pending_persist is None:
                await self._rollback_staged_user_message(user_input)
            raise
        return {
            "reply": result_to_markdown(result_data),
            "agents": agents,
            "usage": usage,
            "elapsed": elapsed,
        }

    async def _rollback_staged_user_message(self, user_input: str) -> None:
        """移除编排前暂存的用户提问（尽力而为，不阻塞取消 / 错误上报）。

        回滚本身是阻塞 IO，放线程池执行；用户停止请求时当前任务已处于取消
        状态，任何 await 都会被立即再次取消，因此只做有限等待，线程自行完成。
        """
        rollback = getattr(self.memory, "rollback_staged_message", None)
        if not callable(rollback):
            return
        task = asyncio.ensure_future(asyncio.to_thread(rollback, user_input))
        task.add_done_callback(self._consume_background_task)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.debug("回滚暂存提问未在限时内完成，后台线程继续执行")

    @staticmethod
    def _consume_background_task(task: asyncio.Future) -> None:
        if not task.cancelled():
            # 后台任务无人 await：取走异常，避免未检索告警
            task.exception()

    def _clear_pending_persist(self, pending: asyncio.Future) -> None:
        if self._pending_persist is pending:
            self._pending_persist = None
        if not pending.cancelled():
            # 取消后没人再 await 这个任务：取走异常，避免
            # 「Task exception was never retrieved」的告警
            pending.exception()

    async def drain_pending_persist(self, timeout_sec: float = 10.0) -> bool:
        """等待在途的记忆落库线程结束，返回是否在超时前完成。

        取消 ``ask()`` 只中断协程，``asyncio.to_thread`` 里的写线程不受影响。
        Web 入口在释放用户锁 / 会话占用标记前先等它结束，否则同一用户随后
        的删除会话 / 新一轮对话会与迟到的写入并发（已删除的会话被写回）。
        """
        pending = getattr(self, "_pending_persist", None)
        if pending is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.warning(
                "等待记忆落库超过 %.1fs，写入线程仍在后台继续", timeout_sec
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 落库失败已由 ask() 调用方处理
            return True
        return True

    def close(self) -> None:
        """释放会话占用的资源（PostgreSQL 连接等）。"""
        close = getattr(self.memory.long_term, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("关闭长期记忆连接失败", exc_info=True)

    @staticmethod
    def _emit(on_event: ProgressCallback | None, event: dict[str, Any]) -> None:
        """安全地发出进度事件（回调异常不影响主流程）。"""
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - 进度展示失败不应中断对话
            logger.debug("Progress callback failed", exc_info=True)

    @staticmethod
    def _normalized_schedule(schedule: list[Any]) -> list[dict[str, Any]]:
        """进度事件的调度计划：统一名称、过滤空白项与非法优先级。"""
        normalized: list[dict[str, Any]] = []
        for item in schedule:
            if not isinstance(item, dict):
                continue
            name = str(item.get("agent_name") or "").strip()
            if not name:
                continue
            try:
                priority = int(item.get("priority", 0))
            except (TypeError, ValueError, OverflowError):
                priority = 0
            normalized.append(
                {
                    "agent_name": canonical_agent_name(name),
                    "priority": priority,
                    "reason": item.get("reason", ""),
                }
            )
        return normalized

    async def _build_context(self, user_input: str) -> list[Msg]:
        """组装意图识别上下文：系统记忆（偏好 + 历史摘要）+ 最近对话 + 当前输入。

        偏好与最近对话的读取会访问 JSON 文件 / Redis，放到线程池执行，
        避免阻塞事件循环。
        """
        preferences_raw, recent_context = await asyncio.to_thread(
            self._memory_snapshot
        )
        memory_parts: list[str] = []

        preferences = {
            key: value
            for key, value in preferences_raw.items()
            if has_preference_value(value)
        }
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
        for message in recent_context:
            # 短期记忆可能含可解析但形状不对的条目（人工修复 / 外部写入）：
            # 直接下标会让该会话每轮都 KeyError 500，这里同样跳过
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant", "system"):
                continue
            if not isinstance(content, str):
                continue
            messages.append(Msg(name=role, content=content, role=role))
        messages.append(Msg(name="user", content=user_input, role="user"))
        return messages

    def _memory_snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """同步读取记忆快照（偏好 + 最近对话），供 ``asyncio.to_thread`` 调用。"""
        preferences = self.memory.long_term.get_preference()
        recent = list(self.memory.short_term.get_recent_context(n_turns=5))
        return preferences, recent

    async def _call_with_resilience(
        self, factory, max_retries: int, base_delay: float, max_delay: float
    ):
        """带重试与熔断记录的单次 LLM 调用。

        只有调用本身抛错才计为熔断失败。编排结果 ``partial_failure`` 是
        子智能体（RAG / 天气等）的问题，不是 LLM 服务不可用：把它计为失败
        会让 CLOSED 状态下每轮先被意图调用的成功记录清零（永远累计不到
        阈值），也会让 HALF_OPEN 状态下的第一个真实请求被非 LLM 故障重新
        打开全局共享熔断器，误伤所有用户。
        """
        try:
            result = await retry_with_backoff(
                factory,
                max_retries=max_retries,
                base_delay_sec=base_delay,
                max_delay_sec=max_delay,
            )
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
                self._probe_recorded = True
            return result
        except CircuitOpenError:
            raise
        except MemoryPersistenceError:
            # 记忆写盘失败与 LLM 服务无关，不计入熔断（否则多次磁盘故障会让
            # 所有用户一起收到「服务暂时不可用」）
            raise
        except Exception:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
                self._probe_recorded = True
            raise

    def _usage_snapshot(self) -> dict[str, Any] | None:
        tracker = getattr(self.model, "usage", None)
        return tracker.snapshot() if tracker is not None else None

    def _usage_delta(self, before: dict[str, Any] | None) -> dict[str, Any] | None:
        after = self._usage_snapshot()
        if before is None or after is None:
            return None
        return {key: after[key] - before[key] for key in before}
