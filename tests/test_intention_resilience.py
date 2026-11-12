"""
意图识别智能体异常传播测试（无需 API Key）

覆盖 Issue #1：
1. 可重试错误（连接失败等）必须向上抛出，供 cli.py 的 retry_with_backoff 与熔断器处理；
2. 不可重试错误（模型返回无法解析的文本）保持降级为默认结果。

运行：python tests/test_intention_resilience.py
"""
import asyncio
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg

from agents.intention_agent import IntentionAgent


class _ConnectionBoomModel:
    """始终抛连接错误的模型（模拟 LLM 服务不可达）"""

    async def __call__(self, messages):
        raise ConnectionError("connection refused")


class _UnparsableModel:
    """返回无法解析文本的模型（模拟模型输出格式错误）"""

    async def __call__(self, messages):
        return "这不是 JSON，无法解析"


class _AuthError(Exception):
    def __init__(self):
        super().__init__("Error code: 401 - Authentication Fails")
        self.status_code = 401


class _AuthBoomModel:
    """始终抛 401 的模型（密钥失效 / 余额耗尽）"""

    async def __call__(self, messages):
        raise _AuthError()


class _EmptyModel:
    """网关以 200 返回空错误体 / choices 为空"""

    async def __call__(self, messages):
        return ""


class _ErrorBodyModel:
    """网关以 200 返回错误体（限流 / 鉴权失败）"""

    def __init__(self, body):
        self.body = body

    async def __call__(self, messages):
        return json.dumps(self.body)


def test_empty_response_is_retriable_error():
    """空响应按可重试错误上抛（重试 + 熔断），不能降级成默认意图。"""
    agent = IntentionAgent(name="IntentionAgent", model=_EmptyModel())
    try:
        asyncio.run(agent.reply([_user_msg()]))
    except ConnectionError:
        print("✓ 空响应按可重试错误上抛")
        return
    raise AssertionError("空响应被吞成默认意图")


def test_http_200_error_body_is_llm_error():
    """200 + 错误体：按错误体状态码分类上抛（429 重试 / 401 永久）。"""
    from utils.llm_resilience import (
        is_permanent_llm_error,
        is_retriable_error,
    )

    cases = [
        ({"error": {"code": 429, "message": "rate limited"}}, True, False),
        ({"error": {"code": 401, "message": "auth failed"}}, False, True),
    ]
    for body, retriable, permanent in cases:
        agent = IntentionAgent(
            name="IntentionAgent", model=_ErrorBodyModel(body)
        )
        try:
            asyncio.run(agent.reply([_user_msg()]))
        except Exception as exc:  # noqa: BLE001 - 断言分类结果
            assert is_retriable_error(exc) is retriable, (body, exc)
            assert is_permanent_llm_error(exc) is permanent, (body, exc)
        else:
            raise AssertionError(f"200 错误体被吞成默认意图：{body}")
    print("✓ 200 错误体按状态码触发重试 / 熔断")


class _TrailingProseModel:
    """合法 JSON 后带说明文字（其中含花括号）的输出"""

    async def __call__(self, messages):
        return (
            '{"reasoning": "用户要出行", "intents": [{"type": "plan_trip", "confidence": 0.9}], '
            '"key_entities": {}, "agent_schedule": [{"agent_name": "itinerary_planning", "priority": 1}]}\n'
            "提示：如 {日期} 未提供可默认明天。"
        )


class _NestedFragmentModel:
    """返回带说明文字、只含嵌套片段的输出（缺少 agent_schedule）"""

    async def __call__(self, messages):
        return '分析结果：{"type": "greeting", "confidence": 0.9}，其余省略'


class _CaptureModel:
    """记录 prompt 并返回空调度结果的模型。"""

    def __init__(self):
        self.prompts = []

    async def __call__(self, messages):
        self.prompts.append(messages)
        return json.dumps(
            {
                "reasoning": "x",
                "intents": [],
                "key_entities": {},
                "rewritten_query": "x",
                "agent_schedule": [],
            }
        )


def _user_msg():
    return Msg(name="user", content="帮我规划去北京的行程", role="user")


def test_retriable_error_is_raised():
    """可重试错误不应被吞掉，否则外层重试与熔断永远不会触发"""
    agent = IntentionAgent(name="IntentionAgent", model=_ConnectionBoomModel())
    try:
        asyncio.run(agent.reply([_user_msg()]))
    except ConnectionError:
        print("✓ 可重试错误已向上抛出（重试/熔断可用）")
        return
    raise AssertionError("可重试错误被吞掉：reply() 应向上抛出 ConnectionError")


def test_permanent_llm_error_is_raised():
    """401 等 4xx 模型调用失败必须上抛：否则熔断器把 LLM 不可用记为成功。"""
    agent = IntentionAgent(name="IntentionAgent", model=_AuthBoomModel())
    try:
        asyncio.run(agent.reply([_user_msg()]))
    except _AuthError:
        print("✓ 不可重试的模型调用错误已向上抛出（熔断器可记账）")
        return
    raise AssertionError("401 被吞成默认意图：熔断器与健康检查将永远看不到故障")


def test_unparsable_output_falls_back():
    """模型输出无法解析属于不可重试错误，应保持降级行为"""
    agent = IntentionAgent(name="IntentionAgent", model=_UnparsableModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert "意图识别出错" in data.get("reasoning", ""), "应降级为默认结果"
    assert data.get("agent_schedule"), "降级结果应包含默认调度"
    print("✓ 不可重试错误保持降级为默认结果")


def test_json_with_trailing_prose_braces_parses():
    """结尾说明里的花括号不能影响 JSON 解析（否则规划请求被降级为信息查询）。"""
    agent = IntentionAgent(name="IntentionAgent", model=_TrailingProseModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert data["intents"][0]["type"] == "plan_trip"
    assert data["agent_schedule"][0]["agent_name"] == "itinerary_planning"
    print("✓ JSON 后带花括号说明文字仍能解析")


class _NanModel:
    """合法 JSON 但包含 NaN（Python 默认接受，标准 JSON 不允许）"""

    async def __call__(self, messages):
        return '{"intents": [{"type": "plan_trip", "confidence": NaN}], "agent_schedule": []}'


class _ArrayModel:
    """顶层是数组而不是对象"""

    async def __call__(self, messages):
        return "[]"


def test_non_finite_json_falls_back():
    agent = IntentionAgent(name="IntentionAgent", model=_NanModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert "意图识别出错" in data.get("reasoning", "")
    print("✓ 含 NaN 的输出降级为默认结果（不会写入记忆）")


def test_non_object_json_falls_back():
    agent = IntentionAgent(name="IntentionAgent", model=_ArrayModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert "意图识别出错" in data.get("reasoning", "")
    assert isinstance(data, dict) and data.get("agent_schedule")
    print("✓ 顶层非对象输出降级为默认结果")


def test_nested_fragment_falls_back_to_default_schedule():
    """扫描到的 JSON 片段缺少 agent_schedule 时不能当成完整意图（否则 no_agents 吞请求）。"""
    agent = IntentionAgent(name="IntentionAgent", model=_NestedFragmentModel())
    out = asyncio.run(agent.reply([_user_msg()]))
    data = json.loads(out.content)
    assert "意图识别出错" in data.get("reasoning", "")
    assert data["agent_schedule"][0]["agent_name"] == "information_query"
    print("✓ 嵌套 JSON 片段降级为默认调度")


def test_long_input_is_truncated():
    """超长输入在意图 prompt 中截断，避免撑爆上下文后被降级并持久化原文。"""
    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    asyncio.run(
        agent.reply(Msg(name="user", content="长" * 5000, role="user"))
    )
    prompt = str(model.prompts[0])
    assert "输入过长已截断" in prompt
    assert len(prompt) < 20000
    print("✓ 超长输入在意图 prompt 中截断")


class _FragmentBeforeFullModel:
    """说明文字里先出现一个合法片段，完整的意图对象在后面。"""

    async def __call__(self, messages):
        return (
            '分析：用户提到 {"destination": "北京"}，最终：'
            '{"reasoning": "规划", "intents": [{"type": "plan_trip"}], '
            '"key_entities": {"destination": "北京"}, '
            '"agent_schedule": [{"agent_name": "itinerary_planning", "priority": 1}]}'
        )


class _TemplateEchoModel:
    """先回显带占位名的调度模板，再给出真实结果：应取最后一个。"""

    async def __call__(self, messages):
        return (
            '{"agent_schedule": [{"agent_name": "子智能体名称", "priority": 1}]}\n'
            '{"reasoning": "r", "intents": [], '
            '"agent_schedule": [{"agent_name": "information_query", "priority": 1}]}'
        )


def test_fragment_before_full_object_keeps_real_schedule():
    """说明文字里的片段不能抢在完整意图结果之前（否则调度整体降级）。"""
    agent = IntentionAgent(name="IntentionAgent", model=_FragmentBeforeFullModel())
    data = json.loads(asyncio.run(agent.reply(_user_msg())).content)
    assert data["agent_schedule"][0]["agent_name"] == "itinerary_planning"
    assert "意图识别出错" not in data.get("reasoning", "")
    print("✓ 片段在前、完整意图在后时采用完整结果")


def test_template_echo_before_real_result_uses_last():
    """回显的占位调度模板在前时，应取后面真实结果而不是占位名。"""
    agent = IntentionAgent(name="IntentionAgent", model=_TemplateEchoModel())
    data = json.loads(asyncio.run(agent.reply(_user_msg())).content)
    assert data["agent_schedule"][0]["agent_name"] == "information_query"
    print("✓ 多次出现 agent_schedule 时采用最后一个")


class _TemplateEchoThenTruncatedModel:
    """回显模板在前、正式意图结果被 max_tokens 截断：模板不能当真。"""

    async def __call__(self, messages):
        return (
            '{"reasoning": "这里是详细的推理过程", "intents": [{"type": "意图类型"}], '
            '"key_entities": {"origin": "出发地（如果有）"}, '
            '"rewritten_query": "标准化、补全后的查询内容", '
            '"agent_schedule": [{"agent_name": "子智能体名称", "priority": 1}]}\n'
            '正式结果：{"reasoning": "用户要规划行程", '
            '"key_entities": {"destination": "杭'
        )


def test_template_echo_with_truncated_result_falls_back_to_default():
    """模板回显 + 正式结果截断：降级默认调度，不执行占位智能体。"""
    agent = IntentionAgent(
        name="IntentionAgent", model=_TemplateEchoThenTruncatedModel()
    )
    data = json.loads(asyncio.run(agent.reply(_user_msg())).content)
    names = [item.get("agent_name") for item in data["agent_schedule"]]
    assert "子智能体名称" not in names, data
    assert names == ["information_query"], data
    assert "意图识别出错" in data.get("reasoning", "")
    print("✓ 模板回显 + 正式结果截断时降级默认调度")


def test_single_msg_call_does_not_leak_previous_history():
    """同一实例混用列表与单条 Msg 输入时，上一轮历史不能串进本次 prompt。"""
    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    stale = "上一轮的独特历史短语ABC"
    asyncio.run(
        agent.reply(
            [
                Msg(name="user", content=stale, role="user"),
                Msg(name="user", content="帮我规划", role="user"),
            ]
        )
    )
    asyncio.run(agent.reply(_user_msg()))
    second_prompt = str(model.prompts[1])
    assert stale not in second_prompt, "单条 Msg 调用不能带上一轮历史"
    print("✓ 单条 Msg 调用不串上一轮历史")


def test_content_block_list_is_rendered_as_text():
    """Msg.content 为 agentscope content block 列表时按纯文本进 prompt。"""
    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    msg = Msg(
        name="user",
        content=[{"type": "text", "text": "帮我规划去上海的行程"}],
        role="user",
    )
    asyncio.run(agent.reply(msg))
    prompt = str(model.prompts[0])
    assert "帮我规划去上海的行程" in prompt
    assert "[{'type'" not in prompt and "{'type'" not in prompt
    print("✓ content block 列表按纯文本渲染")


class _NonListScheduleModel:
    """把 agent_schedule 误写成字符串 / 对象的模型输出。"""

    def __init__(self, schedule):
        self.schedule = schedule

    async def __call__(self, messages):
        return json.dumps(
            {
                "reasoning": "x",
                "intents": [],
                "key_entities": {},
                "rewritten_query": "q",
                "agent_schedule": self.schedule,
            },
            ensure_ascii=False,
        )


def test_non_list_schedule_falls_back_to_default():
    """agent_schedule 为字符串 / null 时降级为默认调度，不能静默 no_agents。"""
    for schedule in ("itinerary_planning", None, {"agent_name": "x"}):
        agent = IntentionAgent(
            name="IntentionAgent", model=_NonListScheduleModel(schedule)
        )
        result = json.loads(asyncio.run(agent.reply(_user_msg())).content)
        planned = result.get("agent_schedule")
        assert isinstance(planned, list) and planned, (schedule, planned)
        assert planned[0]["agent_name"] == "information_query", (schedule, planned)
    print("✓ 非列表 agent_schedule 降级为默认信息查询")


def test_message_text_and_none_input_edges():
    """_message_text 的字符串列表 / 非容器形态；None 输入直接返回空对象。"""
    from agents.intention_agent import _message_text

    assert _message_text(["a", "b"]) == "ab"
    assert _message_text(None) == ""
    assert _message_text(3) == "3"

    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    out = asyncio.run(agent.reply(None))
    assert out.content == "{}"
    assert model.prompts == [], "None 输入不应调用模型"
    print("✓ _message_text 边界形态与 None 输入")


def test_long_history_content_is_truncated_in_prompt():
    """历史消息超过 800 字时在 prompt 中截断并标注省略。"""
    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    history = [
        Msg(name="assistant", content="旧" * 1000, role="assistant"),
        Msg(name="user", content="继续规划", role="user"),
    ]
    asyncio.run(agent.reply(history))
    prompt = str(model.prompts[0])
    assert "..." in prompt, "超长历史应截断并标注省略"
    assert "旧" * 900 not in prompt, "历史内容必须被截到 800 字以内"
    print("✓ 超长历史内容在 prompt 中截断")


def test_system_memory_entry_kept_in_prompt():
    """system 角色的历史（长期记忆）完整保留并作为上下文首段。"""
    model = _CaptureModel()
    agent = IntentionAgent(name="IntentionAgent", model=model)
    history = [
        Msg(name="system", content="长期记忆：用户常住北京", role="system"),
        Msg(name="user", content="帮我规划", role="user"),
        Msg(name="user", content="顺便查下天气", role="user"),
    ]
    asyncio.run(agent.reply(history))
    prompt = str(model.prompts[0])
    assert "[系统记忆]" in prompt and "用户常住北京" in prompt
    assert "用户: 帮我规划" in prompt, "对话历史按角色进入上下文"
    assert prompt.index("[系统记忆]") < prompt.index("用户: 帮我规划"), (
        "长期记忆应排在对话历史之前"
    )
    print("✓ system 记忆注入 prompt")


class _MissingScheduleModel:
    async def __call__(self, messages):
        return '{"reasoning": "r", "intents": []}'


def test_missing_agent_schedule_falls_back():
    """合法 JSON 但缺 agent_schedule：降级为默认调度，不能当成空调度。"""
    agent = IntentionAgent(name="IntentionAgent", model=_MissingScheduleModel())
    data = json.loads(asyncio.run(agent.reply(_user_msg())).content)
    assert "意图识别出错" in data.get("reasoning", "")
    assert data["agent_schedule"][0]["agent_name"] == "information_query"
    print("✓ 缺少 agent_schedule 降级为默认调度")


class _RepairedBadScheduleModel:
    async def __call__(self, messages):
        # 尾逗号让直接解析与对象扫描都失败；修复解析能拿到对象，但
        # agent_schedule 是数字——应走「类型非法」降级而不是原样返回
        return '说明：{"reasoning": "r", "agent_schedule": 5,}'


def test_repaired_json_with_non_list_schedule_falls_back():
    """修复解析得到的对象里 agent_schedule 非列表同样降级为默认调度。"""
    agent = IntentionAgent(name="IntentionAgent", model=_RepairedBadScheduleModel())
    data = json.loads(asyncio.run(agent.reply(_user_msg())).content)
    assert "意图识别出错" in data.get("reasoning", "")
    assert data["agent_schedule"][0]["agent_name"] == "information_query"
    print("✓ 修复解析后的非列表调度同样降级")


if __name__ == "__main__":
    test_retriable_error_is_raised()
    test_permanent_llm_error_is_raised()
    test_empty_response_is_retriable_error()
    test_http_200_error_body_is_llm_error()
    test_unparsable_output_falls_back()
    test_json_with_trailing_prose_braces_parses()
    test_non_finite_json_falls_back()
    test_non_object_json_falls_back()
    test_nested_fragment_falls_back_to_default_schedule()
    test_long_input_is_truncated()
    test_fragment_before_full_object_keeps_real_schedule()
    test_template_echo_before_real_result_uses_last()
    test_template_echo_with_truncated_result_falls_back_to_default()
    test_single_msg_call_does_not_leak_previous_history()
    test_content_block_list_is_rendered_as_text()
    test_non_list_schedule_falls_back_to_default()
    test_message_text_and_none_input_edges()
    test_system_memory_entry_kept_in_prompt()
    test_long_history_content_is_truncated_in_prompt()
    test_missing_agent_schedule_falls_back()
    test_repaired_json_with_non_list_schedule_falls_back()
    print("\n全部通过")
