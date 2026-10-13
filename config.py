"""
Configuration for the Aligo Multi-Agent System
"""
import os

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
_api_key = os.environ.get("LLM_API_KEY", "")
if not _api_key:
    try:
        from config_local import API_KEY as _api_key  # 本地私有配置（已 gitignore）
    except ImportError:
        pass

LLM_CONFIG = {
    "api_key": _api_key,
    "model_name": os.environ.get("LLM_MODEL_NAME", "deepseek-chat"),
    "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
    "temperature": 0.7,
    "max_tokens": 8192,
}

# System Configuration
SYSTEM_CONFIG = {
    "enable_llm": True,  # Set to True to use LLM (recommended), False for rule-based
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,  # Increased timeout for better stability
}

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG = {
    "embedding_model": "data/models/bge-small-zh-v1.5",
}

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG = {
    "max_retries": 3,              # 单次请求最大重试次数（与 SYSTEM_CONFIG 对齐）
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}
