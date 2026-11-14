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
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from agentscope.message import Msg

from agents.intention_agent import IntentionAgent
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from utils.agent_names import canonical_agent_name
from config import RESILIENCE_CONFIG
from context.memory_manager import MemoryManager
from context.short_term_memory import (
    RedisShortTermMemory,
    redis_short_term_available,
)
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.errors import MemoryPersistenceError, UserFacingError
from utils.json_safe import sanitize_json
from utils.llm_resilience import retry_with_backoff
from utils.paths import default_data_root
from utils.preference_values import has_preference_value
from utils.result_formatter import result_to_markdown
from utils.turn_meta import attach_turn_meta, build_turn_meta
from utils.usage import add_usage

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
        # 最近一次在途落库对应的提问 / 本轮回滚状态：落库失败或守卫否决时
        # 由完成回调触发回滚；下一轮开始前先等这次回滚结束，避免迟到的
        # 回滚删掉下一轮文本相同的提问
        self._last_persist_turn: tuple[str, dict[str, bool]] | None = None
        self._pending_rollback: asyncio.Future | None = None
        # 延迟记忆写入（偏好 / 行程）的提交任务：取消落在落库线程期间时由
        # 落库完成回调补提交，drain 释放用户锁前等待它结束
        self._pending_memory_commit: asyncio.Future | None = None
        # 失败 / 被墓碑否决回合的用量账目：这些回合没有消息可承载 _meta.usage，
        # 累计到下一轮成功回合一起写入，否则面板与趋势永久少记
        self._unsettled_usage: dict[str, Any] | None = None

        # 与 Web 面板 / CLI 命令同一数据目录：MemoryManager 默认的
        # "data/memory" 会锚定到包根（wheel 下是 site-packages），与
        # default_data_root()/memory 分叉——对话写入的会话 / 偏好 / 行程
        # 在面板、导出与删除里全部不可见，重装还会静默丢失
        self.memory = MemoryManager(
            user_id=user_id,
            session_id=session_id,
            storage_path=str(default_data_root() / "memory"),
            llm_model=model,
        )
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
             "elapsed": 秒, "persisted": 是否已写入长期记忆}
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
        # 本轮用量的归宿标记：_execute_turn 在每个终态（成功落库 / 记账 /
        # 取消且落库在途托管）置位；早于其内部 try 的取消 / 失败（等待
        # drain、意图调用等）由这里兜底记账——否则下一轮 usage_before 快照
        # 会把它们永久减掉，面板与趋势少记
        settlement = {"done": False}
        try:
            return await self._execute_turn(
                user_input, on_event, rc, start, usage_before, turn_id, settlement
            )
        except BaseException:
            if not settlement["done"]:
                self._accumulate_unsettled_usage(self._usage_delta(usage_before))
            raise
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
        settlement: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """一轮对话的主体（``ask`` 负责熔断名额的占用与归还）。"""
        settlement = settlement if settlement is not None else {"done": False}
        # 本轮开始时的会话纪元：必须在任何 await（上下文总结 / 意图 LLM
        # 重试可达数十秒）之前读取，否则这段窗口内的删除（纪元 +1 后
        # 「从未存在」路径撤销墓碑）会让落库守卫比较到删除后的值而放行，
        # 已删除会话的首轮问答被写回并复活
        epoch_reader = getattr(self.memory, "session_epoch", None)
        epoch_at_start = (
            await asyncio.to_thread(epoch_reader)
            if callable(epoch_reader)
            else None
        )
        guard_factory = getattr(self.memory, "persist_guard", None)
        # object.__new__ 构造的测试替身可能没有 _persist_guard：按无守卫处理
        base_guard = getattr(self, "_persist_guard", None)
        commit_guard = (
            guard_factory(epoch_at_start, base_guard)
            if callable(guard_factory)
            else base_guard
        )
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
            # 意图调用成功不重置熔断计数：只发生在编排腿上的子智能体
            # 429/5xx 否则每轮都被这里清零，永远累计不到阈值
            record_success=False,
        )

        try:
            intention_data = json.loads(intention.content)
        except json.JSONDecodeError as exc:
            raise UserFacingError("意图识别结果解析失败，请重试") from exc
        if not isinstance(intention_data, dict):
            raise UserFacingError("意图识别结果格式错误，请重试")
        # 模型输出可能含孤立代理项（如截断的 emoji 被转义成 \ud800）：事件
        # 序列化（SSE 帧 UTF-8）、落库（JSON 文件 / PostgreSQL）都会编码
        # 失败——在进入下游之前统一转义，整轮对话不再因此中断
        intention_data = sanitize_json(intention_data)

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
        pending_persist: asyncio.Future | None = None
        committed: bool | None = None
        usage_value: dict[str, Any] | None = None
        turn_state = {"rollback_requested": False}
        # 上一轮落库 / 回滚可能仍在途（用户停止后 drain 超时、后台回滚线程
        # 未结束）：先等它收尾，失败 / 否决时在这里补回滚，再暂存本轮提问。
        # 否则旧回调的回滚按内容匹配可能删掉本轮刚暂存的同一文本提问。
        # 落库线程超时仍未结束时只能继续（用户锁已在上一轮入口释放）。
        await self.drain_pending_persist()
        stage_future: "asyncio.Future | None" = None
        try:
            # 保存暂存任务：取消可能落在写入线程执行期间，回滚前必须等它
            # 落地，否则回滚读到的窗口里还没有这条提问、内容不匹配直接
            # 返回 False，随后写入落地留下无回答的孤儿提问
            stage_future = asyncio.ensure_future(
                asyncio.to_thread(self.memory.stage_message, "user", user_input)
            )
            await asyncio.shield(stage_future)
            # 用清洗后的意图数据重建 Msg 再交给编排器：原始 Msg 仍可能带
            # 孤立代理项，会进入编排 prompt / _update_memory 落库路径
            sanitized_intention = Msg(
                name="IntentionAgent",
                content=json.dumps(intention_data, ensure_ascii=False),
                role="assistant",
            )
            # 编排器的偏好 / 行程写库延迟到整轮落库之后（见下方 commit）：
            # 取消 / 墓碑否决的回合不能留下没有对应对话的幽灵记录。
            # 同时校验 reply 是否接受 defer_memory：测试 / 自定义编排器可能
            # 只替换了 reply，签名不兼容时按旧语义直接调用
            defer_supported = callable(
                getattr(self.orchestrator, "commit_deferred_memory", None)
            )
            retry_cache_supported = False
            if defer_supported:
                parameters: Iterable[inspect.Parameter]
                try:
                    parameters = inspect.signature(
                        self.orchestrator.reply
                    ).parameters.values()
                except (TypeError, ValueError):
                    parameters = ()
                defer_supported = any(
                    parameter.name == "defer_memory"
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
                retry_cache_supported = any(
                    parameter.name == "retry_cache"
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            # 整轮重试缓存：外层 retry_with_backoff 会重跑整个编排，缓存让
            # 重试跳过首轮已成功的智能体（一次 429/5xx 曾让同批已完成的
            # 智能体重复调用模型、放大账单与耗时）
            orchestration_retry_cache: dict[str, dict[str, Any]] = {}
            reply_options: dict[str, Any] = {}
            if defer_supported:
                reply_options["defer_memory"] = True
            if retry_cache_supported:
                reply_options["retry_cache"] = orchestration_retry_cache
            orchestration = await self._call_with_resilience(
                lambda: self.orchestrator.reply(
                    sanitized_intention,
                    on_event=on_event,
                    **reply_options,
                ),
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
            # 同上：先转义孤立代理项，避免事件 / 落库 / 响应序列化失败
            result_data = sanitize_json(result_data)

            agents = [
                {"name": item.get("agent_name", ""), "status": item.get("status", "unknown")}
                for item in result_data.get("results", [])
            ]
            usage = self._drain_unsettled_usage(self._usage_delta(usage_before))
            usage_value = usage
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
                    should_commit=commit_guard,
                )
            )
            self._pending_persist = pending
            pending_persist = pending
            self._last_persist_turn = (user_input, turn_state)
            pending.add_done_callback(
                lambda fut: self._clear_pending_persist(
                    fut, user_input, turn_state
                )
            )
            if defer_supported:
                pending.add_done_callback(self._commit_deferred_after_persist)
            persist_started = True
            committed = await asyncio.shield(pending)
        except BaseException:
            # 编排失败 / 取消：暂存的用户提问必须跟着回滚——保留的话，重试
            # 同一问题会在上下文里出现两条重复提问，而长期记忆里没有对应
            # 轮次，两层记忆从此不一致。落库线程已在途（用户停止时常常如此）
            # 就跳过：它会把整轮问答完整写入长期记忆。
            if not persist_started or self._pending_persist is None:
                await self._settle_staged_write(stage_future)
                await self._request_staged_rollback(user_input, turn_state)
            # 本轮用量没有写进任何持久化消息：记账到下一轮成功回合，否则
            # 下一轮的增量会把它们一起减掉，面板 / 趋势永久少记。取消时写
            # 线程可能仍在途——由完成回调按最终结果结算（成功则 _meta.usage
            # 已随助手消息写入，失败 / 墓碑否决才记账）；先前假定的「取消后
            # 写线程一定会成功」并不成立（PG 故障 / 会话刚被删除都会失败）
            pending_usage = (
                usage_value
                if usage_value is not None
                else self._usage_delta(usage_before)
            )
            self._settle_usage_after_persist(pending_persist, pending_usage)
            settlement["done"] = True
            # 在途落库的完成回调会在写入失败 / 守卫否决时补回滚
            raise
        if committed is False:
            # 存储层在写入锁内判定会话已删除（墓碑否决整轮）：本轮用量先
            # 记账（回滚过程中若再被取消，账目也不会随局部变量丢失），
            # 暂存的提问按删除语义回滚，避免墓碑随后被撤销（删除失败）时
            # 上下文里留下一条永远不会落库、也没有回答的孤立提问。
            self._accumulate_unsettled_usage(usage_value)
            # 立即置位：回滚等待期间若再被取消，ask() 外层兜底不会把同一
            # 回合用量重复记账（此前 done 在回滚之后才置位）
            settlement["done"] = True
            await self._request_staged_rollback(user_input, turn_state)
        if committed is not False:
            # 整轮已落库（_meta.usage 已写入消息）：先标记结算，再提交延迟
            # 记忆——提交期间被取消（停止 / 断连）时，ask() 外层兜底不能把
            # 同一份用量再记入下一轮（与 CLI 的 usage_settled 时序一致）
            settlement["done"] = True
            if defer_supported:
                # 现在才提交偏好 / 行程；取消与墓碑否决的回合不会执行到这里，
                # 避免台账出现没有对话的事实记录。落库完成回调可能已调度提交
                # （同样带墓碑复查），这里复用同一入口并等待其完成
                try:
                    self._schedule_deferred_commit()
                    pending_commit = getattr(self, "_pending_memory_commit", None)
                    if pending_commit is not None:
                        await asyncio.shield(pending_commit)
                except Exception:  # noqa: BLE001 - 逐条写入已吞异常，这里兜底
                    logger.warning("提交延迟记忆写入失败", exc_info=True)
        # 成功落库（True / 旧的 None 语义）时 _meta.usage 已在消息里；
        # 否决分支已记账——本轮用量已有归宿
        return {
            "reply": result_to_markdown(result_data),
            "agents": agents,
            "usage": usage,
            "elapsed": elapsed,
            # 落库结果要透传给客户端：墓碑否决（committed=False）时回答
            # 并没有写入历史，调用方不能把它当成功（Web 会提示"本轮未保存"）
            "persisted": committed is not False,
        }

    async def _request_staged_rollback(
        self, user_input: str, turn_state: dict[str, bool]
    ) -> None:
        """本轮只回滚一次（主路径与落库完成回调可能同时请求）。

        另一路径已在回滚时也做有限等待：调用方（``ask`` 返回前、drain 释放
        用户锁前）依赖回滚已落地，不能因为回调抢先置位就提前返回。
        """
        if turn_state["rollback_requested"]:
            await self._await_pending_rollback()
            return
        turn_state["rollback_requested"] = True
        await self._rollback_staged_user_message(user_input)

    async def _settle_staged_write(
        self, stage_future: "asyncio.Future | None"
    ) -> None:
        """取消发生在暂存写入线程期间：先等写入可见再回滚（有界）。

        ``stage_message`` 的 Redis / 文件写入在池线程执行，取消协程不会
        取消线程；写入尚未可见时回滚按末尾内容匹配会直接失败且不再重试，
        短期记忆留下无回答的孤儿提问。
        """
        if stage_future is None or stage_future.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(stage_future), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("等待暂存写入未在限时内完成，回滚可能跳过该条")
        except asyncio.CancelledError:
            logger.debug("等待暂存写入时被取消，回滚交给后台线程")
        except Exception:  # noqa: BLE001 - stage_message 自身吞异常，兜底
            logger.debug("暂存写入失败，回滚无需等待", exc_info=True)

    async def _await_pending_rollback(self, timeout_sec: float = 2.0) -> None:
        """等待在途回滚线程结束（有界；后台线程超时后自行完成）。"""
        task = getattr(self, "_pending_rollback", None)
        if task is None or task.done() or task is asyncio.current_task():
            # 自 await 会永远等不到自己（包装任务内部请求回滚时会走到这里）
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.debug("等待回滚暂存提问未在限时内完成，后台线程继续执行")
        except asyncio.CancelledError:
            # 调用方（except 处理器）的取消不能顶掉后续用量结算 / 状态置位：
            # 回滚线程自行完成，这里吞掉取消继续收尾
            logger.debug("等待回滚时被取消，回滚由后台线程完成")
        except Exception:  # noqa: BLE001 - 回滚尽力而为，不能顶掉调用方的错误
            logger.warning("回滚暂存提问失败", exc_info=True)

    async def _rollback_staged_user_message(self, user_input: str) -> None:
        """移除编排前暂存的用户提问（尽力而为，不阻塞取消 / 错误上报）。

        回滚本身是阻塞 IO，放线程池执行；用户停止请求时当前任务已处于取消
        状态，任何 await 都会被立即再次取消，因此只做有限等待，线程自行完成。
        """
        rollback = getattr(self.memory, "rollback_staged_message", None)
        if not callable(rollback):
            return
        task = asyncio.ensure_future(asyncio.to_thread(rollback, user_input))
        self._pending_rollback = task
        task.add_done_callback(self._consume_background_task)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.debug("回滚暂存提问未在限时内完成，后台线程继续执行")
        except Exception:  # noqa: BLE001 - 回滚尽力而为，不能顶掉调用方的错误
            logger.warning("回滚暂存提问失败", exc_info=True)

    def _commit_deferred_after_persist(self, fut: asyncio.Future) -> None:
        """落库完成回调：提交成功时补做延迟的偏好 / 行程写入。

        取消落在落库线程执行期间时，``ask()`` 的正常收尾路径不会执行，但
        整轮问答会被写线程完整提交——延迟的偏好 / 行程不能因此丢失（下一轮
        defer 会覆盖暂存负载）。否决（False）与写入失败不提交。
        """
        if fut.cancelled():
            return
        try:
            outcome = fut.result()
        except Exception:  # noqa: BLE001 - 落库失败走回滚，不提交记忆
            return
        if outcome is False:
            return
        self._schedule_deferred_commit()

    def _session_deleted_now(self) -> bool:
        """提交延迟记忆前复查共享墓碑（读取失败按未删除处理，保真优先）。"""
        check = getattr(self.memory, "session_deleted", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - 状态不明按未删除处理
            return False

    def _discard_deferred_memory(self) -> None:
        discard = getattr(self.orchestrator, "discard_deferred_memory", None)
        if callable(discard):
            discard()

    def _schedule_deferred_commit(self, check_deleted: bool = True) -> None:
        commit = getattr(self.orchestrator, "commit_deferred_memory", None)
        if not callable(commit):
            return
        if check_deleted:
            # 落库到提交之间会话可能被其它实例 / CLI 删除：先在线程里复查
            # 共享墓碑再提交，删除后丢弃负载（不写带 session_id 的幽灵行程）
            async def _commit_checked() -> bool:
                if await asyncio.to_thread(self._session_deleted_now):
                    self._discard_deferred_memory()
                    logger.info(
                        "会话已被删除，丢弃延迟记忆写入：%s", self.session_id
                    )
                    return False
                return bool(await commit())

            task: asyncio.Future = asyncio.ensure_future(_commit_checked())
        else:
            task = asyncio.ensure_future(commit())
        self._pending_memory_commit = task
        task.add_done_callback(self._consume_memory_commit)

    def _consume_memory_commit(self, task: asyncio.Future) -> None:
        if getattr(self, "_pending_memory_commit", None) is task:
            self._pending_memory_commit = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("提交延迟记忆写入失败", exc_info=exc)

    def _consume_background_task(self, task: asyncio.Future) -> None:
        # 完成即清空槽位：陈旧任务会让后续新调度的回滚无法登记，drain
        # 看到旧任务已完成就提前放行用户锁（回滚线程其实还没跑）
        if getattr(self, "_pending_rollback", None) is task:
            self._pending_rollback = None
        if not task.cancelled():
            # 后台任务无人 await：取走异常，避免未检索告警
            task.exception()

    def _clear_pending_persist(
        self,
        pending: asyncio.Future,
        user_input: str = "",
        turn_state: dict[str, bool] | None = None,
    ) -> None:
        if self._pending_persist is pending:
            self._pending_persist = None
        if pending.cancelled():
            # 取消后没人再 await 这个任务：取走异常，避免
            # 「Task exception was never retrieved」的告警
            return
        failed = object()
        try:
            outcome = pending.result()
        except Exception:  # noqa: BLE001 - 落库失败由回滚兜底
            outcome = failed
        if turn_state is None:
            return
        if outcome is not False and outcome is not failed:
            # True（已提交）或 None（旧实现无返回值按成功处理）：
            # 不需要回滚（标记后主路径 / drain 也不会再回滚）
            turn_state["rollback_requested"] = True
            return
        # 写入失败 / 守卫否决（会话已删除）：取消时跳过的回滚在这里补上
        task = asyncio.ensure_future(
            self._request_staged_rollback(user_input, turn_state)
        )
        current = getattr(self, "_pending_rollback", None)
        if current is None or current.done():
            # 任务尚未开始执行时也让 drain 能看到它，保证释放用户锁前等待。
            # 旧任务已完成时同样登记：陈旧槽位不能让 drain 提前放行
            self._pending_rollback = task
        task.add_done_callback(self._consume_background_task)

    async def drain_pending_persist(self, timeout_sec: float = 10.0) -> bool:
        """等待在途的记忆落库线程结束，返回是否在超时前完成。

        取消 ``ask()`` 只中断协程，``asyncio.to_thread`` 里的写线程不受影响。
        Web 入口在释放用户锁 / 会话占用标记前先等它结束，否则同一用户随后
        的删除会话 / 新一轮对话会与迟到的写入并发（已删除的会话被写回）。
        """
        pending = getattr(self, "_pending_persist", None)
        if pending is None:
            # 落库可能刚被完成回调清掉、回滚线程仍在跑：释放用户锁前也要等它；
            # 完成回调补提交的延迟记忆写入同样可能仍在后台（此前直接返回，
            # 用户锁在偏好 / 行程仍在落库时释放）
            await self._await_pending_rollback()
            await self._await_deferred_commit(timeout_sec)
            return True
        failed = object()
        outcome: object = None
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(pending), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            logger.warning(
                "等待记忆落库超过 %.1fs，写入线程仍在后台继续", timeout_sec
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 落库失败由回滚兜底
            outcome = failed
        last_turn = getattr(self, "_last_persist_turn", None)
        if last_turn is not None and (outcome is False or outcome is failed):
            # 取消时跳过的回滚：确认写入并未提交后在这里补一次（等待线程
            # 结束，保证释放用户锁前回滚已落地）
            await self._request_staged_rollback(*last_turn)
        await self._await_deferred_commit(timeout_sec)
        return True

    async def _await_deferred_commit(self, timeout_sec: float) -> None:
        """等待完成回调补提交的延迟记忆写入（尽力而为，超时后台继续）。"""
        commit_task = getattr(self, "_pending_memory_commit", None)
        if commit_task is None or commit_task.done():
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(commit_task), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            logger.warning(
                "等待延迟记忆写入超过 %.1fs，后台继续", timeout_sec
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 写入失败已逐条吞掉
            pass

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
        if not recent:
            # 进程重启 / 会话被 LRU 淘汰后短期记忆为空，而长期总结又排除了
            # 当前会话：从长期记忆补回本会话最近几轮，否则用户刷新页面看到
            # 完整历史、追问「把刚才的行程改到周五」时模型侧没有任何上文
            # （默认进程内短期记忆的部署必现）
            try:
                recent = list(
                    self.memory.long_term.get_chat_history(
                        limit=10, session_id=self.session_id
                    )
                )
            except Exception:  # noqa: BLE001 - 回填失败按无历史继续
                logger.debug("回填本会话历史失败", exc_info=True)
                recent = []
            if recent:
                # 回填必须写回短期记忆：编排器 _prepare_context 直接读
                # get_recent_context(3)，只放进意图上下文的话当轮子智能体
                # 看不到上文；且本轮落库后短期记忆非空，下一轮会跳过回填，
                # 闲置前的历史从第 2 轮起彻底丢失。写回失败按原路径继续。
                # Redis 降级窗口内不能写回：add_message 会以新 op_id 进入
                # 故障重放日志，恢复后重放把同一批消息再 RPUSH 一份（RPUSH
                # 带来重复 Q/A 并 LTRIM 挤掉更早的真实轮次）；本轮上下文
                # 仍使用回填结果，只是不写库。
                redis_degraded = False
                if isinstance(self.memory.short_term, RedisShortTermMemory):
                    try:
                        # 进程级标志只反映「最近一次真实操作」：启动时客户端
                        # 工厂就在冷却期（client=None）时标志仍为 True，但
                        # 此刻的 add_message 同样只进故障重放日志，恢复后
                        # 会把 Redis 里已存在的消息再 RPUSH 一份
                        redis_degraded = (
                            not redis_short_term_available()
                            or getattr(self.memory.short_term, "client", None)
                            is None
                        )
                    except Exception:  # noqa: BLE001 - 状态不明按可用处理
                        redis_degraded = False
                try:
                    if not redis_degraded:
                        for item in recent:
                            role = item.get("role")
                            content = item.get("content")
                            if role not in ("user", "assistant") or not isinstance(
                                content, str
                            ):
                                continue
                            self.memory.short_term.add_message(
                                role,
                                content,
                                timestamp=(
                                    item.get("timestamp")
                                    if isinstance(item.get("timestamp"), str)
                                    else None
                                ),
                            )
                    else:
                        # 降级窗口：写入进程内影子缓存（不登记故障重放），
                        # 编排器 / 子智能体本轮也能读到回填的历史；恢复后
                        # 以 Redis 真实内容校正影子缓存
                        seed = getattr(
                            self.memory.short_term,
                            "seed_fallback_context",
                            None,
                        )
                        if callable(seed):
                            seed(recent)
                except Exception:  # noqa: BLE001 - 写回失败不影响本轮
                    logger.debug("回填历史写回短期记忆失败", exc_info=True)
        return preferences, recent

    async def _call_with_resilience(
        self,
        factory,
        max_retries: int,
        base_delay: float,
        max_delay: float,
        record_success: bool = True,
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
                if record_success:
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

    def _accumulate_unsettled_usage(self, usage: dict[str, Any] | None) -> None:
        """未落库回合的用量记账：并入下一轮成功回合的 _meta.usage。

        用量是会话级累计器（只增不减）：失败 / 取消回合的模型调用若不计入
        任何消息，下一轮的增量快照会把它们一起减掉，Web 用量面板与近 7 天
        趋势因此永久少记。
        """
        if not usage:
            return
        self._unsettled_usage = add_usage(
            getattr(self, "_unsettled_usage", None), usage
        )

    def _settle_usage_after_persist(
        self,
        pending: asyncio.Future | None,
        usage: dict[str, Any] | None,
    ) -> None:
        """按落库最终结果结算本轮用量（可在完成回调线程 / 事件循环中调用）。

        - ``pending`` 为 None：本轮从未启动落库，直接记账；
        - 落库仍在途：挂完成回调，按最终结果结算（取消场景）；
        - 落库成功（含旧的 None 语义）：_meta.usage 已随助手消息写入，不重复计；
        - 落库失败 / 被取消 / 墓碑否决：本轮用量没有任何归宿，记账到下一轮。
        """
        if not usage:
            return
        if pending is not None and not pending.done():
            pending.add_done_callback(
                lambda fut: self._settle_usage_after_persist(fut, usage)
            )
            return
        written = False
        if pending is not None and not pending.cancelled():
            try:
                outcome = pending.result()
            except Exception:  # noqa: BLE001 - 落库失败由记账兜底
                outcome = False
            written = outcome is not False
        if not written:
            self._accumulate_unsettled_usage(usage)

    def _drain_unsettled_usage(
        self, usage: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """把之前未结算的用量并入本轮用量并清空账目（本轮若落库失败会再记账）。"""
        pending = getattr(self, "_unsettled_usage", None)
        if not pending:
            return usage
        self._unsettled_usage = None
        return add_usage(pending, usage)

    def _usage_snapshot(self) -> dict[str, Any] | None:
        tracker = getattr(self.model, "usage", None)
        return tracker.snapshot() if tracker is not None else None

    def _usage_delta(self, before: dict[str, Any] | None) -> dict[str, Any] | None:
        after = self._usage_snapshot()
        if before is None or after is None:
            return None
        return {key: after[key] - before[key] for key in before}
