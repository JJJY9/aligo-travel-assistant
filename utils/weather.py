"""Open-Meteo 天气查询（无需 API Key）。

城市名 → 地理编码 → 实时天气 + 未来 3 天预报。Web 侧栏与查询技能共用同一套
天气码映射；结果带 10 分钟进程内缓存，避免频繁请求免费 API。
"""
import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEZONE_DEFAULT = "Asia/Shanghai"
CACHE_TTL_SEC = 600.0
# 失败结果（城市不存在 / 上游故障）短时缓存：避免侧栏每次刷新都重打上游
FAILURE_TTL_SEC = 60.0
# 缓存条目上限：城市名来自请求参数，不能让缓存随查询过的城市无限增长
_CACHE_MAX_ENTRIES = 256
_USER_AGENT = "Mozilla/5.0 (compatible; AligoTravel/1.0)"

WEATHER_CODE_MAP: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_failures: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()
# 同一城市并发未命中时，只让一个线程访问上游，其余线程等待它的结果
_inflight: dict[str, threading.Event] = {}


class WeatherError(Exception):
    """天气查询失败（城市不存在或接口不可用）。"""


def clear_weather_cache() -> None:
    """清空天气成功 / 失败缓存（测试或运维需要时使用）。"""
    with _cache_lock:
        _cache.clear()
        _failures.clear()


def _cache_key(name: str) -> str:
    """归一化城市键：忽略大小写与多余空白，避免同一地点重复占用缓存。"""
    return " ".join(name.casefold().split())


def _describe(code: Any) -> str:
    """把 Open-Meteo 天气码转为中文描述（未知 / 非数字返回「未知」）。"""
    try:
        return WEATHER_CODE_MAP.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def _get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise WeatherError("天气接口返回格式异常")
    return data


def _cache_hit(name: str) -> dict[str, Any] | None:
    """返回未过期的缓存结果（过期条目留给写入时的清理）。"""
    with _cache_lock:
        cached = _cache.get(name)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SEC:
            return cached[1]
    return None


def _claim_fetch(name: str) -> tuple[bool, threading.Event]:
    """登记「该城市正在请求上游」，返回 (是否由本线程发起, 完成事件)。"""
    with _cache_lock:
        event = _inflight.get(name)
        if event is not None:
            return False, event
        event = threading.Event()
        _inflight[name] = event
        return True, event


def _finish_fetch(name: str, event: threading.Event) -> None:
    with _cache_lock:
        if _inflight.get(name) is event:
            _inflight.pop(name, None)
    event.set()


def _store_cache(name: str, result: dict[str, Any]) -> None:
    """写入缓存：顺带清掉过期条目，并按上限淘汰最旧的一条。"""
    now = time.monotonic()
    with _cache_lock:
        expired = [key for key, (at, _) in _cache.items() if now - at >= CACHE_TTL_SEC]
        for key in expired:
            del _cache[key]
        if name not in _cache and len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            del _cache[oldest]
        _cache[name] = (now, result)
        _failures.pop(name, None)


def _failure_hit(key: str) -> str | None:
    with _cache_lock:
        failed = _failures.get(key)
        if failed is not None and time.monotonic() - failed[0] < FAILURE_TTL_SEC:
            return failed[1]
    return None


def _store_failure(key: str, message: str) -> None:
    """失败结果短时缓存：同样的错误在 TTL 内直接复用，不再请求上游。"""
    now = time.monotonic()
    with _cache_lock:
        expired = [
            name for name, (at, _) in _failures.items() if now - at >= FAILURE_TTL_SEC
        ]
        for name in expired:
            del _failures[name]
        if key not in _failures and len(_failures) >= _CACHE_MAX_ENTRIES:
            oldest = min(_failures, key=lambda name: _failures[name][0])
            del _failures[oldest]
        _failures[key] = (now, message)


def fetch_weather(city: str, timeout: float = 8.0) -> dict[str, Any]:
    """查询城市的实时天气与未来 3 天预报。

    同一城市的并发未命中只放行一个线程访问上游，其余线程等待并复用
    结果；成功结果缓存 10 分钟，失败结果缓存 60 秒。缓存键忽略大小写
    与多余空白。

    Raises:
        WeatherError: 城市为空 / 未找到，或 Open-Meteo 不可用。
    """
    name = city.strip()
    if not name:
        raise WeatherError("城市不能为空")
    key = _cache_key(name)

    cached = _cache_hit(key)
    if cached is not None:
        return cached
    failed = _failure_hit(key)
    if failed is not None:
        raise WeatherError(failed)

    leader, event = _claim_fetch(key)
    if leader:
        # 检查缓存与登记在途之间可能有别的线程刚好写入了结果
        cached = _cache_hit(key)
        if cached is not None:
            _finish_fetch(key, event)
            return cached
        failed = _failure_hit(key)
        if failed is not None:
            _finish_fetch(key, event)
            raise WeatherError(failed)
    else:
        # 等待领先线程：成功时缓存已写入，直接复用；失败或超时则自己重试
        event.wait(timeout * 2 + 1)
        cached = _cache_hit(key)
        if cached is not None:
            return cached
        failed = _failure_hit(key)
        if failed is not None:
            raise WeatherError(failed)
    try:
        return _fetch_weather_uncached(name, key, timeout)
    except WeatherError as exc:
        _store_failure(key, str(exc))
        raise
    finally:
        if leader:
            _finish_fetch(key, event)


def _fetch_weather_uncached(name: str, key: str, timeout: float) -> dict[str, Any]:
    try:
        geo = _get_json(
            GEO_URL,
            {"name": name, "count": 1, "language": "zh", "format": "json"},
            timeout,
        )
        matches = geo.get("results") or []
        if not matches:
            raise WeatherError(f"未找到城市「{name}」")
        location = matches[0]
        data = _get_json(
            FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": str(location.get("timezone") or TIMEZONE_DEFAULT),
                "forecast_days": 3,
            },
            timeout,
        )
    except requests.RequestException as exc:
        raise WeatherError(f"天气接口暂时不可用：{exc}") from exc
    except WeatherError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError("天气接口返回格式异常") from exc

    result = _parse_weather(name, location, data)
    _store_cache(key, result)
    return result


def _parse_weather(name: str, location: Any, data: Any) -> dict[str, Any]:
    """把 Open-Meteo 响应整理为统一结构；形状异常统一抛 WeatherError。"""
    try:
        if not isinstance(data, dict):
            raise WeatherError("天气接口返回格式异常")
        current_raw = data.get("current") or {}
        daily_raw = data.get("daily") or {}
        if not isinstance(current_raw, dict) or not isinstance(daily_raw, dict):
            raise WeatherError("天气接口返回格式异常")
        dates = daily_raw.get("time") or []
        codes = daily_raw.get("weather_code") or []
        lows = daily_raw.get("temperature_2m_min") or []
        highs = daily_raw.get("temperature_2m_max") or []
        if not all(isinstance(series, list) for series in (dates, codes, lows, highs)):
            raise WeatherError("天气接口返回格式异常")
        daily = []
        for index, date in enumerate(dates[:3]):
            code = codes[index] if index < len(codes) else None
            daily.append(
                {
                    "date": date,
                    "description": _describe(code),
                    "min": lows[index] if index < len(lows) else None,
                    "max": highs[index] if index < len(highs) else None,
                }
            )
        return {
            "city": str(location.get("name") or name),
            "timezone": str(location.get("timezone") or TIMEZONE_DEFAULT),
            "current": {
                "description": _describe(current_raw.get("weather_code")),
                "temperature": current_raw.get("temperature_2m"),
                "humidity": current_raw.get("relative_humidity_2m"),
                "weather_code": current_raw.get("weather_code"),
            },
            "daily": daily,
            "source": {"name": "Open-Meteo", "url": "https://open-meteo.com"},
        }
    except WeatherError:
        raise
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise WeatherError("天气接口返回格式异常") from exc
