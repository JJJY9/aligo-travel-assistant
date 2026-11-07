"""
访问日志格式化

默认输出与原格式一致的单行文本；设置环境变量 ``LOG_FORMAT=json`` 后输出
JSON lines（`event=http_request`），便于日志采集、检索与告警。
"""
import json
import logging
import os
import re


def _safe_log_text(value: object) -> str:
    """去掉控制字符：uvicorn 会 percent-decode 路径，%0A 可伪造整行日志。

    C1 控制符（\x7f-\x9f，含 NEL）与 U+2028/U+2029 行分隔符在部分
    日志采集器 / 终端里同样会断行或破坏 JSON 结构，一并替换。
    """
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(value))
    return text.replace("\u2028", "?").replace("\u2029", "?")


def access_log_format() -> str:
    """当前访问日志格式：``text``（默认）或 ``json``。"""
    return os.environ.get("LOG_FORMAT", "text").strip().lower()


def format_access_log(
    method: str,
    path: str,
    status: int,
    elapsed_ms: float,
    request_id: str,
    fmt: str | None = None,
) -> str:
    """格式化一条访问日志（调用方以单个字符串参数传给 logger）。"""
    mode = (fmt or access_log_format()).strip().lower()
    if mode == "json":
        return json.dumps(
            {
                "event": "http_request",
                "method": _safe_log_text(method),
                "path": _safe_log_text(path),
                "status": int(status),
                "duration_ms": round(float(elapsed_ms), 1),
                "request_id": _safe_log_text(request_id),
            },
            ensure_ascii=False,
        )
    return (
        f"{_safe_log_text(method)} {_safe_log_text(path)} -> {status} "
        f"({float(elapsed_ms):.0f}ms) [{_safe_log_text(request_id)}]"
    )


class JsonLogFormatter(logging.Formatter):
    """把日志记录输出为单行 JSON；访问日志的 JSON 字段提升到顶层。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
        }
        message = record.getMessage()
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            payload.update(data)
        else:
            payload["message"] = message
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)
