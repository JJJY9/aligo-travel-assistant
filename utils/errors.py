"""共享异常类型。

放在独立模块中，避免 ``utils`` 与 ``context`` / ``agents`` 之间循环导入。
"""


class MemoryPersistenceError(RuntimeError):
    """长期记忆写盘失败（磁盘只读 / 空间不足 / 文件被占用）。

    刻意不继承 ``OSError``：``utils.llm_resilience.is_retriable_error`` 会把
    ``OSError`` 当作可重试的网络错误，而写盘失败重试整轮对话既不会恢复磁盘，
    还会重复调用大模型、记录熔断失败，最终把健康的 LLM 服务误判为不可用。
    """
