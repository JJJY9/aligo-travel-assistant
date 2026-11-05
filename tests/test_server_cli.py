"""
Web 服务启动参数测试（无需 API Key、不启动服务器）

运行：python tests/test_server_cli.py
"""
import logging
import os
import sys
import types

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from webapp import server


def test_loopback_host_detection():
    assert server._is_loopback_host("127.0.0.1")
    assert server._is_loopback_host("localhost")
    assert server._is_loopback_host("::1")
    assert not server._is_loopback_host("0.0.0.0")
    assert not server._is_loopback_host("192.168.1.5")
    print("✓ 环回地址判断")


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["aligo-web", "--version"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert server.APP_VERSION in capsys.readouterr().out
    print("✓ --version 输出版本")


def test_public_bind_warns(monkeypatch, caplog):
    fake_uvicorn = types.ModuleType("uvicorn")
    calls = {}

    def run(app, host, port, **kwargs):
        calls["host"] = host

    fake_uvicorn.run = run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(server, "create_app", lambda: object())
    monkeypatch.setattr(
        sys, "argv", ["aligo-web", "--host", "0.0.0.0", "--port", "9000"]
    )
    caplog.set_level(logging.WARNING, logger=server.logger.name)

    server.main()

    assert calls["host"] == "0.0.0.0"
    assert any("0.0.0.0" in record.getMessage() for record in caplog.records)
    print("✓ 非环回监听地址给出安全警告")


if __name__ == "__main__":
    test_loopback_host_detection()
    print("\n全部通过（--version / 监听警告用例需 pytest 的 capsys / caplog）")
