"""Open-Meteo 天气查询（无需 API Key）。

城市名 → 地理编码 → 实时天气 + 未来 3 天预报。Web 侧栏与查询技能共用同一套
天气码映射；结果带 10 分钟进程内缓存，避免频繁请求免费 API。
"""
import logging
import math
import threading
import time
from collections import deque
from typing import Any

import requests

logger = logging.getLogger(__name__)

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEZONE_DEFAULT = "Asia/Shanghai"
CACHE_TTL_SEC = 600.0
# 上游故障时最多沿用最近一次成功结果的窗口（stale-while-error）
STALE_TTL_SEC = 3600.0
# 失败结果（城市不存在 / 上游故障）短时缓存：避免侧栏每次刷新都重打上游
FAILURE_TTL_SEC = 60.0
# 降级结果（stale）在短窗内直接复用：不再重打已故障的上游
_DEGRADED_REUSE_SEC = 60.0
# 单飞等待者最多等待的轮数：每轮等待领先线程 timeout*2+1 秒后重新登记；
# 全部轮次用尽仍无结果时按「服务繁忙」报错，绝不各自重打上游（惊群）
_FOLLOWER_WAIT_ROUNDS = 3
# 全进程上游请求窗口上限：/api/weather 有按 IP 的桶，但对话链路（query-info
# 的 fetch_weather）不受它约束——没有全局限额时轮换城市名即可把 Open-Meteo
# 免费额度（约 600/分钟、5000/小时）打爆，所有用户的天气一起退化
_UPSTREAM_MAX_PER_MINUTE = 120
_upstream_calls: deque[float] = deque()
# Open-Meteo 免费 API 对同一客户端超过 5 个并发上游请求直接 429（排队中的
# 也计入）：Web 线程池 8 worker 加技能侧每会话 4 worker，跨城市并发未命中
# 会越过阈值，把一次瞬时 429 固化成一分钟的失败缓存
_UPSTREAM_MAX_IN_FLIGHT = 5
_upstream_slots = threading.BoundedSemaphore(_UPSTREAM_MAX_IN_FLIGHT)
# 中文城市名的地理编码（language=zh）对境外城市排序很差：「罗马」只命中
# 澳大利亚同名小镇、「东京」命中江苏村庄、「纽约」直接零结果。常去的境外
# 城市用一个小型别名表改查英文名（language=en 的索引与排序准确）。只收录
# 中文名——中文国内城市在 zh 索引里本来就准，不需要二次请求
_CITY_ALIASES_ZH_EN = {
    "东京": "Tokyo",
    "大阪": "Osaka",
    "京都": "Kyoto",
    "奈良": "Nara",
    "札幌": "Sapporo",
    "福冈": "Fukuoka",
    "名古屋": "Nagoya",
    "冲绳": "Okinawa",
    "首尔": "Seoul",
    "釜山": "Busan",
    "济州": "Jeju",
    "新加坡": "Singapore",
    "曼谷": "Bangkok",
    "普吉": "Phuket",
    "清迈": "Chiang Mai",
    "吉隆坡": "Kuala Lumpur",
    "雅加达": "Jakarta",
    "巴厘岛": "Bali",
    "河内": "Hanoi",
    "胡志明市": "Ho Chi Minh City",
    "马尼拉": "Manila",
    "迪拜": "Dubai",
    "多哈": "Doha",
    "阿布扎比": "Abu Dhabi",
    "纽约": "New York",
    "洛杉矶": "Los Angeles",
    "旧金山": "San Francisco",
    "西雅图": "Seattle",
    "芝加哥": "Chicago",
    "波士顿": "Boston",
    "华盛顿": "Washington",
    "拉斯维加斯": "Las Vegas",
    "温哥华": "Vancouver",
    "多伦多": "Toronto",
    "伦敦": "London",
    "巴黎": "Paris",
    "罗马": "Rome",
    "米兰": "Milan",
    "威尼斯": "Venice",
    "佛罗伦萨": "Florence",
    "法兰克福": "Frankfurt",
    "慕尼黑": "Munich",
    "柏林": "Berlin",
    "阿姆斯特丹": "Amsterdam",
    "布鲁塞尔": "Brussels",
    "苏黎世": "Zurich",
    "日内瓦": "Geneva",
    "维也纳": "Vienna",
    "布拉格": "Prague",
    "布达佩斯": "Budapest",
    "马德里": "Madrid",
    "巴塞罗那": "Barcelona",
    "里斯本": "Lisbon",
    "莫斯科": "Moscow",
    "伊斯坦布尔": "Istanbul",
    "悉尼": "Sydney",
    "墨尔本": "Melbourne",
    "奥克兰": "Auckland",
    "台北": "Taipei",
    "香港": "Hong Kong",
    "澳门": "Macau",
}
# 可信的地理编码结果：首都 / 行政中心，或人口达到规模的城市；两者都不满足
# 说明命中的很可能是同名村庄 / 机场，境外中文名改查英文索引
_GEO_TRUSTED_FEATURES = frozenset({"PPLC", "PPLA", "PPLA2"})
_GEO_MIN_TRUSTED_POPULATION = 100_000
_GEO_FEATURE_PRIORITY = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPL": 3}
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
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
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
# 刚降级返回过的 stale 结果：短窗内复用，与成功缓存分开存（时间戳不混用）
_degraded: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
# 同一城市并发未命中时，只让一个线程访问上游，其余线程等待它的结果
_inflight: dict[str, threading.Event] = {}


class WeatherError(Exception):
    """天气查询失败（城市不存在或接口不可用）。"""


def clear_weather_cache() -> None:
    """清空天气成功 / 失败 / 降级缓存（测试或运维需要时使用）。"""
    with _cache_lock:
        _cache.clear()
        _failures.clear()
        _degraded.clear()
        _upstream_calls.clear()


def peek_cached_weather(city: str) -> dict[str, Any] | None:
    """返回 10 分钟内的成功缓存；不访问上游、不消耗任何额度。

    端点在限流之前先查缓存：缓存命中不产生上游请求，不应占用为保护
    Open-Meteo 额度而设的 IP 桶（此前每轮面板刷新都白扣一次额度）。
    """
    name = str(city or "").strip()
    if not name:
        return None
    return _cache_hit(_cache_key(name))


def _cache_key(name: str) -> str:
    """归一化城市键：忽略大小写与多余空白，避免同一地点重复占用缓存。"""
    return " ".join(name.casefold().split())


def _describe(code: Any) -> str:
    """把 Open-Meteo 天气码转为中文描述（未知 / 非数字返回「未知」）。"""
    try:
        number = int(code)
    except (TypeError, ValueError, OverflowError):
        # float('inf') / float('nan') 转 int 会抛 OverflowError / ValueError
        return "未知"
    return WEATHER_CODE_MAP.get(number, "未知")


def _ensure_finite(value: Any) -> None:
    """递归校验响应中的数值都是有限值。

    ``json.loads`` 对 ``1e400`` 会静默溢出为 ``inf``（不触发 parse_constant），
    非有限值进入缓存后 ``/api/weather`` 的 JSON 序列化（allow_nan=False）每次
    500，因此解析后必须显式校验。
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WeatherError("天气接口返回异常数值")
        return
    if isinstance(value, dict):
        for item in value.values():
            _ensure_finite(item)
        return
    if isinstance(value, list):
        for item in value:
            _ensure_finite(item)


def _reject_constant(constant: str) -> Any:
    raise WeatherError(f"天气接口返回异常数值：{constant}")


def _get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    try:
        # NaN / Infinity 字面量按错误处理（requests 默认宽松解析）
        data = response.json(parse_constant=_reject_constant)
    except ValueError as exc:
        raise WeatherError("天气接口返回格式异常") from exc
    if not isinstance(data, dict):
        raise WeatherError("天气接口返回格式异常")
    _ensure_finite(data)
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
    """写入成功缓存：顺带清掉过期条目，并按上限淘汰最旧的一条。"""
    now = time.monotonic()
    keep_sec = max(CACHE_TTL_SEC, STALE_TTL_SEC)
    with _cache_lock:
        expired = [key for key, (at, _) in _cache.items() if now - at >= keep_sec]
        for key in expired:
            del _cache[key]
        if name not in _cache and len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            del _cache[oldest]
        _cache[name] = (now, result)
        _failures.pop(name, None)
        # 新鲜数据到达后，短窗内的降级复用立即失效
        _degraded.pop(name, None)


def _stale_entry(key: str) -> tuple[float, dict[str, Any]] | None:
    """上游失败时的降级数据：(最近一次成功的时间戳, 带 stale 标记的结果)。"""
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < STALE_TTL_SEC:
            return cached[0], {**cached[1], "stale": True}
    return None


def _store_degraded(key: str, stale: dict[str, Any]) -> None:
    """登记「刚刚降级返回过」：短窗内后续请求直接复用，不再重打故障上游。

    与成功缓存分开存储：降级结果不能拿当前时间去刷新新鲜度锚点，否则
    上游长时间故障时过期数据会被无限期沿用。
    """
    now = time.monotonic()
    with _cache_lock:
        expired = [
            name
            for name, (at, _) in _degraded.items()
            if now - at >= _DEGRADED_REUSE_SEC
        ]
        for name in expired:
            del _degraded[name]
        _degraded[key] = (now, stale)


def _degraded_hit(key: str) -> dict[str, Any] | None:
    """短窗内刚降级过的结果（单飞等待者 / 紧接着的请求直接复用）。"""
    now = time.monotonic()
    with _cache_lock:
        entry = _degraded.get(key)
        if entry is not None and now - entry[0] < _DEGRADED_REUSE_SEC:
            return {**entry[1]}
    return None


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


def _admit_upstream() -> None:
    """全进程上游额度：滑动窗口内请求数达到上限时直接拒绝。

    /api/weather 的按 IP 桶保护不到对话链路（query-info 直接调
    fetch_weather），城市名由用户输入、缓存按城市隔离，轮换城市名即可
    把 Open-Meteo 免费额度打爆——所有用户一起退化为 stale / 503。
    """
    now = time.monotonic()
    with _cache_lock:
        while _upstream_calls and now - _upstream_calls[0] >= 60.0:
            _upstream_calls.popleft()
        if len(_upstream_calls) >= _UPSTREAM_MAX_PER_MINUTE:
            raise WeatherError("天气查询过于频繁，请稍后再试")
        _upstream_calls.append(now)


def fetch_weather(city: str, timeout: float = 8.0) -> dict[str, Any]:
    """查询城市的实时天气与未来 3 天预报。

    同一城市的并发未命中只放行一个线程访问上游，其余线程等待并复用
    结果；成功结果缓存 10 分钟，失败结果缓存 60 秒。缓存键忽略大小写
    与多余空白。上游故障时，若该城市 1 小时内有过成功结果，则沿用旧
    数据并带 ``stale: true`` 标记。

    Raises:
        WeatherError: 城市为空 / 未找到，或 Open-Meteo 不可用且无旧数据可用。
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
    # 刚降级返回过（60 秒内）：直接复用旧数据，不重打已故障的上游
    degraded = _degraded_hit(key)
    if degraded is not None:
        return degraded

    # 单飞：同一城市只让一个线程打上游，其余等待并复用结果。等待者每轮
    # event.wait(timeout*2+1) 后重新登记；轮次用尽仍无结果时按「服务繁忙」
    # 报错——绝不能未抢到名额就自行请求上游（慢上游 / DNS 挂起时所有等待
    # 线程会同时重打，把单飞变成惊群并放大上游请求）
    for _ in range(_FOLLOWER_WAIT_ROUNDS + 1):
        leader, event = _claim_fetch(key)
        if leader:
            # 检查缓存 / 失败 / 降级与登记在途之间可能有别的线程刚好写入
            cached = _cache_hit(key)
            if cached is not None:
                _finish_fetch(key, event)
                return cached
            failed = _failure_hit(key)
            if failed is not None:
                _finish_fetch(key, event)
                raise WeatherError(failed)
            degraded = _degraded_hit(key)
            if degraded is not None:
                _finish_fetch(key, event)
                return degraded
            try:
                _admit_upstream()
            except WeatherError:
                # 全局额度用尽：先沿用该城市 1 小时内的成功数据。额度是全局
                # 状态，不能按城市写失败缓存——否则窗口释放后该城市仍被钉住
                # 60 秒，额度恢复也看不到天气
                _finish_fetch(key, event)
                entry = _stale_entry(key)
                if entry is not None:
                    _stored_at, stale = entry
                    logger.warning("天气上游额度用尽，沿用最近一次成功数据")
                    _store_degraded(key, stale)
                    return stale
                raise
            break
        # 等待领先线程：成功时缓存已写入，直接复用；失败或降级同理
        event.wait(timeout * 2 + 1)
        cached = _cache_hit(key)
        if cached is not None:
            return cached
        failed = _failure_hit(key)
        if failed is not None:
            raise WeatherError(failed)
        degraded = _degraded_hit(key)
        if degraded is not None:
            return degraded
    else:
        message = "天气服务繁忙，请稍后再试"
        _store_failure(key, message)
        raise WeatherError(message)
    try:
        return _fetch_weather_uncached(name, key, timeout)
    except WeatherError as exc:
        message = str(exc)
        # 上游抖动 / 格式异常时沿用最近一次成功数据；城市不存在没有旧数据可用
        entry = None if message.startswith("未找到城市") else _stale_entry(key)
        if entry is not None:
            _stored_at, stale = entry
            logger.warning("天气接口失败，沿用最近一次成功数据")
            # 降级结果单独登记（不写成功缓存）：短窗内复用给并发等待者与后续
            # 请求，但不会刷新「最近一次成功」的时间戳、把 1 小时窗口无限滑动
            _store_degraded(key, stale)
            return stale
        _store_failure(key, message)
        raise
    finally:
        if leader:
            _finish_fetch(key, event)


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def _is_trusted_location(item: dict[str, Any]) -> bool:
    feature = str(item.get("feature_code") or "")
    if feature in _GEO_TRUSTED_FEATURES:
        return True
    try:
        population = int(item.get("population") or 0)
    except (TypeError, ValueError):
        population = 0
    return population >= _GEO_MIN_TRUSTED_POPULATION


def _best_geocode_match(
    name: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """按「同名优先 → 人口 → 行政级别」排序，取最可信的一条。"""
    wanted = name.strip().casefold()

    def rank(item: dict[str, Any]) -> tuple[int, int, int]:
        item_name = str(item.get("name") or "").strip().casefold()
        exact = 0 if item_name == wanted else 1
        try:
            population = int(item.get("population") or 0)
        except (TypeError, ValueError):
            population = 0
        feature = str(item.get("feature_code") or "")
        return (exact, -population, _GEO_FEATURE_PRIORITY.get(feature, 4))

    return min(candidates, key=rank)


def _geocode_candidates(
    name: str, language: str, timeout: float
) -> list[dict[str, Any]]:
    geo = _get_json(
        GEO_URL,
        {"name": name, "count": 10, "language": language, "format": "json"},
        timeout,
    )
    return [item for item in (geo.get("results") or []) if isinstance(item, dict)]


def _geocode(name: str, timeout: float) -> dict[str, Any]:
    """城市名 → 地理编码最优匹配。

    中文名查 zh 索引；别名表里的境外城市同时查 en 索引并优先采用可信的
    英文结果（zh 索引的境外城市排序/覆盖都不可靠）。
    """
    if not _has_cjk(name):
        candidates = _geocode_candidates(name, "en", timeout)
        if not candidates:
            raise WeatherError(f"未找到城市「{name}」")
        return _best_geocode_match(name, candidates)

    candidates = _geocode_candidates(name, "zh", timeout)
    alias = _CITY_ALIASES_ZH_EN.get(name.strip())
    if alias:
        # 别名表里的中文境外城市一律以 en 索引为准：zh 索引里可能只有同名
        # 小城（「伦敦」→ 加拿大 London，人口 42 万，按人口门槛也会被当成
        # 可信结果），英文索引才能拿到真正的伦敦 / 纽约 / 罗马
        english = [
            item
            for item in _geocode_candidates(alias, "en", timeout)
            if _is_trusted_location(item)
        ]
        if english:
            return _best_geocode_match(alias, english)
    if not candidates:
        raise WeatherError(f"未找到城市「{name}」")
    return _best_geocode_match(name, candidates)


def _fetch_weather_uncached(name: str, key: str, timeout: float) -> dict[str, Any]:
    # 上游并发闸门：Open-Meteo 免费 API 同一客户端超过 5 个并发请求即 429；
    # 等待超过单个请求超时就按繁忙报错，由上层走 stale / 503
    if not _upstream_slots.acquire(timeout=timeout):
        raise WeatherError("天气服务繁忙，请稍后再试")
    try:
        try:
            location = _geocode(name, timeout)
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
            # requests 的异常文本含完整上游 URL 与查询参数（城市名）：只记
            # 异常类型，原始异常由 raise ... from 保留给调用栈
            logger.warning("天气请求失败：%s", type(exc).__name__)
            raise WeatherError("天气接口暂时不可用") from exc
        except WeatherError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("天气接口返回格式异常") from exc
    finally:
        _upstream_slots.release()

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
