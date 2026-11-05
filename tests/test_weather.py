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


@pytest.fixture(autouse=True)
def _isolate_weather_cache():
    """天气缓存是进程级全局状态，避免成功 / 失败结果在用例之间泄漏。"""
    weather.clear_weather_cache()
    yield
    weather.clear_weather_cache()


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
        weather.clear_weather_cache()
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

    weather._cache["丁城"] = (time.monotonic() - weather.STALE_TTL_SEC - 1, {})
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


def test_cache_key_ignores_case_and_extra_space(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            calls.append(params["name"])
            return _Response(
                {"results": [{"name": params["name"], "latitude": 1, "longitude": 2}]}
            )
        calls.append("forecast")
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    first = weather.fetch_weather("Beijing")
    second = weather.fetch_weather("  beijing ")
    assert first == second
    assert calls == ["Beijing", "forecast"], "大小写 / 空白不同的同一城市应共享缓存"
    print("✓ 缓存键忽略大小写与多余空白")


def test_failure_cache_prevents_repeat_requests(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        return _Response({"results": []})

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 1

    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 1, "失败结果在 TTL 内不应重复请求上游"
    print("✓ 失败结果短时缓存，避免重复请求上游")


def test_failure_cache_expires(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        return _Response({"results": []})

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 1

    # 直接把失败条目的时间戳拨到 TTL 之外（Windows 上 sleep 精度不足 15.6ms）
    at, message = weather._failures["不存在"]
    weather._failures["不存在"] = (at - weather.FAILURE_TTL_SEC - 1, message)
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 2, "失败缓存过期后应重新请求上游"
    print("✓ 失败缓存过期后重试上游")


def test_success_clears_cached_failure(monkeypatch):
    responses = [{"results": []}, _geo("北京")]

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(responses.pop(0))
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    weather._failures.clear()  # 模拟缓存过期后上游恢复
    data = weather.fetch_weather("北京")
    assert data["city"] == "北京"
    assert "北京" not in weather._failures
    print("✓ 成功结果清除同城失败缓存")


def test_stale_cache_served_when_upstream_fails(monkeypatch):
    def ok_get(url, params=None, timeout=None, headers=None):
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", ok_get)
    weather.clear_weather_cache()
    fresh = weather.fetch_weather("北京")
    assert "stale" not in fresh

    # 缓存超过新鲜期但仍在 stale 窗口内
    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.CACHE_TTL_SEC - 1, result)

    def fail_get(url, params=None, timeout=None, headers=None):
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", fail_get)
    stale = weather.fetch_weather("北京")
    assert stale["stale"] is True
    assert stale["city"] == "北京"
    assert weather._failures == {}, "沿用旧数据时不应记录失败缓存"
    print("✓ 上游故障时沿用 stale 窗口内的成功数据")


def test_stale_cache_expires(monkeypatch):
    def ok_get(url, params=None, timeout=None, headers=None):
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", ok_get)
    weather.clear_weather_cache()
    weather.fetch_weather("北京")

    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.STALE_TTL_SEC - 1, result)

    def fail_get(url, params=None, timeout=None, headers=None):
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", fail_get)
    with pytest.raises(weather.WeatherError, match="天气接口暂时不可用"):
        weather.fetch_weather("北京")
    assert weather._failures, "超过 stale 窗口后应记录失败缓存"
    print("✓ 超过 stale 窗口后不再沿用旧数据")


def test_unknown_city_does_not_use_stale_cache(monkeypatch):
    responses = [_geo("北京"), {"results": []}]

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(responses.pop(0))
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    weather.fetch_weather("北京")

    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.CACHE_TTL_SEC - 1, result)

    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    print("✓ 城市不存在时不使用旧缓存掩盖错误")


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
        test_cache_key_ignores_case_and_extra_space,
        test_failure_cache_prevents_repeat_requests,
        test_failure_cache_expires,
        test_success_clears_cached_failure,
        test_stale_cache_served_when_upstream_fails,
        test_stale_cache_expires,
        test_unknown_city_does_not_use_stale_cache,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_empty_city_raises()
    print("\n全部通过")
