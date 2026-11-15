"""直跑清单用的 caplog 替身。

``caplog`` 是 pytest 夹具，只在 pytest 内注入；``python tests/xxx.py``
直接运行时若静默跳过所有日志断言用例，日志脱敏 / 告警回归会在直跑清单
上表现为假绿（「全部通过」实际零断言）。这里提供最小的 text / records /
at_level 实现，让这些用例在直跑时真正执行断言。
"""
import logging


class _AtLevel:
    def __init__(self, shim: "CaplogShim", level: int):
        self._shim = shim
        self._level = level
        self._old = logging.NOTSET

    def __enter__(self):
        root = logging.getLogger()
        self._old = root.level
        root.setLevel(self._level)
        self._shim.records.clear()
        return self._shim

    def __exit__(self, *exc_info):
        logging.getLogger().setLevel(self._old)
        return False


class CaplogShim:
    """最小 pytest caplog 替身：``text`` / ``records`` / ``at_level``。"""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        records = self.records

        class _Handler(logging.Handler):
            def emit(self, record):  # noqa: D102 - 与 logging.Handler 一致
                records.append(record)

        self._handler = _Handler()
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)

    def at_level(self, level):
        return _AtLevel(self, level)

    def clear(self):
        self.records.clear()

    def close(self):
        logging.getLogger().removeHandler(self._handler)
