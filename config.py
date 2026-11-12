"""
Configuration for the Aligo Multi-Agent System
"""
import logging
import os
from typing import TypedDict

logger = logging.getLogger(__name__)

# LLM Configuration
# 默认使用 DeepSeek（OpenAI 兼容接口），也支持豆包、OpenAI 等任意兼容服务。
#
# 密钥读取优先级：环境变量 LLM_API_KEY > 本地 config_local.py 中的 API_KEY
#   macOS/Linux:        export LLM_API_KEY=你的密钥
#   Windows PowerShell: $env:LLM_API_KEY="你的密钥"
#   （Windows 推荐在项目根目录创建 config_local.py，内容为 API_KEY = "sk-..."，该文件已 gitignore）
#
# 切换服务商：覆盖下面两个环境变量，或直接修改默认值。示例——
#   豆包:   LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
#           LLM_MODEL_NAME=doubao-seed-1-6-flash-250828
#   OpenAI: LLM_BASE_URL=https://api.openai.com/v1
#           LLM_MODEL_NAME=gpt-4o-mini
def _env_str(name: str, default: str) -> str:
    """环境变量存在但为空 / 纯空白时回退默认值（Docker / K8s 常见空注入）。"""
    value = os.environ.get(name, "").strip()
    return value or default


# 密钥必须 strip：复制粘贴带尾随空格 / 换行时，httpx 会拒绝非法 header
# 值（Bearer sk-xxx␠），所有请求变成「Connection error」且异常链带完整
# 密钥进日志；纯空白值按未设置处理（否则会阻止回退 config_local.py）
_api_key = os.environ.get("LLM_API_KEY", "").strip()
if not _api_key:
    try:
        # 本地私有配置（已 gitignore）。注意不要用 import ... as _api_key 的写法：
        # config_local.py 不存在时 mypy 会报 "Name already defined [no-redef]"
        from config_local import API_KEY

        _api_key = str(API_KEY or "").strip()
    except ImportError as exc:
        # 只吞掉「config_local 不存在 / 没有 API_KEY」；文件里自身 import 失败
        # （例如先 import 了未安装的模块再写密钥）必须暴露，否则有效密钥
        # 被静默忽略、各处误报「未配置 API Key」
        if getattr(exc, "name", "") != "config_local":
            raise


class LLMConfig(TypedDict):
    api_key: str
    model_name: str
    base_url: str
    temperature: float
    max_tokens: int


class SystemConfig(TypedDict):
    enable_llm: bool
    log_level: str
    max_retries: int
    timeout: int


class RAGConfig(TypedDict):
    embedding_model: str
    # 问答检索的最低余弦相似度：低于该分的片段视为「与知识库无关」，
    # 不再注入回答提示词（无关问题得分通常 <0.5，相关命中 >0.6）
    min_score: float


class StorageConfig(TypedDict):
    redis_url: str
    postgres_dsn: str
    redis_ttl_sec: int


class ResilienceConfig(TypedDict):
    max_retries: int
    retry_base_delay_sec: float
    retry_max_delay_sec: float
    circuit_failure_threshold: int
    circuit_recovery_timeout_sec: float
    circuit_half_open_successes: int
    health_check_timeout_sec: float


class RateLimitConfig(TypedDict):
    chat_requests: int
    ip_requests: int
    kb_requests: int
    kb_import_requests: int
    weather_requests: int
    read_requests: int
    write_requests: int
    window_sec: int
    # 是否信任反向代理写入的 X-Forwarded-For（默认关闭，避免客户端伪造 IP 绕过 IP 桶）
    trust_proxy_headers: bool


LLM_CONFIG: LLMConfig = {
    "api_key": _api_key,
    "model_name": _env_str("LLM_MODEL_NAME", "deepseek-chat"),
    "base_url": _env_str("LLM_BASE_URL", "https://api.deepseek.com"),
    "temperature": 0.7,
    "max_tokens": 8192,
}

# System Configuration
SYSTEM_CONFIG: SystemConfig = {
    "enable_llm": True,  # Set to True to use LLM (recommended), False for rule-based
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,  # Increased timeout for better stability
}

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG: RAGConfig = {
    "embedding_model": "data/models/bge-small-zh-v1.5",
    "min_score": 0.5,
}

# 存储后端（生产模式可选；未配置或连接失败时自动回退本地实现）：
#   REDIS_URL     例如 redis://localhost:6379/0 —— 启用 Redis 短期记忆
#   DATABASE_URL  例如 postgresql://user:pass@localhost:5432/db —— 启用 PostgreSQL 长期记忆
#   未设置时分别使用进程内滑动窗口 / data/memory/*.json
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


STORAGE_CONFIG: StorageConfig = {
    "redis_url": os.environ.get("REDIS_URL", ""),
    "postgres_dsn": os.environ.get("DATABASE_URL", ""),
    "redis_ttl_sec": _env_int("REDIS_TTL_SEC", 3600),
}

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG: ResilienceConfig = {
    "max_retries": 3,              # 单次请求最大重试次数（与 SYSTEM_CONFIG 对齐）
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}

# 对话接口限流（进程内滑动窗口；用于本地演示 / 简单防滥用）
#   CHAT_RATE_LIMIT      单个用户（客户端 + 用户 ID）窗口内允许的对话请求数（默认 30）
#   CHAT_RATE_LIMIT_IP   单个客户端 IP 窗口内允许的对话请求总数（默认 120，用户 ID 由
#                        请求方提供、可轮换，IP 桶防止通过换 ID 绕过限流）
#   CHAT_RATE_WINDOW_SEC 窗口长度（秒，默认 60）
#   KB_RATE_LIMIT        知识库写操作 / 语义检索的单 IP 窗口额度（默认 60）
#   KB_IMPORT_RATE_LIMIT 知识库导入 / 导入预览的独立单 IP 窗口额度（默认 1200：
#                        面板分批导入需要远高于普通知识库操作的额度）
#   READ_RATE_LIMIT      面板数据读接口（会话列表 / 状态 / 导出等每次全量读取）
#                        的单 IP 窗口额度（默认 600）
#   WRITE_RATE_LIMIT     用户数据写接口（偏好 / 会话元数据 / 行程 / 删除等）的
#                        单 IP 窗口额度（默认 240）：每次写都会全量重载并重写
#                        用户记忆，且与对话落库共用文件锁 / 数据库连接
#   TRUST_PROXY_HEADERS  为 1 时按反向代理的 X-Forwarded-For 第一个地址限流（默认关闭）
RATE_LIMIT_CONFIG: RateLimitConfig = {
    "chat_requests": _env_int("CHAT_RATE_LIMIT", 30),
    "ip_requests": _env_int("CHAT_RATE_LIMIT_IP", 120),
    "kb_requests": _env_int("KB_RATE_LIMIT", 60),
    "kb_import_requests": _env_int("KB_IMPORT_RATE_LIMIT", 1200),
    "weather_requests": _env_int("WEATHER_RATE_LIMIT", 30),
    "read_requests": _env_int("READ_RATE_LIMIT", 600),
    "write_requests": _env_int("WRITE_RATE_LIMIT", 240),
    "window_sec": _env_int("CHAT_RATE_WINDOW_SEC", 60),
    "trust_proxy_headers": _env_bool("TRUST_PROXY_HEADERS", False),
}

# 窗口 <= 0 会让所有滑动窗口桶退化成「永远是新窗口」（每次请求都被放行，
# 形同不限流）：按下限 1 秒处理并告警，而不是静默 fail-open
if int(RATE_LIMIT_CONFIG["window_sec"]) < 1:
    logger.warning(
        "CHAT_RATE_WINDOW_SEC=%s 非法（需 >= 1 秒），按 1 秒处理",
        RATE_LIMIT_CONFIG["window_sec"],
    )
    RATE_LIMIT_CONFIG["window_sec"] = 1
