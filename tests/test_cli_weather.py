"""
`python cli.py weather` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_weather.py
"""
import contextlib
import io
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from utils import weather

_FORECAST = {
    "city": "北京",
    "timezone": "Asia/Shanghai",
    "current": {
        "description": "晴",
        "temperature": 12.4,
        "humidity": 30,
        "weather_code": 0,
    },
    "daily": [
        {"date": "2026-11-05", "description": "晴", "min": 5, "max": 15},
        {"date": "2026-11-06", "description": "阴", "min": None, "max": None},
    ],
    "source": {"name": "Open-Meteo", "url": "https://open-meteo.com"},
}


def _patch_fetch(monkeypatch, result=None, error=None):
    def fake_fetch(city, timeout=8.0):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(weather, "fetch_weather", fake_fetch)


def _run(city="北京", as_json=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_weather_standalone(city, as_json=as_json)
    return code, buf.getvalue()


def test_weather_text(monkeypatch):
    _patch_fetch(monkeypatch, result=dict(_FORECAST))
    code, output = _run()
    assert code == 0
    assert "北京 当前天气：晴" in output
    assert "12.4°C" in output
    assert "未来几天" in output
    assert "2026-11-05: 晴，5~15°C" in output
    assert "?~?°C" in output, "缺失温度显示为 ?"
    print("✓ cli.py weather 文本输出")


def test_weather_json(monkeypatch):
    _patch_fetch(monkeypatch, result=dict(_FORECAST))
    code, output = _run(as_json=True)
    payload = json.loads(output)
    assert code == 0
    assert payload["ok"] is True
    assert payload["city"] == "北京"
    assert payload["daily"][0]["max"] == 15
    print("✓ weather --json 输出")


def test_weather_failure(monkeypatch):
    _patch_fetch(monkeypatch, error=weather.WeatherError("未找到城市「不存在」"))
    code, output = _run("不存在")
    assert code == 1
    assert "未找到城市" in output
    print("✓ 城市不存在 → 退出码 1")


def test_weather_failure_json(monkeypatch):
    _patch_fetch(monkeypatch, error=weather.WeatherError("天气接口暂时不可用"))
    code, output = _run(as_json=True)
    payload = json.loads(output)
    assert code == 1
    assert payload["ok"] is False
    assert "天气接口暂时不可用" in payload["error"]
    print("✓ 失败时 --json 输出 ok=false")


def test_weather_stale_note(monkeypatch):
    _patch_fetch(monkeypatch, result={**_FORECAST, "stale": True})
    code, output = _run()
    assert code == 0
    assert "可能不是最新" in output
    print("✓ stale 数据在 CLI 中标注")


def test_main_dispatch(monkeypatch):
    _patch_fetch(monkeypatch, result=dict(_FORECAST))
    monkeypatch.setattr(sys, "argv", ["cli.py", "weather", "北京"])
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    print("✓ cli.py weather 命令分发")


def test_main_dispatch_usage(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cli.py", "weather"])
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    print("✓ 缺少城市 → 退出码 2")


if __name__ == "__main__":
    for case in (
        test_weather_text,
        test_weather_json,
        test_weather_failure,
        test_weather_failure_json,
        test_weather_stale_note,
        test_main_dispatch,
        test_main_dispatch_usage,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
