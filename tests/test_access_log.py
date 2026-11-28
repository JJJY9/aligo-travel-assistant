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


def test_text_format_escapes_path_delimiters():
    """文本格式按空格 / 箭头 / 括号分词：路径里的同名分隔符必须转义。

    uvicorn 会 percent-decode 路径，未转义时可伪造出完整伪访问记录。
    """
    path = "/api/a b(c)[d]>"
    line = format_access_log("GET", path, 200, 1.0, "rid", fmt="text")
    assert line == "GET /api/a%20b%28c%29%5Bd%5D%3E -> 200 (1ms) [rid]"
    # JSON 模式字段独立，路径原样保留（不改变机器消费方契约）
    data = json.loads(format_access_log("GET", path, 200, 1.0, "rid", fmt="json"))
    assert data["path"] == path

    # Unicode 空白（全角空格 / NBSP）：按 str.split() 分词的分析器同样会把
    # 它们当分隔符，文本格式必须一并转义 / 替换，否则字段错位
    ideographic_space = chr(0x3000)
    nbsp = chr(0xA0)
    unicode_path = f"/api/a{ideographic_space}b{nbsp}c"
    line = format_access_log("GET", unicode_path, 200, 1.0, "rid", fmt="text")
    assert ideographic_space not in line and nbsp not in line, line
    assert len(line.split()) == 6, line.split()
    print("✓ 文本访问日志转义路径分隔符")


def test_env_switch(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    assert access_log_format() == "json"
    data = json.loads(format_access_log("GET", "/api/x", 500, 1.0, "r"))
    assert data["status"] == 500

    monkeypatch.delenv("LOG_FORMAT")
    assert access_log_format() == "text"
    assert "->" in format_access_log("GET", "/api/x", 200, 1.0, "r")
    print("✓ LOG_FORMAT 环境变量切换")


def test_env_switch_strips_injected_quotes_and_bom(monkeypatch, caplog):
    """文件注入形态（成对引号 / BOM）仍启用 JSON 日志，未知值告警一次。"""
    import logging

    monkeypatch.setenv("LOG_FORMAT", '"json"')
    assert access_log_format() == "json"
    data = json.loads(format_access_log("GET", "/api/x", 200, 1.0, "r"))
    assert data["status"] == 200

    monkeypatch.setenv("LOG_FORMAT", chr(0xFEFF) + "JSON")
    assert access_log_format() == "json"

    monkeypatch.setenv("LOG_FORMAT", "jsno")
    with caplog.at_level(logging.WARNING):
        assert access_log_format() == "text"
    assert "LOG_FORMAT" in caplog.text, caplog.text
    # 每请求都会调用：同一未知值只告警一次
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert access_log_format() == "text"
    assert caplog.text == "", caplog.text
    print("✓ LOG_FORMAT 去成对引号 / BOM，未知值告警一次")


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
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    import pytest

    mp = pytest.MonkeyPatch()
    try:
        test_text_format_default()
        test_text_format_escapes_path_delimiters()
        test_json_format()
        test_env_switch(mp)
        test_json_formatter_merges_access_fields()
    finally:
        mp.undo()
    import _caplog_shim

    _shim = _caplog_shim.CaplogShim()
    mp2 = pytest.MonkeyPatch()
    try:
        test_env_switch_strips_injected_quotes_and_bom(mp2, _shim)
    finally:
        mp2.undo()
        _shim.close()
    print("\n全部通过")
