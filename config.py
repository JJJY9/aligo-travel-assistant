"""
Configuration for the Aligo Multi-Agent System
"""
import logging
import importlib.util
import math
import os
from pathlib import Path
from typing import TypedDict, cast

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
def _strip_injected_quotes(value: str) -> str:
    """剥离文件注入的成对引号（K8s ConfigMap / .env 模板）。

    ``"value"`` / ``'value'`` 连引号一起复制进环境变量时，字符串配置会
    带着引号生效（URL / 密钥鉴权失败），数值 / 布尔则解析失败并静默
    回退默认值——三类配置必须同一清洗口径。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _env_str(name: str, default: str) -> str:
    """环境变量存在但为空 / 纯空白时回退默认值（Docker / K8s 常见空注入）。

    同时剥离 UTF-8 BOM（U+FEFF 不是空白）：带 BOM 的 .env 会让 URL / 密钥
    「看起来已配置」却永远连不上（BOM 混进库名 / HTTP 头编码失败）。
    """
    value = os.environ.get(name, "").replace("\ufeff", "").strip()
    # 文件注入的 "value" / 'value'（K8s ConfigMap、.env 模板）连引号
    # 一起复制进来：原样生效会让 URL / 密钥带引号、连接在鉴权阶段失败
    return _strip_injected_quotes(value) or default


def _env_int(name: str, default: int) -> int:
    # 与 _env_str 同一 BOM / 空白清洗口径：带 U+FEFF 的数值此前解析失败、
    # 静默回退默认值（文件注入的 K8s 值不生效）
    raw = _strip_injected_quotes(
        os.environ.get(name, str(default)).replace("\ufeff", "").strip()
    )
    try:
        return int(raw)
    except ValueError:
        # 静默回退会让一次笔误（如 CHAT_RATE_LIMIT=2,000）变成看不见的默认值
        logger.warning(
            "环境变量 %s=%r 不是整数，使用默认值 %s",
            name,
            str(raw)[:40],
            default,
        )
        return default


def _env_float(name: str, default: float) -> float:
    """浮点环境变量：与 _env_int 同一 BOM / 引号清洗口径。

    非法值 / 非有限值（nan / inf）回退默认并告警：静默回退会让一次笔误
    （``RAG_MIN_SCORE=0,5``）变成看不见的默认阈值。
    """
    raw = _strip_injected_quotes(
        os.environ.get(name, str(default)).replace("\ufeff", "").strip()
    )
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "环境变量 %s=%r 不是数字，使用默认值 %s",
            name,
            str(raw)[:40],
            default,
        )
        return default
    if not math.isfinite(value):
        logger.warning(
            "环境变量 %s=%r 不是有限数值，使用默认值 %s",
            name,
            str(raw)[:40],
            default,
        )
        return default
    return value


_BOOL_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    raw = _strip_injected_quotes(
        os.environ.get(name, "").replace("\ufeff", "").strip()
    ).lower()
    if not raw:
        return default
    if raw in _BOOL_TRUE_VALUES:
        return True
    if raw in _BOOL_FALSE_VALUES:
        return False
    # 无法识别的写法（enabled / ture 笔误）此前静默按 False：开关
    # 永远不生效且无任何提示
    logger.warning(
        "环境变量 %s=%r 不是布尔值（true/false/1/0/yes/no/on/off），使用默认值 %s",
        name,
        raw[:40],
        default,
    )
    return default


# 密钥必须 strip：复制粘贴带尾随空格 / 换行时，httpx 会拒绝非法 header
# 值（Bearer sk-xxx␠），所有请求变成「Connection error」且异常链带完整
# 密钥进日志；纯空白值按未设置处理（否则会阻止回退 config_local.py）
_api_key = _strip_injected_quotes(
    os.environ.get("LLM_API_KEY", "").replace("\ufeff", "").strip()
)
if not _api_key:
    try:
        # 本地私有配置（已 gitignore）。注意不要用 import ... as _api_key 的写法：
        # config_local.py 不存在时 mypy 会报 "Name already defined [no-redef]"
        from config_local import API_KEY

        _api_key = _strip_injected_quotes(
            str(API_KEY or "").replace("\ufeff", "").strip()
        )
    except ImportError as exc:
        # 只吞掉「config_local 不存在 / 没有 API_KEY」；文件里自身 import 失败
        # （例如先 import 了未安装的模块再写密钥）必须暴露，否则有效密钥
        # 被静默忽略、各处误报「未配置 API Key」
        if getattr(exc, "name", "") != "config_local":
            raise
        # wheel / console script 启动时 sys.path 不含当前目录，常规 import
        # 找不到运行目录下的 config_local.py（README 指导的放置方式）：
        # 按显式路径加载一次；文件自身报错照常上抛
        local_file = Path.cwd() / "config_local.py"
        if local_file.is_file():
            spec = importlib.util.spec_from_file_location(
                "config_local", local_file
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _api_key = _strip_injected_quotes(
                    str(getattr(module, "API_KEY", "") or "")
                    .replace("\ufeff", "")
                    .strip()
                )


class LLMConfig(TypedDict):
    api_key: str
    model_name: str
    base_url: str
    temperature: float
    max_tokens: int


class SystemConfig(TypedDict):
    timeout: int


class RAGConfig(TypedDict):
    embedding_model: str
    # 问答检索的证据门槛：聊天链路按混合 distance = max(向量余弦,
    # 归一化 BM25) 过滤，低于该分的片段不再注入回答提示词。实测
    # BGE-small-zh 的余弦基线就在 0.5 附近：0.5 能滤掉明显无关的问题，
    # 0.5–0.7 之间的擦边问题（如「护照怎么办理」命中应急文档里提到
    # 护照的片段，0.68）仍会注入，由回答提示词要求模型只取相关部分。
    # 调高该值必须同步跑 python cli.py eval 复核合法问题的命中率
    min_score: float
    # v2.0 混合检索：向量 + BM25，RRF 融合；重排模型为空表示不启用。
    # BM25 索引从 Milvus 拉取片段构建并缓存，集合过大时跳过。
    hybrid_enabled: bool
    hybrid_candidates: int
    rrf_k: int
    rerank_model: str
    rerank_candidates: int
    bm25_max_docs: int
    bm25_ttl_sec: int


class PlanConfig(TypedDict):
    # v2.0 行程校验：五项检查未通过时带原因重生成（默认 1 次）。
    repair_enabled: bool
    max_repair_attempts: int


class StorageConfig(TypedDict):
    redis_url: str
    postgres_dsn: str
    redis_ttl_sec: int


class ResilienceConfig(TypedDict):
    max_retries: int
    retry_base_delay_sec: float
    retry_max_delay_sec: float
    turn_retry_budget_sec: float
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
    # 进程内限流器每个桶的键表上限（满时对新 key fail-closed，Redis 后端不设上限）
    max_keys: int
    # 是否信任反向代理写入的 X-Forwarded-For（默认关闭，避免客户端伪造 IP 绕过 IP 桶）
    trust_proxy_headers: bool
    # TRUST_PROXY_HEADERS=1 时允许写入 XFF 的对端网段（逗号分隔 CIDR；默认只信回环）
    trusted_proxy_cidrs: str
    # 可信代理层数：XFF 取倒数第 N 跳（默认 1；CDN→nginx→应用这类多层部署
    # 设为实际代理层数，否则所有客户端共用 CDN 出口地址的限流桶）
    trust_proxy_hops: int
    # IPv6 身份聚合前缀长度（默认 /64；按接入网络可收紧到 /56、/48）
    ipv6_prefix_len: int


LLM_CONFIG: LLMConfig = {
    "api_key": _api_key,
    "model_name": _env_str("LLM_MODEL_NAME", "deepseek-chat"),
    "base_url": _env_str("LLM_BASE_URL", "https://api.deepseek.com"),
    "temperature": 0.7,
    "max_tokens": 8192,
}

# System Configuration
# 这里只保留真正被运行时消费的键：重试次数统一由 RESILIENCE_CONFIG
# ["max_retries"] 管（config_agentscope / 应用层重试共用该口径）；曾有的
# enable_llm / log_level / max_retries 没有任何消费者，改它们对行为零效果，
# 只会让排障误以为配置已生效
SYSTEM_CONFIG: SystemConfig = {
    # 非流式请求的 read 超时兜住整段生成时间：max_tokens=8192 在长行程 /
    # 大历史下可能超过 60s，60s 会让正常请求 ReadTimeout 并被应用层整轮
    # 重试（重复计费）。默认 120s，可用 LLM_REQUEST_TIMEOUT_SEC 调整。
    # 注意：本值只决定单次 HTTP 请求的 read 超时；编排层对每个子智能体
    # 另有独立的 300 秒总执行上限（含懒加载与一次重试），设到 300 以上
    # 不会延长子智能体执行，超时任务仍会在上限处被取消
    "timeout": _env_int("LLM_REQUEST_TIMEOUT_SEC", 120),
}

# 超时下限 / 上限钳制（与限流窗口、额度的处理同口径）：0 / 负数会被
# httpx 解释为「立即截止」，每次模型调用瞬间 APITimeoutError、重试
# 全败并最终打开熔断（全站 503）；数百位的超大值在 float() 转换时
# OverflowError
if SYSTEM_CONFIG["timeout"] < 1:
    logger.warning(
        "LLM_REQUEST_TIMEOUT_SEC=%s 非法（需 >= 1 秒；0/负数会导致每次请求"
        "立即超时），按 120 秒处理",
        SYSTEM_CONFIG["timeout"],
    )
    SYSTEM_CONFIG["timeout"] = 120
elif SYSTEM_CONFIG["timeout"] > 3600:
    logger.warning(
        "LLM_REQUEST_TIMEOUT_SEC=%s 超出合理上限，按 3600 秒处理",
        SYSTEM_CONFIG["timeout"],
    )
    SYSTEM_CONFIG["timeout"] = 3600

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG: RAGConfig = {
    # 嵌入模型可用 RAG_EMBEDDING_MODEL 覆盖（如指向本地 BAAI/bge-m3）；
    # 维度由 utils.embedding_model 按模型配置探测，不写死
    "embedding_model": _env_str(
        "RAG_EMBEDDING_MODEL", "data/models/bge-small-zh-v1.5"
    ),
    # 问答检索阈值可用 RAG_MIN_SCORE 覆盖（钳制到 0–1）
    "min_score": min(1.0, max(0.0, _env_float("RAG_MIN_SCORE", 0.5))),
    # 混合检索（v2.0）：默认开启；BM25 索引构建失败时自动退回纯向量。
    # 重排模型（如 BAAI/bge-reranker-base）留空表示不加载，避免默认
    # 拉取额外模型；启用后对 RRF 候选做 cross-encoder 精排。
    "hybrid_enabled": _env_bool("RAG_HYBRID_ENABLED", True),
    "hybrid_candidates": max(
        1, min(200, _env_int("RAG_HYBRID_CANDIDATES", 20))
    ),
    "rrf_k": max(1, _env_int("RAG_RRF_K", 60)),
    "rerank_model": _env_str("RAG_RERANK_MODEL", ""),
    "rerank_candidates": max(
        1, min(200, _env_int("RAG_RERANK_CANDIDATES", 20))
    ),
    "bm25_max_docs": max(1, _env_int("RAG_BM25_MAX_DOCS", 20000)),
    # 0 / 负数留到下面统一钳制：消费方按 (now - cached) <= ttl 判定，
    # 0 会让缓存永不命中、每次问答都在请求线程重建索引（数万片段）
    "bm25_ttl_sec": _env_int("RAG_BM25_TTL_SEC", 60),
}

# BM25 缓存 TTL 的 0 / 负值语义与 REDIS_TTL_SEC 不同（消费方不是
# 「0=永不过期」，而是永不命中）：每次问答都从 Milvus 拉全量片段同步
# 重建索引（2 万片段约 0.4–0.5 GB），且全程无提示。按下限 1 秒的
# 合理值 60 秒处理并告警
if RAG_CONFIG["bm25_ttl_sec"] < 1:
    logger.warning(
        "RAG_BM25_TTL_SEC=%s 非法（需 >= 1 秒；0/负数会让 BM25 索引缓存"
        "永不命中、每次问答都在请求线程重建索引），按 60 秒处理",
        RAG_CONFIG["bm25_ttl_sec"],
    )
    RAG_CONFIG["bm25_ttl_sec"] = 60

# 行程规划校验（v2.0）：五项检查失败后带原因重生成。
# attempts 上限 3，默认 1（一次重生成；仍失败则保留问题列表返回）。
PLAN_CONFIG: PlanConfig = {
    "repair_enabled": _env_bool("PLAN_REPAIR_ENABLED", True),
    "max_repair_attempts": max(0, min(3, _env_int("PLAN_REPAIR_ATTEMPTS", 1))),
}

# 存储后端（生产模式可选；未配置或连接失败时自动回退本地实现）：
#   REDIS_URL     例如 redis://localhost:6379/0 —— 启用 Redis 短期记忆
#   DATABASE_URL  例如 postgresql://user:pass@localhost:5432/db —— 启用 PostgreSQL 长期记忆
#   未设置时分别使用进程内滑动窗口 / data/memory/*.json
STORAGE_CONFIG: StorageConfig = {
    # 必须 strip / 去 BOM：K8s --from-file 的尾随换行会让 DATABASE_URL 的
    # 换行并进库名（连接永久失败、长期记忆静默落回本地 JSON），前导空白
    # 让 REDIS_URL 在 from_url 直接 ValueError（全部限流桶降级）
    "redis_url": _env_str("REDIS_URL", ""),
    "postgres_dsn": _env_str("DATABASE_URL", ""),
    "redis_ttl_sec": _env_int("REDIS_TTL_SEC", 3600),
}

# EXPIRE 的秒值超过 Redis 的 int64 上限会直接报错（invalid expire time）：
# 每次写入的消息 / 去重集合都拿不到过期时间，短期记忆静默降级且键永久
# 驻留。按 10 年上限钳制并告警（下限 <1 由 context/storage.py 钳制）
_MAX_REDIS_TTL_SEC = 10 * 365 * 86400
_redis_ttl_sec = int(STORAGE_CONFIG["redis_ttl_sec"])
if _redis_ttl_sec > _MAX_REDIS_TTL_SEC:
    logger.warning(
        "REDIS_TTL_SEC=%s 过大（上限 %s 秒，约 10 年），按上限处理",
        _redis_ttl_sec,
        _MAX_REDIS_TTL_SEC,
    )
    STORAGE_CONFIG["redis_ttl_sec"] = _MAX_REDIS_TTL_SEC

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG: ResilienceConfig = {
    "max_retries": 3,              # 单次请求最大重试次数（应用层重试的唯一口径）
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    # 整个回合（意图 + 编排两段外层重试）的重试总预算（秒，0 表示不限）：
    # 外层 3 次重试 × 内层逐智能体 2 次尝试，同一失败智能体在一次回合里
    # 最多被完整执行 8 次，单次 120 秒读超时下可把用户锁与半开名额占住
    # 十几分钟。预算只限制「再发起新的尝试」，不打断在途调用
    "turn_retry_budget_sec": 300.0,
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}

# 对话接口限流（进程内滑动窗口；用于本地演示 / 简单防滥用）
#   CHAT_RATE_LIMIT      单个用户（客户端 + 用户 ID）窗口内允许的对话请求数（默认 30）
#   CHAT_RATE_LIMIT_IP   单个客户端 IP 窗口内允许的对话请求总数（默认 120，用户 ID 由
#                        请求方提供、可轮换，IP 桶防止通过换 ID 绕过限流）
#   CHAT_RATE_WINDOW_SEC 窗口长度（秒，默认 60）。注意：它同时作用于全部 7 个
#                        桶（对话 / 知识库 / 导入 / 天气 / 读 / 写），各桶额度都是
#                        「每窗口 N 次」——调小窗口会按比例放大其它桶的每分钟额度
#   KB_RATE_LIMIT        知识库写操作 / 语义检索的单 IP 窗口额度（默认 60）
#   KB_IMPORT_RATE_LIMIT 知识库导入 / 导入预览与 /api/trips/import 的独立单 IP
#                        窗口额度（默认 1200：面板分批导入需要远高于普通知识库
#                        操作的额度；调整它会同时改变行程导入额度）
#   READ_RATE_LIMIT      面板数据读接口（会话列表 / 状态 / 导出等每次全量读取）
#                        的单 IP 窗口额度（默认 600）
#   WRITE_RATE_LIMIT     用户数据写接口（偏好 / 会话元数据 / 行程 / 删除等）的
#                        单 IP 窗口额度（默认 240）：每次写都会全量重载并重写
#                        用户记忆，且与对话落库共用文件锁 / 数据库连接
#   RATE_LIMIT_MAX_KEYS  进程内限流器每个桶的键表上限（默认 10000，钳制在
#                        100..1000000）。表满时对新 key fail-closed，Retry-After
#                        按最早到期键估算；小时 / 日窗口或单进程键基数可能超过
#                        上限时应配置 REDIS_URL（Redis 后端不设键表上限）
#   TRUST_PROXY_HEADERS  为 1 时按反向代理的 X-Forwarded-For 地址限流（默认关闭）
#   TRUSTED_PROXY_CIDRS  TRUST_PROXY_HEADERS=1 时允许写 XFF 的对端网段（默认只信
#                        回环 127.0.0.1/32,::1/128；容器 / 跨主机代理必须显式配置）
#   TRUST_PROXY_HOPS     XFF 取倒数第几跳（默认 1 = 直接对端代理追加的真实客户端；
#                        CDN→nginx→应用这类多层部署设为实际代理层数）
#   RATE_IPV6_PREFIX_LEN IPv6 客户端身份的聚合前缀（默认 64；按接入网络委派粒度
#                        可改为 56 或 48，多实例部署必须同值）
RATE_LIMIT_CONFIG: RateLimitConfig = {
    "chat_requests": _env_int("CHAT_RATE_LIMIT", 30),
    "ip_requests": _env_int("CHAT_RATE_LIMIT_IP", 120),
    "kb_requests": _env_int("KB_RATE_LIMIT", 60),
    "kb_import_requests": _env_int("KB_IMPORT_RATE_LIMIT", 1200),
    "weather_requests": _env_int("WEATHER_RATE_LIMIT", 30),
    "read_requests": _env_int("READ_RATE_LIMIT", 600),
    "write_requests": _env_int("WRITE_RATE_LIMIT", 240),
    "window_sec": _env_int("CHAT_RATE_WINDOW_SEC", 60),
    "max_keys": _env_int("RATE_LIMIT_MAX_KEYS", 10000),
    "trust_proxy_headers": _env_bool("TRUST_PROXY_HEADERS", False),
    "trusted_proxy_cidrs": _env_str(
        "TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128"
    ),
    "trust_proxy_hops": _env_int("TRUST_PROXY_HOPS", 1),
    "ipv6_prefix_len": _env_int("RATE_IPV6_PREFIX_LEN", 64),
}

# 窗口 <= 0 会让所有滑动窗口桶退化成「永远是新窗口」（每次请求都被放行，
# 形同不限流）：按下限 1 秒处理并告警，而不是静默 fail-open
if int(RATE_LIMIT_CONFIG["window_sec"]) < 1:
    logger.warning(
        "CHAT_RATE_WINDOW_SEC=%s 非法（需 >= 1 秒），按 1 秒处理",
        RATE_LIMIT_CONFIG["window_sec"],
    )
    RATE_LIMIT_CONFIG["window_sec"] = 1
# 大窗口是有意支持的（小时 / 日配额），但进程内桶的键表在窗口内不会老化：
# 窗口远大于正常业务节奏时，键表打满会持续整个窗口。只告警不硬钳，避免
# 破坏有意的日配额配置
if int(RATE_LIMIT_CONFIG["window_sec"]) > 86400:
    logger.warning(
        "CHAT_RATE_WINDOW_SEC=%s 超过一天：进程内限流器的键表在窗口内不会"
        "老化，建议配置 REDIS_URL 或确认 RATE_LIMIT_MAX_KEYS 足够",
        RATE_LIMIT_CONFIG["window_sec"],
    )

# 窗口同时作用于全部桶：非默认值时列出每个桶的等效每分钟额度，避免
# 「只调对话窗口」却按比例放大知识库 / 读 / 写额度的静默耦合。
# 窗口秒数最终会被 float() 进限流器（utils/rate_limit.py），Redis PEXPIRE
# 也按毫秒计：数百位的超大整数（>1.8e308）在 create_app 里直接
# OverflowError、Web 启动失败。上限取 1 年（仍覆盖小时 / 日配额），
# 越界钳制并告警（与 LLM_REQUEST_TIMEOUT_SEC 同口径）
_MAX_WINDOW_SEC = 366 * 86400
_window_sec = int(RATE_LIMIT_CONFIG["window_sec"])
if _window_sec > _MAX_WINDOW_SEC:
    logger.warning(
        "CHAT_RATE_WINDOW_SEC=%s 超出合理上限，按 %s 秒（约 1 年）处理",
        _window_sec,
        _MAX_WINDOW_SEC,
    )
    RATE_LIMIT_CONFIG["window_sec"] = _window_sec = _MAX_WINDOW_SEC
if _window_sec != 60:
    logger.warning(
        "CHAT_RATE_WINDOW_SEC=%s 同时作用于全部限流桶（对话 / IP / 知识库 / "
        "导入 / 天气 / 读 / 写）：各桶额度按每窗口计，等效每分钟额度 = N×60/%s；"
        "如只想调整对话窗口请改用独立部署或显式核对各桶额度",
        _window_sec,
        _window_sec,
    )

# 额度 <= 0 会被 RateLimiter 构造时静默钳成 max(1, limit)：常见的
# 「0 = 关闭限流」写法反而把该桶变成每窗口只有 1 次（面板批量操作全部
# 429）。显式告警并按下限 1 处理，不再无声变更
_rate_limits = cast("dict[str, int]", RATE_LIMIT_CONFIG)
for _limit_key, _limit_env in (
    ("chat_requests", "CHAT_RATE_LIMIT"),
    ("ip_requests", "CHAT_RATE_LIMIT_IP"),
    ("kb_requests", "KB_RATE_LIMIT"),
    ("kb_import_requests", "KB_IMPORT_RATE_LIMIT"),
    ("weather_requests", "WEATHER_RATE_LIMIT"),
    ("read_requests", "READ_RATE_LIMIT"),
    ("write_requests", "WRITE_RATE_LIMIT"),
):
    _limit_value = int(_rate_limits[_limit_key])
    if _limit_value < 1:
        logger.warning(
            "%s=%s 非法（需 >= 1），按每窗口 1 次处理；如需放宽请设置更大值",
            _limit_env,
            _limit_value,
        )
        _rate_limits[_limit_key] = 1
    elif _limit_value > 1_000_000:
        # 超大额度时进程内单键的 deque 可增长到 limit 个时间戳：只告警，
        # 不硬钳（有意的大额度配置需要保留语义）
        logger.warning(
            "%s=%s 过大：进程内限流器为每个键最多保存 limit 个时间戳，"
            "内存上界为 max_keys×limit；请确认确有必要",
            _limit_env,
            _limit_value,
        )

# 键表上限：低于 100 会让正常部署频繁触发容量拒绝；高于 100 万时单桶内存
# 上界过大。越界时钳制并告警（防止 0 / 负数把限流器变成永远满表的拒绝器）
_max_keys_value = int(RATE_LIMIT_CONFIG["max_keys"])
if _max_keys_value < 100 or _max_keys_value > 1_000_000:
    logger.warning(
        "RATE_LIMIT_MAX_KEYS=%s 越界（100..1000000），按最近边界处理",
        _max_keys_value,
    )
    RATE_LIMIT_CONFIG["max_keys"] = min(1_000_000, max(100, _max_keys_value))

# IPv6 聚合前缀：32..64 之外（<32 会把整片 ISP 范围并桶、>64 会把单个 /64
# 再切细）没有聚合语义，按默认 /64 处理
_prefix_value = int(RATE_LIMIT_CONFIG["ipv6_prefix_len"])
if not 32 <= _prefix_value <= 64:
    logger.warning(
        "RATE_IPV6_PREFIX_LEN=%s 非法（需 32..64），按 64 处理",
        _prefix_value,
    )
    RATE_LIMIT_CONFIG["ipv6_prefix_len"] = 64

# 可信代理层数：1..8 之外没有实际语义（更深的链几乎必然是配置错误），
# 按 1 处理（等价旧行为：只取 XFF 末跳）
_hops_value = int(RATE_LIMIT_CONFIG["trust_proxy_hops"])
if not 1 <= _hops_value <= 8:
    logger.warning(
        "TRUST_PROXY_HOPS=%s 非法（需 1..8），按 1 处理", _hops_value
    )
    RATE_LIMIT_CONFIG["trust_proxy_hops"] = 1
