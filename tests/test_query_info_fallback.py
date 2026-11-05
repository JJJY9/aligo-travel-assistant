"""
信息查询智能体天气回退测试（无需 API Key、不访问网络）

覆盖：
1. 天气接口不可用（返回 query_success=False）时，自动回退到联网搜索；
2. 天气接口正常时不触发联网搜索；
3. 天气查询抛异常时同样回退；
4. 天气结果格式化为中文摘要（复用 utils.weather.fetch_weather）。

运行：python tests/test_query_info_fallback.py
"""
import asyncio
import importlib.util
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agentscope.message import Msg


def _load_agent_class():
    path = os.path.join(project_root, ".claude", "skills", "query-info", "script", "agent.py")
    spec = importlib.util.spec_from_file_location("skills.query_info.agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.InformationQueryAgent


class _FakeModel:
    """搜索摘要用的假模型"""

    async def __call__(self, messages, **kwargs):
        class _Resp:
            pass

        resp = _Resp()
        resp.text = "明天杭州多云，12-20℃。"
        return resp


def _msg():
    return Msg(name="user", content="杭州明天天气怎么样？", role="user")


def test_weather_failure_falls_back_to_search():
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())
    calls = {"weather": 0, "search": 0}

    async def fake_weather(query):
        calls["weather"] += 1
        return {
            "query_type": "天气查询",
            "query_success": False,
            "results": {"message": "天气接口暂时不可用"},
        }

    async def fake_search(query):
        calls["search"] += 1
        return {
            "query_type": "网络搜索",
            "query_success": True,
            "results": {"summary": "明天杭州多云 12-20℃", "sources": []},
        }

    agent._weather_query = fake_weather
    agent._web_search = fake_search

    result = json.loads(asyncio.run(agent.reply(_msg())).content)
    assert calls == {"weather": 1, "search": 1}, "天气失败后必须回退到联网搜索"
    assert result["query_success"] is True
    assert "多云" in result["results"]["summary"]
    print("✓ 天气接口失败时回退联网搜索")


def test_weather_exception_falls_back_to_search():
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())
    calls = {"search": 0}

    async def boom(query):
        raise ConnectionError("ssl eof")

    async def fake_search(query):
        calls["search"] += 1
        return {
            "query_type": "网络搜索",
            "query_success": True,
            "results": {"summary": "搜索得到天气信息", "sources": []},
        }

    agent._weather_query = boom
    agent._web_search = fake_search

    result = json.loads(asyncio.run(agent.reply(_msg())).content)
    assert calls["search"] == 1
    assert result["query_success"] is True
    print("✓ 天气查询抛异常时回退联网搜索")


def test_weather_success_no_search():
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())
    calls = {"search": 0}

    async def fake_weather(query):
        return {
            "query_type": "天气查询",
            "query_success": True,
            "results": {"summary": "杭州当前多云 18℃", "sources": []},
        }

    async def fake_search(query):
        calls["search"] += 1
        return {"query_type": "网络搜索", "query_success": True, "results": {"summary": "x"}}

    agent._weather_query = fake_weather
    agent._web_search = fake_search

    result = json.loads(asyncio.run(agent.reply(_msg())).content)
    assert calls["search"] == 0, "天气可用时不应触发搜索"
    assert result["query_type"] == "天气查询"
    print("✓ 天气可用时不触发搜索")


def test_extract_city():
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())
    assert agent._extract_city_from_query("杭州明天天气怎么样？") == "杭州"
    assert agent._extract_city_from_query("南阳明天天气怎么样？") == "南阳"
    print("✓ 城市提取正常")


def test_weather_query_open_meteo_formatting():
    """fetch_weather 的结构化结果应格式化为可读的中文摘要"""
    from utils import weather

    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())

    def fake_fetch(city, timeout=8.0):
        assert city == "杭州"
        return {
            "city": "杭州",
            "timezone": "Asia/Shanghai",
            "current": {
                "description": "多云",
                "temperature": 18.5,
                "humidity": 72,
                "weather_code": 2,
            },
            "daily": [
                {"date": "2026-11-04", "description": "多云", "min": 12, "max": 20},
                {"date": "2026-11-05", "description": "小雨", "min": 11, "max": 18},
            ],
            "source": {"name": "Open-Meteo", "url": "https://open-meteo.com"},
        }

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(weather, "fetch_weather", fake_fetch)
        result = asyncio.run(agent._weather_query("杭州明天天气怎么样？"))
    finally:
        mp.undo()
    assert result["query_success"] is True
    summary = result["results"]["summary"]
    assert "杭州当前天气：多云" in summary
    assert "18.5" in summary
    assert "小雨" in summary
    print("✓ 天气结果格式化正常")


def test_weather_query_unknown_city():
    from utils import weather

    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())

    def fake_fetch(city, timeout=8.0):
        raise weather.WeatherError(f"未找到城市「{city}」")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(weather, "fetch_weather", fake_fetch)
        result = asyncio.run(agent._weather_query("某些小城天气怎么样"))
    finally:
        mp.undo()
    assert result["query_success"] is False
    assert "未找到城市" in result["results"]["message"]
    assert "请检查城市名" in result["results"]["message"]
    print("✓ 城市未找到时返回失败")


def test_weather_query_unavailable():
    from utils import weather

    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())

    def fake_fetch(city, timeout=8.0):
        raise weather.WeatherError("天气接口暂时不可用：network down")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(weather, "fetch_weather", fake_fetch)
        result = asyncio.run(agent._weather_query("杭州天气怎么样"))
    finally:
        mp.undo()
    assert result["query_success"] is False
    assert "天气接口暂时不可用" in result["results"]["message"]
    assert result["results"]["sources"][0]["title"] == "Open-Meteo"
    print("✓ 天气接口不可用时返回失败并附来源")


if __name__ == "__main__":
    test_weather_failure_falls_back_to_search()
    test_weather_exception_falls_back_to_search()
    test_weather_success_no_search()
    test_extract_city()
    test_weather_query_open_meteo_formatting()
    test_weather_query_unknown_city()
    test_weather_query_unavailable()
    print("\n全部通过")
