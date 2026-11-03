"""
访问日志格式测试（无需 API Key、不访问网络）

运行：python tests/test_access_log.py
"""
import json
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.access_log import access_log_format, format_access_log


def test_text_format_default():
    line = format_access_log("GET", "/api/health", 200, 12.34, "abc123", fmt="text")
    assert line == "GET /api/health -> 200 (12ms) [abc123]"
    print("✓ 默认文本格式")


def test_json_format():
    line = format_access_log("POST", "/api/chat", 429, 0.456, "rid", fmt="json")
    data = json.loads(line)
    assert data == {
        "event": "http_request",
        "method": "POST",
        "path": "/api/chat",
        "status": 429,
        "duration_ms": 0.5,
        "request_id": "rid",
    }
    print("✓ JSON 格式字段完整")


def test_env_switch(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    assert access_log_format() == "json"
    data = json.loads(format_access_log("GET", "/api/x", 500, 1.0, "r"))
    assert data["status"] == 500

    monkeypatch.delenv("LOG_FORMAT")
    assert access_log_format() == "text"
    assert "->" in format_access_log("GET", "/api/x", 200, 1.0, "r")
    print("✓ LOG_FORMAT 环境变量切换")


def test_json_formatter_merges_access_fields():
    """JSON 模式下整行必须是合法 JSON，访问日志字段提升到顶层。"""
    import logging

    from utils.access_log import JsonLogFormatter

    access = format_access_log("GET", "/api/x", 200, 3.2, "rid", fmt="json")
    record = logging.LogRecord(
        "webapp.server", logging.INFO, __file__, 10, "%s", (access,), None
    )
    data = json.loads(JsonLogFormatter().format(record))
    assert data["event"] == "http_request"
    assert data["path"] == "/api/x"
    assert data["status"] == 200
    assert data["request_id"] == "rid"
    assert data["logger"] == "webapp.server"
    assert data["level"] == "INFO"
    assert data["time"]

    plain = logging.LogRecord(
        "webapp.server", logging.WARNING, __file__, 11, "plain %s", ("msg",), None
    )
    data = json.loads(JsonLogFormatter().format(plain))
    assert data["message"] == "plain msg"
    assert data["level"] == "WARNING"
    print("✓ JSON formatter 输出合法 JSON（访问字段在顶层）")


if __name__ == "__main__":
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        test_text_format_default()
        test_json_format()
        test_env_switch(mp)
        test_json_formatter_merges_access_fields()
    finally:
        mp.undo()
    print("\n全部通过")
