"""
访问日志格式化

默认输出与原格式一致的单行文本；设置环境变量 ``LOG_FORMAT=json`` 后输出
JSON lines（`event=http_request`），便于日志采集、检索与告警。
"""
import json
import os


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
                "method": str(method),
                "path": str(path),
                "status": int(status),
                "duration_ms": round(float(elapsed_ms), 1),
                "request_id": str(request_id),
            },
            ensure_ascii=False,
        )
    return (
        f"{method} {path} -> {status} "
        f"({float(elapsed_ms):.0f}ms) [{request_id}]"
    )
