"""共享异常类型。

放在独立模块中，避免 ``utils`` 与 ``context`` / ``agents`` 之间循环导入。
"""


class MemoryPersistenceError(RuntimeError):
    """长期记忆写盘失败（磁盘只读 / 空间不足 / 文件被占用）。

    刻意不继承 ``OSError``：``utils.llm_resilience.is_retriable_error`` 会把
    ``OSError`` 当作可重试的网络错误，而写盘失败重试整轮对话既不会恢复磁盘，
    还会重复调用大模型、记录熔断失败，最终把健康的 LLM 服务误判为不可用。
    """


class PartialImportError(MemoryPersistenceError):
    """批量写入中途失败：``created`` / ``updated`` 为失败前已提交的条数。

    PostgreSQL 后端逐行 autocommit，中途失败时已写入的行保留：调用方必须
    如实报告「部分已提交」，否则用户以为整份文件未导入，换文件或丢弃备份
    重试后数据就永久停留在半份状态。
    """

    def __init__(self, message: str, created: int, updated: int) -> None:
        super().__init__(message)
        self.created = created
        self.updated = updated


class ImportVolumeError(ValueError):
    """导入写入前的锁内体积复核拒绝。

    预检在锁外、写入在锁内：两次并发导入各自通过复核后先后写入，合计
    越限会让账号此后永久无法导出全量备份。写入前在导入互斥锁内重取快照
    并重跑合并体积复核，超限时抛本异常（ValueError 子类：这是用户可
    修正的数据问题，Web 映射 413、CLI 直接展示原因，而不是可重试 503）。
    """


class AccountCapacityError(ValueError):
    """账号 / 全局数据容量已达上限的永久、用户可修正状态。

    与请求内容非法（400）不同：调用方应先删除数据再重试，Web 层统一
    映射 409（与实时写入路径同口径），CLI 直接展示原因。继承
    ``ValueError`` 以兼容既有的 ``except ValueError`` 消费方。
    """


class UserFacingError(RuntimeError):
    """消息本身就是给用户看的提示（固定文案，不含内部异常细节）。

    Web 层对未预期异常统一脱敏返回，只有这类异常的 ``str(exc)`` 可以原样
    透出（例如「意图识别结果解析失败，请重试」）。``status_code`` 描述
    这类失败在 HTTP 上的语义：默认 502（上游 / 模型返回了不可用响应，
    客户端按原样重试即可），瞬时预算耗尽可传 503 + Retry-After——不能用
    4xx（客户端改请求也不会消除，且与「请重试」文案矛盾）。
    """

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code
