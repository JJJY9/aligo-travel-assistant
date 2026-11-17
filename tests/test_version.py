"""
版本号与 CLI --version 测试（无需 API Key）

运行：python tests/test_version.py
"""
import os
import re
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils.version import APP_VERSION
from webapp import server


def test_version_format_and_shared():
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)
    assert server.APP_VERSION == APP_VERSION
    print("✓ 版本号格式与 Web 共用同一常量")


def test_cli_version_flag(monkeypatch, capsys):
    for flag in ("--version", "-V"):
        monkeypatch.setattr(sys, "argv", ["cli.py", flag])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert APP_VERSION in capsys.readouterr().out
    print("✓ cli.py --version / -V")


_DIRECT_RUN_SKIP: dict[str, str] = {
    "test_cli_version_flag": "需要 capsys 夹具",
}

if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_version_format_and_shared()
    print("\n全部通过（--version 用例需 pytest 的 capsys）")
    if _DIRECT_RUN_SKIP:
        print(f"直跑未纳入 {len(_DIRECT_RUN_SKIP)} 个用例（需 pytest 专用夹具或真实服务）")
