"""直跑清单用的 caplog 替身。

``caplog`` 是 pytest 夹具，只在 pytest 内注入；``python tests/xxx.py``
直接运行时若静默跳过所有日志断言用例，日志脱敏 / 告警回归会在直跑清单
上表现为假绿（「全部通过」实际零断言）。这里提供最小的 text / records /
at_level 实现，让这些用例在直跑时真正执行断言。
"""
import logging


class _AtLevel:
    def __init__(self, shim: "CaplogShim", level: int, logger: str | None):
        self._shim = shim
        self._level = level
        self._logger = logger
        self._old = logging.NOTSET

    def _target(self):
        return logging.getLogger(self._logger)

    def __enter__(self):
        target = self._target()
        self._old = target.level
        self._handler_old = self._shim._handler.level
        target.setLevel(self._level)
        # pytest 的 at_level 同时把捕获 handler 的级别一并调到目标级别：
        # 否则目标 logger 的高级别会拦住其它 logger 的低级别日志
        self._shim._handler.setLevel(self._level)
        # 与 pytest 夹具一致：进入 at_level 不清空已捕获记录（块外日志
        # 同样参与断言；清空只由显式 clear() 触发）
        return self._shim

    def __exit__(self, *exc_info):
        self._target().setLevel(self._old)
        self._shim._handler.setLevel(self._handler_old)
        return False


class CaplogShim:
    """最小 pytest caplog 替身：``text`` / ``records`` / ``at_level``。"""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        records = self.records

        class _Handler(logging.Handler):
            def emit(self, record):  # noqa: D102 - 与 logging.Handler 一致
                # 与 pytest 的 LogCaptureHandler 一致：emit 内格式化，
                # Formatter.format 会设置 record.message（caplog.records
                # 断言 message 的用例在直跑时不再依赖其它 handler 先格式化）
                self.format(record)
                records.append(record)

        self._handler = _Handler()
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)

    def at_level(self, level, logger: str | None = None):
        return _AtLevel(self, level, logger)

    def set_level(self, level, logger: str | None = None) -> None:
        """与 pytest caplog.set_level 的调用形态一致：调整目标 logger 与
        捕获 handler 级别。直跑进程不按用例恢复级别（持续到进程结束 /
        下一次 set_level）——pytest 的逐用例恢复由夹具机制提供，替身
        不做；需要限制作用域时用 ``at_level`` 上下文。"""
        logging.getLogger(logger).setLevel(level)
        self._handler.setLevel(level)

    def clear(self):
        self.records.clear()

    def close(self):
        logging.getLogger().removeHandler(self._handler)
