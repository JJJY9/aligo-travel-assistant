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
CACHE_TTL_SEC = 600.0
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
_cache_lock = threading.Lock()


class WeatherError(Exception):
    """天气查询失败（城市不存在或接口不可用）。"""


def clear_weather_cache() -> None:
    """清空天气缓存（测试或运维需要时使用）。"""
    with _cache_lock:
        _cache.clear()


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


def fetch_weather(city: str, timeout: float = 8.0) -> dict[str, Any]:
    """查询城市的实时天气与未来 3 天预报。

    Raises:
        WeatherError: 城市为空 / 未找到，或 Open-Meteo 不可用。
    """
    name = city.strip()
    if not name:
        raise WeatherError("城市不能为空")

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(name)
        if cached is not None and now - cached[0] < CACHE_TTL_SEC:
            return cached[1]

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
                "timezone": str(location.get("timezone") or "Asia/Shanghai"),
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

    current_raw = data.get("current") or {}
    daily_raw = data.get("daily") or {}
    dates = daily_raw.get("time") or []
    codes = daily_raw.get("weather_code") or []
    lows = daily_raw.get("temperature_2m_min") or []
    highs = daily_raw.get("temperature_2m_max") or []
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

    result = {
        "city": str(location.get("name") or name),
        "timezone": str(location.get("timezone") or "Asia/Shanghai"),
        "current": {
            "description": _describe(current_raw.get("weather_code")),
            "temperature": current_raw.get("temperature_2m"),
            "humidity": current_raw.get("relative_humidity_2m"),
            "weather_code": current_raw.get("weather_code"),
        },
        "daily": daily,
        "source": {"name": "Open-Meteo", "url": "https://open-meteo.com"},
    }
    with _cache_lock:
        _cache[name] = (time.monotonic(), result)
    return result
