"""
AgentScope Configuration for Aligo Multi-Agent Travel Planning System
适配 AgentScope 1.0.16+
"""
import agentscope

from config import LLM_CONFIG


def init_agentscope():
    """
    初始化AgentScope

    注意：AgentScope 1.0.16+ 版本的API已改变：
    - init()函数不再接受model_configs参数
    - 模型配置改为直接在Agent初始化时指定
    """
    agentscope.init(
        project="Aligo-Travel-Planning",
        name="multi_agent_system",
        logging_level="INFO"
    )

    print(f"✓ AgentScope initialized (version: {agentscope.__version__})")


class UsageTracker:
    """累计模型调用的次数、token 用量与调用耗时。"""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.time = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record_usage(self, usage) -> None:
        """累计一次调用的 usage（agentscope ChatUsage，dict 子类，可能为 None）。"""
        if not usage:
            return
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        self.time += float(usage.get("time", 0.0) or 0.0)

    def snapshot(self) -> dict:
        """当前用量快照（用于计算单次查询的增量）。"""
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "time": self.time,
        }

    def summary_text(self) -> str:
        return (
            f"{self.calls} 次调用 · tokens {self.total_tokens}"
            f"（输入 {self.input_tokens} / 输出 {self.output_tokens}）"
        )


class UsageTrackingModel:
    """模型代理：透明转发调用，并累计每次调用的 token 用量。

    说明：agentscope 的非流式响应 ChatResponse 携带 usage（ChatUsage）；
    若底层模型没有返回 usage，则只统计调用次数、不累计 token。
    其余属性（stream / generate_kwargs 等）透明代理到底层模型。
    """

    def __init__(self, model) -> None:
        self._model = model
        self.usage = UsageTracker()

    async def __call__(self, messages, **kwargs):
        response = await self._model(messages, **kwargs)
        self.usage.calls += 1
        usage = (
            response.get("usage")
            if isinstance(response, dict)
            else getattr(response, "usage", None)
        )
        self.usage.record_usage(usage)
        return response

    def __getattr__(self, item):
        # 仅在常规属性查找失败时触发；透明代理底层模型属性
        return getattr(self._model, item)


def unwrap_usage_model(model):
    """返回用量跟踪包装下的底层模型；非包装模型原样返回。"""
    return model._model if isinstance(model, UsageTrackingModel) else model


def wrap_usage_tracking(model):
    """为单个会话创建独立的用量跟踪包装。

    底层模型（HTTP 客户端 / 连接池）仍共享，只把计数分开，避免多会话并发时
    各自的 token 增量互相污染（甚至出现负数）。
    """
    return UsageTrackingModel(unwrap_usage_model(model))


def create_chat_model(
    api_key: str | None = None,
    model_name: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_retries: int | None = None,
):
    """
    创建 OpenAI 兼容的聊天模型实例（默认 DeepSeek）。

    针对 agentscope 1.0.16 的两个注意点：
    1. `temperature` / `max_tokens` 必须通过 `generate_kwargs` 传入，
       作为构造函数顶层参数会被静默忽略（只打印 Unknown keyword arguments 警告）；
    2. 其流式解析在 DeepSeek 等标准 OpenAI 兼容服务上会崩溃
       （delta.audio 为 None 时仍执行 `"data" in None`），因此统一使用
       非流式（stream=False）。非流式响应为 ChatResponse（dict 子类，
       content 为文本块列表），解析统一走 utils.json_parser.extract_response_text。
    """
    from agentscope.model import OpenAIChatModel

    from config import SYSTEM_CONFIG

    return UsageTrackingModel(
        OpenAIChatModel(
            model_name=model_name or LLM_CONFIG["model_name"],
            api_key=LLM_CONFIG["api_key"] if api_key is None else api_key,
            stream=False,
            client_kwargs={
                "base_url": base_url or LLM_CONFIG["base_url"],
                "timeout": float(
                    timeout if timeout is not None else SYSTEM_CONFIG.get("timeout", 60)
                ),
                # OpenAI SDK 默认 max_retries=2：健康检查等「单次请求」场景会
                # 被放大成 3 次、整体耗时超出调用方给的 timeout；需要时由调用方
                # 显式传 0（应用层的 retry_with_backoff 才是重试的唯一入口）
                "max_retries": int(
                    2 if max_retries is None else max_retries
                ),
            },
            generate_kwargs={
                "temperature": (
                    LLM_CONFIG.get("temperature", 0.7) if temperature is None else temperature
                ),
                "max_tokens": (
                    LLM_CONFIG.get("max_tokens", 8192) if max_tokens is None else max_tokens
                ),
            },
        )
    )


def get_model_config():
    """
    获取模型配置（用于Agent初始化）

    Returns:
        dict: 模型配置字典
    """
    return {
        "model_type": "openai_chat",  # 使用OpenAI兼容接口
        "config_name": "doubao_api",
        "model_name": LLM_CONFIG["model_name"],
        "api_key": LLM_CONFIG["api_key"],
        "base_url": LLM_CONFIG["base_url"],
        "temperature": LLM_CONFIG.get("temperature", 0.7),
        "max_tokens": LLM_CONFIG.get("max_tokens", 2000),
    }
