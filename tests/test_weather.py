"""
Open-Meteo 天气工具测试（无需 API Key、不访问网络）

运行：python tests/test_weather.py
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils import weather


class _Response:
    def __init__(self, data, error=None):
        self._data = data
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._data


def _geo(city="北京"):
    return {
        "results": [
            {
                "name": city,
                "latitude": 39.9,
                "longitude": 116.4,
                "timezone": "Asia/Shanghai",
            }
        ]
    }


def _forecast():
    return {
        "current": {
            "temperature_2m": 12.4,
            "relative_humidity_2m": 30,
            "weather_code": 0,
        },
        "daily": {
            "time": ["2026-11-05", "2026-11-06", "2026-11-07"],
            "weather_code": [0, 3, 61],
            "temperature_2m_min": [5, 6, 8],
            "temperature_2m_max": [15, 16, 12],
        },
    }


def test_fetch_weather_and_cache(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        return _Response(_geo() if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()

    data = weather.fetch_weather("北京")
    assert data["city"] == "北京"
    assert data["current"]["description"] == "晴"
    assert data["current"]["temperature"] == 12.4
    assert [day["description"] for day in data["daily"]] == ["晴", "阴", "小雨"]
    assert data["daily"][0]["min"] == 5 and data["daily"][0]["max"] == 15

    count = len(calls)
    weather.fetch_weather("北京")
    assert len(calls) == count, "10 分钟内应命中缓存"
    print("✓ 天气查询与进程内缓存")


def test_forecast_uses_location_timezone(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {})))
        if "geocoding" in url:
            return _Response(
                {
                    "results": [
                        {
                            "name": "New York",
                            "latitude": 40.7,
                            "longitude": -74.0,
                            "timezone": "America/New_York",
                        }
                    ]
                }
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()

    data = weather.fetch_weather("纽约")
    assert data["timezone"] == "America/New_York"
    assert calls[-1][1]["timezone"] == "America/New_York", "预报应按地点自身时区聚合日期"
    print("✓ 天气预报使用地点自身时区")


def test_unknown_city_raises(monkeypatch):
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda *args, **kwargs: _Response({"results": []}),
    )
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    print("✓ 未找到城市抛 WeatherError")


def test_request_failure_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", boom)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="天气接口暂时不可用"):
        weather.fetch_weather("北京")
    print("✓ 接口故障抛 WeatherError")


def test_empty_city_raises():
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="城市不能为空"):
        weather.fetch_weather("   ")
    print("✓ 空城市拒绝")


def test_http_error_maps_to_weather_error(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        return _Response({}, error=weather.requests.HTTPError("429 Too Many Requests"))

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="天气接口暂时不可用"):
        weather.fetch_weather("北京")
    print("✓ 上游 429 / 5xx 映射为 WeatherError")


def test_malformed_payload_raises(monkeypatch):
    payloads = [
        {"current": "oops", "daily": []},
        {"current": {}, "daily": {"time": 3}},
        {"current": {}, "daily": {"time": ["2026-11-05"], "weather_code": 1}},
    ]
    weather.clear_weather_cache()
    for payload in payloads:
        def fake_get(url, params=None, timeout=None, headers=None, _payload=payload):
            return _Response(_geo() if "geocoding" in url else _payload)

        monkeypatch.setattr(weather.requests, "get", fake_get)
        with pytest.raises(weather.WeatherError, match="天气接口返回格式异常"):
            weather.fetch_weather("北京")
    print("✓ 上游返回形状异常映射为 WeatherError")


def test_cache_is_bounded_and_prunes_expired(monkeypatch):
    monkeypatch.setattr(weather, "_CACHE_MAX_ENTRIES", 2)

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(
                {"results": [{"name": params["name"], "latitude": 1, "longitude": 2}]}
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    for city in ("甲城", "乙城", "丙城"):
        weather.fetch_weather(city)
    assert list(weather._cache) == ["乙城", "丙城"], "超过上限应淘汰最旧条目"

    weather._cache["丁城"] = (time.monotonic() - weather.CACHE_TTL_SEC - 1, {})
    weather.fetch_weather("戊城")
    assert set(weather._cache) == {"丙城", "戊城"}, "写入时应清理过期条目并维持上限"
    print("✓ 天气缓存有上限且清理过期条目")


def test_concurrent_misses_share_upstream_request(monkeypatch):
    calls = []
    calls_lock = threading.Lock()

    def fake_get(url, params=None, timeout=None, headers=None):
        with calls_lock:
            calls.append(url)
        time.sleep(0.05)
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: weather.fetch_weather("北京"), range(8)))
    assert all(item["city"] == "北京" for item in results)
    assert len(calls) == 2, f"同城并发未命中只应请求上游一次，实际 {len(calls)} 次"
    print("✓ 同城并发未命中合并为一次上游请求")


if __name__ == "__main__":
    for case in (
        test_fetch_weather_and_cache,
        test_forecast_uses_location_timezone,
        test_unknown_city_raises,
        test_request_failure_raises,
        test_http_error_maps_to_weather_error,
        test_malformed_payload_raises,
        test_cache_is_bounded_and_prunes_expired,
        test_concurrent_misses_share_upstream_request,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_empty_city_raises()
    print("\n全部通过")
