"""
信息查询智能体天气回退测试（无需 API Key、不访问网络）

覆盖：
1. wttr.in 不可用（返回 query_success=False）时，自动回退到联网搜索；
2. 天气接口正常时不触发联网搜索；
3. 天气查询抛异常时同样回退。

运行：python tests/test_query_info_fallback.py
"""
import asyncio
import importlib.util
import json
import os
import sys

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
    """Open-Meteo 返回结果应格式化为可读的中文摘要"""
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())

    async def fake_http(url, params):
        if "geocoding" in url:
            return {"results": [{"name": "杭州", "latitude": 30.29, "longitude": 120.16}]}
        return {
            "current": {"temperature_2m": 18.5, "relative_humidity_2m": 72, "weather_code": 2},
            "daily": {
                "time": ["2026-11-04", "2026-11-05", "2026-11-06"],
                "weather_code": [2, 61, 0],
                "temperature_2m_min": [12, 11, 10],
                "temperature_2m_max": [20, 18, 22],
            },
        }

    agent._http_json = fake_http
    result = asyncio.run(agent._weather_query("杭州明天天气怎么样？"))
    assert result["query_success"] is True
    summary = result["results"]["summary"]
    assert "杭州当前天气：多云" in summary
    assert "18.5" in summary
    assert "小雨" in summary
    print("✓ Open-Meteo 天气结果格式化正常")


def test_weather_query_unknown_city():
    agent = _load_agent_class()(name="InformationQueryAgent", model=_FakeModel())

    async def fake_http(url, params):
        return {"results": []}

    agent._http_json = fake_http
    result = asyncio.run(agent._weather_query("某些小城天气怎么样"))
    assert result["query_success"] is False
    print("✓ 城市未找到时返回失败")


if __name__ == "__main__":
    test_weather_failure_falls_back_to_search()
    test_weather_exception_falls_back_to_search()
    test_weather_success_no_search()
    test_extract_city()
    test_weather_query_open_meteo_formatting()
    test_weather_query_unknown_city()
    print("\n全部通过")
