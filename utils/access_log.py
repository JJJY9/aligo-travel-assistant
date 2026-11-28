"""
访问日志格式化

默认输出与原格式一致的单行文本；设置环境变量 ``LOG_FORMAT=json`` 后输出
JSON lines（`event=http_request`），便于日志采集、检索与告警。
"""
import json
import logging
import os
import re

# 单条访问日志里用户可控字段的上限：uvicorn 允许约 64KB 的请求行（长查询
# 串），未匹配路径的 404 不受限流——不截断时一个请求就能写入接近 64KB 的
# 日志行（放大上千倍）。指标里的路径同样截到 200 字符、方法截到 16。
MAX_LOG_TEXT = 200
MAX_METHOD_TEXT = 16

# 无法识别的取值只告警一次（access_log_format 每请求调用）
_warned_formats: set[str] = set()


def _safe_log_text(value: object) -> str:
    """去掉控制字符：uvicorn 会 percent-decode 路径，%0A 可伪造整行日志。

    C1 控制符（\x7f-\x9f，含 NEL）与 U+2028/U+2029 行分隔符在部分
    日志采集器 / 终端里同样会断行或破坏 JSON 结构，一并替换。
    """
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(value))
    return text.replace("\u2028", "?").replace("\u2029", "?")


# 文本格式以空白 / "->" / "(ms)" / "[id]" 分词：path 是唯一可被请求方
# 控制且未转义的字段（method 来自请求行、request_id 由中间件生成），
# 其中出现同名分隔符时用 percent-encoding 转义，避免路径伪造出额外的
# 状态码 / 请求 ID。JSON 模式字段相互独立，不受影响。\s 覆盖 Unicode
# 空白（U+00A0 / U+3000 等，uvicorn percent-decode 后可出现在路径里）：
# 按 str.split() 分词的分析器同样把它们当分隔符，漏转义会让字段错位。
_TEXT_DELIMITERS = re.compile(r"[ \[\]()>\s]")


def _escape_text_delimiters(value: str) -> str:
    # 非 Latin-1 字符（U+3000 等）没有合法的 %XX 编码，与 _safe_log_text
    # 的控制符处理一致替换为 '?'
    return _TEXT_DELIMITERS.sub(
        lambda m: f"%{ord(m.group()):02X}" if ord(m.group()) < 0x100 else "?",
        value,
    )


def access_log_format() -> str:
    """当前访问日志格式：``text``（默认）或 ``json``。

    与 config.py 的字符串型环境变量同一清洗口径（去 BOM / 成对引号 /
    空白）：docker --env-file、K8s ConfigMap、Windows ``set VAR="json"``
    会把引号连值一起注入，不清洗时 JSON 结构化日志静默失效且无告警。
    """
    value = os.environ.get("LOG_FORMAT", "").replace("\ufeff", "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    mode = value.lower() or "text"
    if mode in {"text", "json"}:
        return mode
    if mode not in _warned_formats:
        _warned_formats.add(mode)
        logging.getLogger(__name__).warning(
            "LOG_FORMAT=%r 不是 text/json，按 text 输出", mode[:40]
        )
    return "text"


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
    safe_method = _safe_log_text(method)[:MAX_METHOD_TEXT]
    safe_path = _safe_log_text(path)[:MAX_LOG_TEXT]
    if mode == "json":
        return json.dumps(
            {
                "event": "http_request",
                "method": safe_method,
                "path": safe_path,
                "status": int(status),
                "duration_ms": round(float(elapsed_ms), 1),
                "request_id": _safe_log_text(request_id),
            },
            ensure_ascii=False,
        )
    return (
        f"{safe_method} {_escape_text_delimiters(safe_path)} -> {status} "
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
        # 序列化后再清洗：json.dumps 已转义 \x00-\x1f（作为 \uXXXX 文本），
        # 但 ensure_ascii=False 会原样输出 U+2028/U+2029 与 C1 控制符，按行
        # 切分的采集器会把单条记录拆成多行、后续行不再是合法 JSON
        return _safe_log_text(json.dumps(payload, ensure_ascii=False))
