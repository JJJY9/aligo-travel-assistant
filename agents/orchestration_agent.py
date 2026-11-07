"""
协调器智能体 OrchestrationAgent
职责：根据意图识别结果，协调调度多个子智能体完成任务

核心功能：
1. 接收 IntentionAgent 的调度决策
2. 按照优先级顺序执行子智能体
3. 管理智能体之间的消息传递
4. 聚合多个智能体的结果
5. 与三层记忆系统集成

执行模式：
- Sequential (顺序执行): 按优先级依次执行，前一个的输出作为后一个的输入
- Parallel (并行执行): 同时执行多个智能体（暂不实现）
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from agentscope.agent import AgentBase
from agentscope.message import Msg

from utils.agent_names import SKILL_AGENT_NAMES as _SKILL_AGENT_NAMES
from utils.llm_resilience import retry_with_backoff
from utils.preference_values import has_preference_value, merge_preference_value

logger = logging.getLogger(__name__)

# 单个子智能体的执行上限与瞬时错误重试：防止一个卡住的智能体让整轮对话
# （以及该用户的会话锁）无限等待；重试只针对可重试的 LLM / 网络错误
_AGENT_TIMEOUT_SEC = 300.0
_AGENT_RETRY_MAX = 1
_AGENT_RETRY_BASE_DELAY = 1.0
_AGENT_RETRY_MAX_DELAY = 5.0

# 行程规划依赖事项收集与偏好抽取的结果：两者同批时规划拿不到这些上下文
_PLANNING_DEPENDENCIES = {"event_collection", "preference"}

# 技能智能体通常把 LLM / 网络异常吞成 {"error": ...} 返回，外层重试等不到
# 异常；识别这些瞬时错误标记并重新抛出，走与真实异常相同的退避重试
_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "connection",
    "rate limit",
    "too many requests",
    "unavailable",
    "overloaded",
    "429",
    "502",
    "503",
    "504",
    "超时",
    "连接",
    "限流",
    "暂时不可用",
)


_TEXT_SCALAR_KEYS = ("city", "name", "destination", "value", "text", "label")


def _as_text(value: Any) -> str:
    """把模型输出（可能为列表 / 对象 / 数字 / None）转为可持久化的单行文本。

    对象不能直接 str()：Python repr 是单引号伪 JSON，会污染行程历史与
    ``frequent_destinations`` 统计键并让去重永远失配；优先取常见标量字段
    （``{"city": "杭州"}`` → ``杭州``），否则输出合法 JSON 文本。
    """
    if isinstance(value, (list, tuple)):
        return "、".join(text for text in (_as_text(item) for item in value) if text)
    if isinstance(value, dict):
        for key in _TEXT_SCALAR_KEYS:
            if key in value:
                text = _as_text(value.get(key))
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False)
    return str(value or "").strip()


def _transient_agent_error(response: Any) -> str | None:
    """技能返回的 error JSON 若像瞬时故障，返回错误文本以触发重试。"""
    content = getattr(response, "content", None)
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not error:
        return None
    text = str(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS):
        return text
    return None

# 注册器同时接受技能目录名与旧智能体名（plan-trip / event-collection 等）；
# 下游的聚合、记忆写入与前端展示统一使用旧名称。映射放在
# utils.agent_names，供轮次元数据等模块共用

ProgressCallback = Callable[[dict[str, Any]], None]


class OrchestrationAgent(AgentBase):
    """协调器智能体 - 调度和协调多个子智能体"""

    def __init__(
        self,
        name: str = "OrchestrationAgent",
        agent_registry: Any = None,  # dict 或 LazyAgentRegistry（鸭子类型）
        memory_manager: Any = None,
        **kwargs
    ):
        """
        初始化协调器

        Args:
            name: 智能体名称
            agent_registry: 子智能体注册表 {agent_name: agent_instance}
            memory_manager: 记忆管理器
        """
        super().__init__()
        self.name = name
        self.agent_registry = agent_registry or {}
        self.memory_manager = memory_manager

    def register_agent(self, agent_name: str, agent: AgentBase):
        """注册子智能体"""
        self.agent_registry[agent_name] = agent
        logger.info(f"Registered agent: {agent_name}")

    def unregister_agent(self, agent_name: str):
        """注销子智能体"""
        if agent_name in self.agent_registry:
            del self.agent_registry[agent_name]
            logger.info(f"Unregistered agent: {agent_name}")

    async def reply(
        self,
        x: Msg | list[Msg] | None = None,
        on_event: ProgressCallback | None = None,
    ) -> Msg:
        """
        协调执行流程

        Args:
            x: 输入消息，应包含 IntentionAgent 的输出
            on_event: 进度回调（可选），接收 agent_start / agent_done 事件

        Returns:
            Msg: 执行结果
        """
        if x is None:
            return Msg(
                name=self.name,
                content=json.dumps({"error": "No input provided"}),
                role="assistant"
            )

        # 解析输入
        if isinstance(x, list):
            intention_output = x[-1].content if x else "{}"
        else:
            intention_output = x.content

        # 解析意图识别结果
        try:
            intention_data = json.loads(intention_output) if isinstance(intention_output, str) else intention_output
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse intention output: {e}")
            return Msg(
                name=self.name,
                content=json.dumps({"error": "Invalid intention format"}),
                role="assistant"
            )

        # 获取智能体调度计划
        if not isinstance(intention_data, dict):
            logger.error("Intention output is not a JSON object")
            return Msg(
                name=self.name,
                content=json.dumps({"error": "Invalid intention format"}),
                role="assistant",
            )
        # 规范化调度计划：LLM 可能返回字符串项 / 单对象 / 非法 priority，
        # 直接 sorted 会抛 TypeError 让整轮对话 500
        raw_schedule = intention_data.get("agent_schedule", [])
        if isinstance(raw_schedule, dict):
            raw_schedule = [raw_schedule]
        if not isinstance(raw_schedule, list):
            raw_schedule = []
        agent_schedule: list[dict[str, Any]] = []
        for task in raw_schedule:
            if not isinstance(task, dict):
                continue
            name = str(task.get("agent_name") or "").strip()
            if not name:
                continue
            # 存回裁剪后的名称：canonical 映射与注册表查找都依赖它，
            # 带空白的原始值会让 " plan-trip " 匹配不到任何智能体
            task["agent_name"] = name
            agent_schedule.append(task)
        for task in agent_schedule:
            try:
                task["priority"] = int(task.get("priority", 999))
            except (TypeError, ValueError, OverflowError):
                task["priority"] = 999

        # 行程规划必须在事项收集 / 偏好抽取之后一批执行，否则拿不到本轮结果
        def _canonical(task: dict[str, Any]) -> str:
            name = str(task.get("agent_name") or "")
            return _SKILL_AGENT_NAMES.get(name, name)

        # 同一智能体可能以「技能目录名 + 标准名」或重复条目出现：注册表同时
        # 接受两种写法，不去重会被并行执行多次（重复调用 LLM、偏好重复落库、
        # 回复出现两份结果）。按 canonical 名保留第一次出现的条目
        deduped_schedule: list[dict[str, Any]] = []
        seen_agents: set[str] = set()
        for task in agent_schedule:
            canonical = _canonical(task)
            if canonical in seen_agents:
                logger.warning("调度计划中重复的智能体已去重：%s", canonical)
                continue
            seen_agents.add(canonical)
            deduped_schedule.append(task)
        agent_schedule = deduped_schedule

        dependency_priorities = [
            task["priority"]
            for task in agent_schedule
            if _canonical(task) in _PLANNING_DEPENDENCIES
        ]
        if dependency_priorities:
            planning_floor = max(dependency_priorities) + 1
            for task in agent_schedule:
                if (
                    _canonical(task) == "itinerary_planning"
                    and task["priority"] <= max(dependency_priorities)
                ):
                    task["priority"] = planning_floor
        if not agent_schedule:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "no_agents",
                    "message": "没有需要调度的智能体"
                }),
                role="assistant"
            )

        # 按优先级排序
        sorted_schedule = sorted(agent_schedule, key=lambda x: x["priority"])

        logger.info(f"Orchestrating {len(sorted_schedule)} agents")

        # 准备上下文信息（短期 / 长期记忆读取是阻塞 IO，放到线程池，
        # 避免配置 Redis / PostgreSQL 时卡住共享事件循环）
        context = await asyncio.to_thread(self._prepare_context, intention_data)

        # 并行执行智能体（按优先级分组）
        results: list[dict[str, Any]] = []
        current_priority = None
        parallel_tasks = []

        for task in sorted_schedule:
            priority = task.get("priority", 0)

            # 如果优先级变化，先执行当前批次
            if current_priority is not None and priority != current_priority:
                # 并行执行当前优先级的所有任务
                if parallel_tasks:
                    batch_results = await self._execute_parallel_agents(
                        parallel_tasks,
                        self._context_with_previous_preferences(context, results),
                        results,
                        on_event,
                    )
                    results.extend(batch_results)
                    parallel_tasks = []

            current_priority = priority
            parallel_tasks.append(task)

        # 执行最后一批
        if parallel_tasks:
            batch_results = await self._execute_parallel_agents(
                parallel_tasks,
                self._context_with_previous_preferences(context, results),
                results,
                on_event,
            )
            results.extend(batch_results)

        # 聚合结果
        final_result = self._aggregate_results(results, intention_data)

        # 更新记忆（长期 / 短期写入是阻塞 IO，放到线程池）
        if self.memory_manager:
            await asyncio.to_thread(self._update_memory, intention_data, results)

        return Msg(
            name=self.name,
            content=json.dumps(final_result, ensure_ascii=False),
            role="assistant"
        )

    def _prepare_context(self, intention_data: dict[str, Any]) -> dict[str, Any]:
        """
        准备上下文信息，供子智能体使用

        Args:
            intention_data: 意图识别结果

        Returns:
            上下文字典
        """
        context = {
            "reasoning": intention_data.get("reasoning", ""),
            "intents": intention_data.get("intents", []),
            "key_entities": intention_data.get("key_entities", {}),
            "rewritten_query": intention_data.get("rewritten_query", "")
        }

        # 从记忆系统获取上下文
        if self.memory_manager:
            short_term = getattr(self.memory_manager, "short_term", None)
            recent_context = (
                short_term.get_recent_context(3) if short_term is not None else []
            )
            context["recent_dialogue"] = recent_context
            # 用户本轮原话：偏好等抽取只依据该文本，避免把上下文 / 助手建议
            # 里的内容误当成用户偏好
            context["user_query"] = next(
                (
                    str(item.get("content") or "")
                    for item in reversed(recent_context)
                    if item.get("role") == "user"
                ),
                "",
            )

            # 长期记忆：用户偏好
            preferences = self.memory_manager.long_term.get_preference()
            context["user_preferences"] = preferences

        return context

    def _context_with_previous_preferences(
        self, context: dict[str, Any], previous_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """把前序批次里偏好智能体抽到的新偏好合并进上下文。

        偏好在全部智能体执行完才落库，后续批次的行程规划需要先看到本轮
        新偏好（例如用户刚说「我搬到上海了」就开始规划行程）。
        """
        merged = dict(context)
        preferences = dict(context.get("user_preferences") or {})
        for result in previous_results:
            if result.get("agent_name") != "preference":
                continue
            # 抽取失败（status=error）的偏好不能进入后续批次上下文：界面与
            # 聚合结果已把它标为失败，携带的 preferences 可能是残缺 / 错误的
            # （与 _update_memory 的落库守卫一致），会误导行程规划
            if (result.get("result") or {}).get("status") != "success":
                continue
            data = (result.get("result") or {}).get("data") or {}
            if not isinstance(data, dict):
                continue
            raw = data.get("preferences")
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    pref_type = item.get("type")
                    value = item.get("value")
                    if not isinstance(pref_type, str) or not pref_type.strip():
                        # 模型可能把 type 包成数组 / 对象：不可哈希值当偏好键
                        # 会抛 TypeError（上下文合并）或污染长期记忆
                        continue
                    pref_type = pref_type.strip()
                    if not has_preference_value(value):
                        continue
                    action = str(item.get("action") or "replace").strip().lower()
                    if action in {"append", "add", "追加"}:
                        # 与持久化路径一致：列表值逐项合并，不能把整个列表
                        # 塞进列表（嵌套列表会让后续 ", ".join 报错）
                        preferences[pref_type] = merge_preference_value(
                            preferences.get(pref_type), value
                        )
                    else:
                        preferences[pref_type] = value
            elif isinstance(raw, dict):
                for pref_type, value in raw.items():
                    if pref_type in {"has_preferences", "error"}:
                        continue
                    if has_preference_value(value):
                        preferences[pref_type] = value
        merged["user_preferences"] = preferences
        return merged

    async def _execute_parallel_agents(
        self,
        tasks: list[dict],
        context: dict[str, Any],
        previous_results: list[dict],
        on_event: ProgressCallback | None = None,
    ) -> list[dict]:
        """
        并行执行多个智能体

        Args:
            tasks: 任务列表，每个任务包含 agent_name, priority, reason, expected_output
            context: 上下文信息
            previous_results: 前序智能体的结果
            on_event: 进度回调

        Returns:
            执行结果列表
        """
        if not tasks:
            return []

        # 如果只有一个任务，直接执行
        if len(tasks) == 1:
            task = tasks[0]
            result: dict[str, Any] = await self._execute_agent(
                agent_name=str(task.get("agent_name") or ""),
                context=context,
                reason=task.get("reason", ""),
                expected_output=task.get("expected_output", ""),
                previous_results=previous_results,
                on_event=on_event,
            )
            requested_name = str(task.get("agent_name") or "")
            return [{
                "agent_name": _SKILL_AGENT_NAMES.get(requested_name, requested_name),
                "priority": task.get("priority", 0),
                "result": result
            }]

        # 多个任务并行执行
        logger.info(f"Executing {len(tasks)} agents in parallel")

        # 创建并行任务
        parallel_coroutines = []
        for task in tasks:
            agent_name = str(task.get("agent_name") or "")
            priority = task.get("priority", 0)
            reason = task.get("reason", "")
            expected_output = task.get("expected_output", "")

            logger.info(f"Parallel executing agent: {agent_name} (priority={priority})")

            # 创建协程
            coroutine = self._execute_agent(
                agent_name=agent_name,
                context=context,
                reason=reason,
                expected_output=expected_output,
                previous_results=previous_results,
                on_event=on_event,
            )
            parallel_coroutines.append((agent_name, priority, coroutine))

        # 使用 asyncio.gather 并行执行
        execution_results = await asyncio.gather(
            *[coro for _, _, coro in parallel_coroutines],
            return_exceptions=True
        )

        # 整理结果
        results: list[dict[str, Any]] = []
        for (agent_name, priority, _), exec_result in zip(parallel_coroutines, execution_results):
            canonical_name = _SKILL_AGENT_NAMES.get(agent_name, agent_name)
            if isinstance(exec_result, BaseException):
                logger.error(f"Parallel agent execution failed: {agent_name}, error: {exec_result}")
                result = {
                    "status": "error",
                    "agent_name": canonical_name,
                    "data": {"error": str(exec_result)},
                    "message": f"并行执行失败: {exec_result!s}"
                }
            else:
                result = exec_result

            results.append({
                "agent_name": canonical_name,
                "priority": priority,
                "result": result
            })

        return results

    @staticmethod
    def _emit(on_event: ProgressCallback | None, event: dict[str, Any]) -> None:
        """安全地发出进度事件（回调异常不影响调度）。"""
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - 进度展示失败不应中断执行
            logger.debug("Progress callback failed", exc_info=True)

    async def _execute_agent(
        self,
        agent_name: str,
        context: dict[str, Any],
        reason: str,
        expected_output: str,
        previous_results: list[dict],
        on_event: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """执行单个智能体，并在开始 / 结束时发出进度事件。"""
        # 注册器两种名称都接受，但事件与结果统一用标准名
        canonical_name = _SKILL_AGENT_NAMES.get(agent_name, agent_name)
        self._emit(on_event, {"type": "agent_start", "agent_name": canonical_name})
        started = time.perf_counter()
        result = await self._run_agent(
            agent_name=agent_name,
            context=context,
            reason=reason,
            expected_output=expected_output,
            previous_results=previous_results,
        )
        duration = round(time.perf_counter() - started, 2)
        result["duration"] = duration
        self._emit(
            on_event,
            {
                "type": "agent_done",
                "agent_name": canonical_name,
                "status": result.get("status", "unknown"),
                "message": result.get("message", ""),
                "duration": duration,
            },
        )
        return result

    async def _run_agent(
        self,
        agent_name: str,
        context: dict[str, Any],
        reason: str,
        expected_output: str,
        previous_results: list[dict]
    ) -> dict[str, Any]:
        """
        执行单个智能体

        Args:
            agent_name: 智能体名称
            context: 上下文信息
            reason: 调用原因
            expected_output: 期望输出
            previous_results: 前序智能体的结果

        Returns:
            执行结果
        """
        # 检查智能体是否注册
        if agent_name not in self.agent_registry:
            logger.warning(f"Agent not registered: {agent_name}")
            error_message = f"智能体未注册: {agent_name}"
            return {
                "status": "error",
                # data.error 供结果聚合与 CLI 展示使用（此前只放在 message 字段，
                # 聚合后错误原因会丢失，界面显示为"未知错误"）
                "data": {"error": error_message},
                "message": error_message,
            }

        # 构建输入消息
        input_msg = Msg(
            name="Orchestrator",
            content=json.dumps({
                "context": context,
                "reason": reason,
                "expected_output": expected_output,
                "previous_results": previous_results
            }, ensure_ascii=False),
            role="user"
        )

        try:
            # 懒加载失败（缺依赖 / 模型文件损坏）也必须落在下面的 except 里，
            # 否则错误会绕过 per-agent 处理让整轮对话 500、进度停在执行中。
            # 首次加载 RAG 会导入 torch / 加载嵌入模型（约 10s），放到线程池
            # 避免卡住事件循环（SSE 心跳与其它会话）；加载与调用一起受超时
            # 约束，卡在导入 / 模型加载上也不会无限占用整轮与该用户的会话锁
            async def _load_and_reply() -> Any:
                agent = await asyncio.get_running_loop().run_in_executor(
                    None, self.agent_registry.__getitem__, agent_name
                )

                async def _reply_once() -> Any:
                    # 调用智能体：瞬时 LLM / 网络错误重试一次。技能智能体
                    # 会把异常吞成 error JSON 返回，这里识别瞬时标记后重新
                    # 抛出（ConnectionError 属可重试类型），让退避重试生效
                    response = await agent.reply(input_msg)
                    error_text = _transient_agent_error(response)
                    if error_text is not None:
                        raise ConnectionError(f"瞬时错误：{error_text}")
                    return response

                return await retry_with_backoff(
                    _reply_once,
                    max_retries=_AGENT_RETRY_MAX,
                    base_delay_sec=_AGENT_RETRY_BASE_DELAY,
                    max_delay_sec=_AGENT_RETRY_MAX_DELAY,
                )

            response = await asyncio.wait_for(
                _load_and_reply(), timeout=_AGENT_TIMEOUT_SEC
            )

            # 解析响应
            if isinstance(response.content, str):
                try:
                    result = json.loads(response.content)
                except json.JSONDecodeError:
                    result = {"output": response.content}
            else:
                result = response.content

            # 智能体可能用 error 字段或 status=error 表示失败；
            # error 为 null / 空串时是正常输出，不能当作失败
            if isinstance(result, dict):
                error_value = result.get("error")
                state = str(result.get("status") or "").lower()
                if error_value or state == "error":
                    error_msg = (
                        error_value
                        or result.get("message")
                        or "智能体返回错误状态"
                    )
                    return {
                        "status": "error",
                        "agent_name": agent_name,
                        "data": result,
                        "message": error_msg,
                    }

            return {
                "status": "success",
                "agent_name": agent_name,
                "data": result
            }

        except asyncio.TimeoutError:
            logger.error("Agent execution timed out: %s", agent_name)
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": {"error": f"执行超时（>{_AGENT_TIMEOUT_SEC:g} 秒）"},
                "message": "智能体执行超时",
            }

        except Exception as e:
            logger.error(f"Agent execution failed: {agent_name}, error: {e}")
            # 返回友好的错误信息，但不中断流程
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": {"error": str(e)},
                "message": f"智能体执行失败: {e!s}"
            }

    def _aggregate_results(
        self,
        results: list[dict],
        intention_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        聚合多个智能体的结果

        Args:
            results: 所有智能体的执行结果
            intention_data: 原始意图识别结果

        Returns:
            聚合后的最终结果
        """
        aggregated: dict[str, Any] = {
            "status": "completed",
            "intention": {
                "intents": intention_data.get("intents", []),
                "key_entities": intention_data.get("key_entities", {})
            },
            "agents_executed": len(results),
            "results": []
        }

        # 收集每个智能体的结果
        for result in results:
            aggregated["results"].append({
                "agent_name": result["agent_name"],
                "priority": result["priority"],
                "status": result["result"].get("status", "unknown"),
                "data": result["result"].get("data", {}),
                "duration": result["result"].get("duration"),
            })

        # 检查是否有错误
        errors = [r for r in results if r["result"].get("status") == "error"]
        if errors:
            aggregated["status"] = "partial_failure"
            aggregated["errors"] = len(errors)

        return aggregated

    def _update_memory(self, intention_data: dict[str, Any], results: list[dict]):
        """
        更新记忆系统

        Args:
            intention_data: 意图识别结果
            results: 智能体执行结果
        """
        if not self.memory_manager:
            return

        # 提取并保存信息到长期记忆
        for result in results:
            agent_name = result["agent_name"]
            data = result["result"].get("data", {})

            # 如果是偏好智能体，保存偏好信息到长期记忆
            if agent_name == "preference" and isinstance(data, dict):
                # 抽取失败（status=error）的结果不能落库：界面与聚合已把它
                # 标为失败，携带的 preferences 可能是残缺 / 错误的（与下方
                # 行程分支同一防护）
                if result["result"].get("status") != "success":
                    continue
                preferences_data = data.get("preferences", {})

                # 新格式：preferences 是列表，包含 {type, value, action}
                if isinstance(preferences_data, list):
                    for pref_item in preferences_data:
                        if not isinstance(pref_item, dict):
                            continue

                        pref_type = pref_item.get("type")
                        pref_value = pref_item.get("value")
                        pref_action = str(pref_item.get("action") or "replace").strip().lower()

                        if not isinstance(pref_type, str) or not pref_type.strip():
                            # 不可哈希的 type（数组 / 对象）会作为偏好键写入
                            # 长期记忆，此后每次 get_preference 都抛 TypeError
                            continue
                        pref_type = pref_type.strip()
                        if not has_preference_value(pref_value):
                            continue

                        # 根据 action 决定操作（模型可能返回 Append / add / 追加 等变体）
                        if pref_action in {"append", "add", "追加"}:
                            append_pref = getattr(
                                self.memory_manager.long_term,
                                "append_preference",
                                None,
                            )
                            if callable(append_pref):
                                # 存储层在锁内重新读取再合并，避免覆盖本轮
                                # LLM 调用期间用户在记忆面板写入的偏好
                                merged = append_pref(pref_type, pref_value)
                                logger.info(
                                    f"Appended to {pref_type}: {pref_value}, total: {merged}"
                                )
                                continue
                            # 旧实现 / 自定义后端没有原子追加时的兜底：与
                            # 持久化 / 上下文路径一致，用 merge_preference_value
                            # 逐项合并（列表值整体 append 会形成嵌套列表）
                            current_prefs = self.memory_manager.long_term.get_preference()
                            existing_value = current_prefs.get(pref_type)
                            merged_value = merge_preference_value(
                                existing_value, pref_value
                            )
                            self.memory_manager.long_term.save_preference(
                                pref_type, merged_value
                            )
                            logger.info(
                                f"Appended to {pref_type}: {pref_value}, "
                                f"total: {merged_value}"
                            )
                        else:
                            # 覆盖模式：直接保存新值
                            self.memory_manager.long_term.save_preference(pref_type, pref_value)
                            logger.info(f"Replaced {pref_type}: {pref_value}")

                # 旧格式兼容：preferences 是字典
                elif isinstance(preferences_data, dict):
                    for pref_type, value in preferences_data.items():
                        if (
                            has_preference_value(value)
                            and pref_type not in {"has_preferences", "error"}
                        ):
                            self.memory_manager.long_term.save_preference(pref_type, value)
                            logger.info(f"Updated {pref_type}: {value} (legacy format)")

            # 如果是行程规划智能体，保存行程到长期记忆
            if agent_name == "itinerary_planning" and isinstance(data, dict):
                # 规划失败时 plan-trip 返回的兜底 itinerary 不是真实行程，
                # 不能写入历史/统计/日历
                if result["result"].get("status") != "success":
                    continue
                itinerary = data.get("itinerary", {})

                # 只要有行程信息就保存（不管是否完全规划好）
                if itinerary:
                    # 提取事项收集的信息（出发地、目的地等）
                    event_data = {}
                    for r in results:
                        if r["agent_name"] == "event_collection":
                            event_data = r["result"].get("data", {})
                            break

                    # 从 event_data 获取行程信息；模型可能给出列表（多目的地）
                    # 或数字，统一转成单行文本再落库（不可哈希值会让统计
                    # 更新抛 TypeError，整轮回复作废）
                    origin = _as_text(event_data.get("origin"))
                    destination = _as_text(event_data.get("destination"))
                    start_date = _as_text(event_data.get("start_date"))
                    end_date = _as_text(event_data.get("end_date"))
                    purpose = _as_text(event_data.get("trip_purpose")) or "旅游"

                    # 保存到长期记忆：必须有出发日期——没有日期的行程无法参与
                    # 去重（_find_same_trip 对缺 start_date 直接跳过），复述或
                    # 「重新生成」会让同一条无日期行程被反复写入、统计累加
                    if destination and start_date:
                        self.memory_manager.long_term.save_trip_history({
                            "origin": origin,
                            "destination": destination,
                            "start_date": start_date,
                            "end_date": end_date,
                            "purpose": purpose,
                            "session_id": getattr(self.memory_manager, "session_id", None),
                        })
                        logger.info(f"Saved trip to long-term memory: {origin} -> {destination}")

        logger.info("Memory updated after orchestration")
