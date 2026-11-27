"""
Open-Meteo 天气工具测试（无需 API Key、不访问网络）

运行：python tests/test_weather.py
"""
import json
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

    def json(self, **kwargs):
        return self._data


class _FakeSharedRedis:
    """最小 Redis 替身：只实现天气共享缓存用到的 GET / SETEX。"""

    def __init__(self):
        self.data = {}
        self.get_calls = 0
        self.setex_calls = []

    def get(self, key):
        self.get_calls += 1
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.data[key] = value
        return True


class _BrokenSharedRedis:
    def __init__(self):
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        raise ConnectionError("redis down")

    def setex(self, key, ttl, value):
        raise ConnectionError("redis down")


def _shared_envelope(payload, fetched_at=None):
    return json.dumps(
        {
            "fetched_at": time.time() if fetched_at is None else fetched_at,
            "data": payload,
        },
        ensure_ascii=False,
    )


def test_shared_cache_hit_avoids_upstream(monkeypatch):
    """共享缓存命中直接返回并回填进程内缓存，不访问上游、不续期共享条目。"""
    client = _FakeSharedRedis()
    payload = {"city": "杭州", "current": {"temperature": 20}, "daily": []}
    key = weather._cache_key("杭州")
    client.data[f"aligo:weather:{key}"] = _shared_envelope(payload)
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    upstream_calls = []

    def fake_uncached(*args, **kwargs):
        upstream_calls.append(args)
        return {}

    monkeypatch.setattr(weather, "_fetch_weather_uncached", fake_uncached)
    monkeypatch.setattr(weather, "_admit_upstream", lambda: None)

    assert weather.fetch_weather("杭州") == payload
    assert not upstream_calls, "共享缓存命中不应访问上游"
    assert client.setex_calls == [], "回填进程内缓存不能续期共享条目"
    # 回填后同一进程再读命中进程内缓存，不再触达 Redis
    assert weather.fetch_weather("杭州") == payload
    assert client.get_calls == 1
    print("✓ 共享天气缓存命中避免上游调用")


def test_success_cache_write_is_shared(monkeypatch):
    """成功结果写入共享缓存（带 10 分钟 TTL 与抓取时刻信封）。"""
    client = _FakeSharedRedis()
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    key = weather._cache_key("杭州")
    sample = {"city": "杭州", "current": {"temperature": 18}, "daily": []}
    weather._store_cache(key, sample)
    assert client.setex_calls, "成功缓存应写入共享缓存"
    stored_key, ttl, raw = client.setex_calls[-1]
    assert stored_key == f"aligo:weather:{key}"
    assert ttl == int(weather.CACHE_TTL_SEC)
    envelope = json.loads(raw)
    assert envelope["data"] == sample
    assert abs(envelope["fetched_at"] - time.time()) < 60
    print("✓ 成功天气结果写入共享缓存")


def test_shared_fill_respects_cache_limits(monkeypatch):
    """共享缓存回填同样受进程内条数上限约束（不能无限膨胀）。"""
    client = _FakeSharedRedis()
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    limit = weather._CACHE_MAX_ENTRIES
    for index in range(limit + 50):
        payload = {"city": f"城市{index}", "current": {}, "daily": []}
        weather._fill_from_shared(f"city-{index}", payload, 0)
    with weather._cache_lock:
        assert len(weather._cache) <= limit, len(weather._cache)
    print("✓ 共享回填受进程内缓存上限约束")


def test_shared_cache_success_resets_redis_availability(monkeypatch):
    """共享缓存成功操作复位短期记忆的 Redis 可用性标记。"""
    from context import short_term_memory as module

    client = _FakeSharedRedis()
    payload = {"city": "杭州", "current": {}, "daily": []}
    key = weather._cache_key("杭州")
    client.data[f"aligo:weather:{key}"] = _shared_envelope(payload)
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    module.note_redis_operation_failure(ConnectionError("down"))
    assert module.redis_short_term_available() is False
    weather._shared_cache_get(key)
    assert module.redis_short_term_available() is True
    print("✓ 共享缓存成功操作复位 Redis 可用性")


def test_shared_cache_failure_enters_cooldown(monkeypatch):
    """Redis 操作失败后 60 秒冷却内不再触达客户端（避免每次等满超时）。"""
    from context import storage as storage_module

    broken = _BrokenSharedRedis()
    weather.reset_shared_cache_failure()
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: broken
    )
    assert weather._shared_cache_get("bj") is None
    assert weather._shared_cache_get("sh") is None
    assert broken.get_calls == 1, "冷却期内不应再触达 Redis"
    weather.reset_shared_cache_failure()
    assert weather._shared_cache_get("gz") is None
    assert broken.get_calls == 2, "重置冷却后重新尝试"
    print("✓ 共享天气缓存的 Redis 故障进入冷却")


def test_shared_cache_ignores_stale_or_invalid_entry(monkeypatch):
    """共享缓存的陈旧 / 非法负载按未命中处理（不阻断查询、不当成功结果）。"""
    client = _FakeSharedRedis()
    key = weather._cache_key("杭州")
    redis_key = f"aligo:weather:{key}"
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    payload = {"city": "杭州", "current": {}, "daily": []}
    # 超过 10 分钟：即使 Redis 里还在也不能当新鲜数据
    client.data[redis_key] = _shared_envelope(
        payload, fetched_at=time.time() - weather.CACHE_TTL_SEC - 5
    )
    assert weather._shared_cache_get(key) is None
    # 非 JSON / 非对象 / 结构不对 / 非有限数值：一律按未命中
    client.data[redis_key] = b"not-json"
    assert weather._shared_cache_get(key) is None
    client.data[redis_key] = _shared_envelope(
        {"city": "杭州", "current": {}, "daily": ["x"]}
    )
    assert weather._shared_cache_get(key) is None
    client.data[redis_key] = (
        '{"fetched_at": %s, "data": {"city": "杭州", '
        '"current": {"temperature": Infinity}, "daily": []}}' % time.time()
    ).encode()
    assert weather._shared_cache_get(key) is None
    print("✓ 共享缓存的陈旧 / 非法负载按未命中处理")


def test_shared_hit_anchors_age_to_fetch_time(monkeypatch):
    """回填的新鲜度锚点按真实抓取时刻：共享条目不会因回填续命。"""
    client = _FakeSharedRedis()
    payload = {"city": "杭州", "current": {"temperature": 20}, "daily": []}
    key = weather._cache_key("杭州")
    client.data[f"aligo:weather:{key}"] = _shared_envelope(
        payload, fetched_at=time.time() - 500
    )
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    monkeypatch.setattr(weather, "_fetch_weather_uncached", lambda *a, **k: {})
    monkeypatch.setattr(weather, "_admit_upstream", lambda: None)
    # 单调钟从 0 起算，CI 新 VM 的上机时长可能小于回拨量（< 500 秒），
    # 减法被钳到 0 后「回拨到抓取时刻」的断言失去意义（3.10 矩阵实测
    # monotonic()≈278s 时误报）：装一个大起点的时钟，只验证相对回拨
    monkeypatch.setattr(weather.time, "monotonic", lambda: 1_000_000.0)
    before = time.monotonic()
    assert weather.fetch_weather("杭州") == payload
    stamp = weather._cache[key][0]
    assert stamp <= before - 400, "锚点必须回拨到真实抓取时刻附近"
    assert stamp >= before - 700
    print("✓ 共享命中按真实抓取时刻回填新鲜度")


def test_shared_backfill_keeps_newer_local_entry():
    """并发下本地刚抓到的新结果不被共享回填覆盖。"""
    key = weather._cache_key("杭州")
    new_payload = {"city": "杭州", "current": {"temperature": 30}, "daily": []}
    old_payload = {"city": "杭州", "current": {"temperature": 10}, "daily": []}
    weather.clear_weather_cache()
    with weather._cache_lock:
        weather._cache[key] = (time.monotonic(), new_payload)
    assert weather._fill_from_shared(key, old_payload, 500) is new_payload
    assert weather._cache[key][1] is new_payload
    print("✓ 共享回填不覆盖更新的本地结果")


def test_shared_fill_keeps_negative_stamp_on_fresh_boot(monkeypatch):
    """主机刚启动（monotonic 小于共享条目年龄）时负锚点必须保留，不能钳 0。

    钳 0 会把已接近 10 分钟的共享条目当成「刚抓取」：进程内新鲜度窗口
    与 stale 兜底窗口都被拉长最多一个 CACHE_TTL。
    """
    client = _FakeSharedRedis()
    payload = {"city": "杭州", "current": {"temperature": 20}, "daily": []}
    key = weather._cache_key("杭州")
    client.data[f"aligo:weather:{key}"] = _shared_envelope(
        payload, fetched_at=time.time() - 500
    )
    monkeypatch.setattr(weather, "_shared_cache_client", lambda: client)
    weather.clear_weather_cache()
    monkeypatch.setattr(weather.time, "monotonic", lambda: 100.0)
    assert weather._cache_hit(key) == payload
    stamp = weather._cache[key][0]
    # 锚点 = monotonic - 真实年龄：约 -400（墙钟与抓取时刻之间有毫秒级误差）
    assert -401.0 <= stamp <= -399.0, "锚点必须保留负值（真实抓取时刻）"
    # 150 秒后真实总龄约 650 秒（> 10 分钟）：进程内条目必须按过期未命中
    monkeypatch.setattr(weather.time, "monotonic", lambda: 250.0)
    monkeypatch.setattr(weather, "_shared_cache_get", lambda name: None)
    assert weather._cache_hit(key) is None, "总龄超过 CACHE_TTL 的条目不能继续命中"
    print("✓ 刚启动的主机回填共享条目保留负锚点（窗口不翻倍）")


@pytest.fixture(autouse=True)
def _isolate_weather_cache(monkeypatch):
    """天气缓存是进程级全局状态，避免成功 / 失败结果在用例之间泄漏。

    共享缓存（配置 REDIS_URL 时）是进程外状态：测试统一隔离为「无
    Redis」，否则用例会读写真实 Redis 并互相污染（clear_weather_cache
    只清进程内表，不清共享键）。共享缓存的成功 / 失败会改短期记忆的
    Redis 可用性标记，夹具一并复位。
    """
    from context import short_term_memory as short_term_module
    from context import storage as storage_module

    weather.clear_weather_cache()
    short_term_module.reset_redis_degraded()
    monkeypatch.setattr(
        storage_module, "get_shared_redis_client", lambda *a, **k: None
    )
    yield
    weather.clear_weather_cache()
    short_term_module.reset_redis_degraded()


def _geo(city="北京"):
    return {
        "results": [
            {
                "name": city,
                "latitude": 39.9,
                "longitude": 116.4,
                "timezone": "Asia/Shanghai",
                # 省级行政中心 + 人口过百万：默认视为可信结果，不触发
                # 「裸名 / X市」变体补查（该行为由专门的变体用例覆盖）
                "population": 20000000,
                "feature_code": "PPLC",
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


def test_leader_recheck_uses_result_written_by_other_thread(monkeypatch):
    """登记在途后、请求上游前，其它线程刚好写入了缓存：直接复用并释放在途登记。"""
    cache_hits = {"n": 0}
    cached_value = {"city": "北京", "current": {"description": "晴"}}

    def fake_cache_hit(key):
        cache_hits["n"] += 1
        return None if cache_hits["n"] == 1 else cached_value

    monkeypatch.setattr(weather, "_cache_hit", fake_cache_hit)
    monkeypatch.setattr(weather, "_failure_hit", lambda key: None)
    events = []

    def fake_claim(key):
        event = threading.Event()
        events.append(event)
        return True, event

    monkeypatch.setattr(weather, "_claim_fetch", fake_claim)

    def boom(*args, **kwargs):
        raise AssertionError("命中缓存后不应请求上游")

    monkeypatch.setattr(weather, "_fetch_weather_uncached", boom)
    result = weather.fetch_weather("北京")
    assert result is cached_value
    assert events[0].is_set(), "返回前应释放在途登记"


def test_leader_recheck_raises_failure_written_by_other_thread(monkeypatch):
    """登记在途后别的线程记录了失败：直接抛出并释放在途登记。"""
    failures = {"n": 0}

    def fake_failure(key):
        failures["n"] += 1
        return None if failures["n"] == 1 else "天气接口暂时不可用：boom"

    monkeypatch.setattr(weather, "_cache_hit", lambda key: None)
    monkeypatch.setattr(weather, "_failure_hit", fake_failure)
    events = []

    def fake_claim(key):
        event = threading.Event()
        events.append(event)
        return True, event

    monkeypatch.setattr(weather, "_claim_fetch", fake_claim)
    with pytest.raises(weather.WeatherError, match="暂时不可用"):
        weather.fetch_weather("北京")
    assert events[0].is_set()


def test_follower_uses_cached_result_or_failure(monkeypatch):
    """等待领先线程后：缓存命中直接复用；失败缓存则抛出。"""
    event = threading.Event()
    event.set()
    monkeypatch.setattr(weather, "_claim_fetch", lambda key: (False, event))

    cache_hits = {"n": 0}

    def fake_cache(key):
        cache_hits["n"] += 1
        return None if cache_hits["n"] == 1 else {"city": "北京"}

    monkeypatch.setattr(weather, "_cache_hit", fake_cache)
    monkeypatch.setattr(weather, "_failure_hit", lambda key: None)
    assert weather.fetch_weather("北京")["city"] == "北京"

    failure_hits = {"n": 0}

    def fake_failure(key):
        failure_hits["n"] += 1
        return None if failure_hits["n"] == 1 else "boom"

    monkeypatch.setattr(weather, "_cache_hit", lambda key: None)
    monkeypatch.setattr(weather, "_failure_hit", fake_failure)
    with pytest.raises(weather.WeatherError, match="boom"):
        weather.fetch_weather("北京")


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
    # 裸名零结果后会补查「X市」变体：首轮共 2 次地理编码请求
    assert len(calls) == 2

    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 2, "失败结果在 TTL 内不应重复请求上游"
    print("✓ 失败结果短时缓存，避免重复请求上游")


def test_busy_gate_does_not_cache_failure(monkeypatch):
    """上游并发闸门繁忙不能写 60 秒失败缓存（名额恢复后应立即可用）。"""
    class _BusySlots:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(weather, "_upstream_slots", _BusySlots())
    with pytest.raises(weather.WeatherError) as info:
        weather.fetch_weather("北京", timeout=0.01)
    assert "繁忙" in str(info.value)
    assert weather._failure_hit(weather._cache_key("北京")) is None

    class _OpenSlots:
        def acquire(self, timeout=None):
            return True

        def release(self):
            pass

    monkeypatch.setattr(weather, "_upstream_slots", _OpenSlots())
    monkeypatch.setattr(
        weather,
        "_fetch_weather_uncached",
        lambda name, key, timeout: {"city": name, "source": "test"},
    )
    assert weather.fetch_weather("北京", timeout=0.01)["city"] == "北京"
    print("✓ 闸门繁忙不写失败缓存")


def test_follower_wait_exhaustion_not_cached(monkeypatch):
    """单飞等待轮次耗尽不是上游故障：不写失败缓存。"""
    class _Event:
        def wait(self, timeout=None):
            return False

        def set(self):
            pass

    monkeypatch.setattr(
        weather, "_claim_fetch", lambda name: (False, _Event())
    )
    with pytest.raises(weather.WeatherError) as info:
        weather.fetch_weather("北京", timeout=0.01)
    assert "繁忙" in str(info.value)
    assert weather._failure_hit(weather._cache_key("北京")) is None
    print("✓ 单飞等待耗尽不写失败缓存")


def test_failure_cache_expires(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        return _Response({"results": []})

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 2

    # 直接把失败条目的时间戳拨到 TTL 之外（Windows 上 sleep 精度不足 15.6ms）
    at, message = weather._failures["不存在"]
    weather._failures["不存在"] = (at - weather.FAILURE_TTL_SEC - 1, message)
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("不存在")
    assert len(calls) == 4, "失败缓存过期后应重新请求上游"
    print("✓ 失败缓存过期后重试上游")


def test_success_clears_cached_failure(monkeypatch):
    recovered = {"ok": False}

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(_geo("北京") if recovered["ok"] else {"results": []})
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    weather._failures.clear()  # 模拟缓存过期后上游恢复
    recovered["ok"] = True
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


def test_stale_fallback_does_not_refresh_cache_timestamp(monkeypatch):
    """降级沿用旧数据不能刷新成功缓存的时间戳：1 小时窗口只锚定最近一次成功。"""

    def ok_get(url, params=None, timeout=None, headers=None):
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", ok_get)
    weather.clear_weather_cache()
    weather.fetch_weather("北京")

    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    shifted = at - weather.CACHE_TTL_SEC - 1
    weather._cache[key] = (shifted, result)

    def fail_get(url, params=None, timeout=None, headers=None):
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", fail_get)
    stale = weather.fetch_weather("北京")
    assert stale["stale"] is True
    assert weather._cache[key][0] == shifted, "降级回写不能刷新成功缓存的时间戳"

    # 时间继续流逝：旧数据超出 1 小时窗口后必须报错（不能被反复回写续命）
    weather._degraded.clear()
    weather._cache[key] = (time.monotonic() - weather.STALE_TTL_SEC - 1, result)
    with pytest.raises(weather.WeatherError, match="天气接口暂时不可用"):
        weather.fetch_weather("北京")
    print("✓ 反复降级不会滑动 stale 窗口")


def test_degraded_reuse_window_and_expiry(monkeypatch):
    """降级结果 60 秒内直接复用（不再打上游），过期后重新尝试。"""
    times = {"now": 1000.0}
    monkeypatch.setattr(weather.time, "monotonic", lambda: times["now"])

    calls = {"n": 0}

    def ok_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", ok_get)
    weather.clear_weather_cache()
    fresh = weather.fetch_weather("北京")
    assert "stale" not in fresh
    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.CACHE_TTL_SEC - 1, result)
    ok_calls = calls["n"]

    def fail_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", fail_get)
    stale = weather.fetch_weather("北京")
    assert stale["stale"] is True
    failures = calls["n"]
    assert failures > ok_calls

    # 60 秒复用窗口内：直接返回上次降级结果，不再请求上游
    times["now"] += 30
    again = weather.fetch_weather("北京")
    assert again["stale"] is True
    assert calls["n"] == failures, "复用窗口内不应重打已故障的上游"

    # 窗口过期：重新尝试上游（仍失败 → 再次降级，失败计数 +1）
    times["now"] += 31
    weather.fetch_weather("北京")
    assert calls["n"] == failures + 1
    print("✓ 降级结果复用窗口与过期重试")


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
    known = {"ok": True}

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(_geo("北京") if known["ok"] else {"results": []})
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    weather.fetch_weather("北京")

    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.CACHE_TTL_SEC - 1, result)

    known["ok"] = False
    with pytest.raises(weather.WeatherError, match="未找到城市"):
        weather.fetch_weather("北京")
    print("✓ 城市不存在时不使用旧缓存掩盖错误")


def test_stale_fallback_merges_concurrent_requests(monkeypatch):
    """上游故障沿用 stale 数据时并发等待者也复用同一份结果（单飞不失效）。"""

    def ok_get(url, params=None, timeout=None, headers=None):
        return _Response(_geo("北京") if "geocoding" in url else _forecast())

    monkeypatch.setattr(weather.requests, "get", ok_get)
    weather.clear_weather_cache()
    weather.fetch_weather("北京")
    key = weather._cache_key("北京")
    at, result = weather._cache[key]
    weather._cache[key] = (at - weather.CACHE_TTL_SEC - 1, result)

    calls = []
    calls_lock = threading.Lock()

    def fail_get(url, params=None, timeout=None, headers=None):
        with calls_lock:
            calls.append(url)
        time.sleep(0.05)
        raise weather.requests.ConnectionError("network down")

    monkeypatch.setattr(weather.requests, "get", fail_get)
    barrier = threading.Barrier(6)

    def worker(_):
        barrier.wait(timeout=10)
        return weather.fetch_weather("北京")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert all(item.get("stale") is True for item in results)
    assert len(calls) == 1, f"stale 降级也应单飞，实际请求上游 {len(calls)} 次"
    # 后续请求直接复用回写的缓存，不再重打故障上游
    weather.fetch_weather("北京")
    assert len(calls) == 1
    print("✓ stale 降级时并发请求合并为一次上游失败")


def test_weather_codes_include_freezing_drizzle():
    """Open-Meteo 的 56/57（冻毛毛雨）不能显示成「未知」。"""
    assert weather._describe(56) == "冻毛毛雨"
    assert weather._describe(57) == "强冻毛毛雨"
    print("✓ 天气码 56/57 有中文描述")


def test_follower_reclaims_single_flight_after_timeout(monkeypatch):
    """等待超时后：先重新登记单飞，抢到名额才发起新一轮上游请求。"""
    claims = {"n": 0}
    events = []

    def fake_claim(key):
        claims["n"] += 1
        event = threading.Event()
        events.append(event)
        if claims["n"] == 1:
            event.set()  # 第一次等待立即超时返回
            return False, event
        return True, event

    monkeypatch.setattr(weather, "_claim_fetch", fake_claim)
    monkeypatch.setattr(weather, "_cache_hit", lambda key: None)
    monkeypatch.setattr(weather, "_failure_hit", lambda key: None)
    monkeypatch.setattr(weather, "_degraded_hit", lambda key: None)
    fetched = {"n": 0}

    def fake_fetch(name, key, timeout):
        fetched["n"] += 1
        return {"city": name, "current": {}, "daily": []}

    monkeypatch.setattr(weather, "_fetch_weather_uncached", fake_fetch)
    monkeypatch.setattr(weather, "_finish_fetch", lambda key, event: event.set())
    result = weather.fetch_weather("并发城", timeout=0.01)
    assert result["city"] == "并发城"
    assert claims["n"] == 2, "等待超时后必须重新登记单飞"
    assert fetched["n"] == 1
    assert events[-1].is_set(), "新一轮结束要释放登记"
    print("✓ 等待超时后重新登记单飞（不放大上游请求）")


def test_helpers_tolerate_bad_shapes(monkeypatch):
    """_describe 非数字 / _get_json 非对象 / _parse_weather 非对象都按异常数据处理。"""
    assert weather._describe(None) == "未知"
    assert weather._describe("abc") == "未知"

    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: _Response([1, 2]))
    with pytest.raises(weather.WeatherError):
        weather._get_json("http://example.invalid", {}, 1.0)

    with pytest.raises(weather.WeatherError):
        weather._parse_weather("北京", {"name": "北京"}, ["不是对象"])
    print("✓ 天气辅助函数对非法形状的容错")


def _patch_reclaim_env(monkeypatch, claim_sequence, cache_values, failure_values,
                      degraded_values):
    """按调用顺序返回预设值的单飞辅助桩（覆盖「等待超时后再次登记」路径）。"""
    state = {"claims": 0, "cache": 0, "failure": 0, "degraded": 0}
    finished = []

    def fake_claim(key):
        index = state["claims"]
        state["claims"] += 1
        event = threading.Event()
        event.set()
        return (claim_sequence[index] if index < len(claim_sequence) else False, event)

    def _next(kind, values):
        def fake(key):
            index = state[kind]
            state[kind] += 1
            return values[index] if index < len(values) else values[-1]
        return fake

    monkeypatch.setattr(weather, "_claim_fetch", fake_claim)
    monkeypatch.setattr(weather, "_cache_hit", _next("cache", cache_values))
    monkeypatch.setattr(weather, "_failure_hit", _next("failure", failure_values))
    monkeypatch.setattr(weather, "_degraded_hit", _next("degraded", degraded_values))
    monkeypatch.setattr(
        weather, "_finish_fetch", lambda key, event: finished.append(key)
    )

    def _no_upstream(*args):
        raise AssertionError("命中缓存 / 失败 / 降级时不能再打上游")

    monkeypatch.setattr(weather, "_fetch_weather_uncached", _no_upstream)
    return state, finished


def test_reclaim_leader_reuses_cached_result(monkeypatch):
    """等待超时后再次抢到单飞：其它线程已写入缓存时直接复用。"""
    cached = {"temperature": 20, "source": "cache"}
    state, finished = _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False, True],
        cache_values=[None, None, cached],
        failure_values=[None],
        degraded_values=[None],
    )
    assert weather.fetch_weather("北京", timeout=0.01) is cached
    assert state["claims"] == 2, "第二次必须重新登记单飞"
    assert finished, "复用缓存后要释放单飞名额"
    print("✓ 再抢占比：复用缓存结果")


def test_reclaim_leader_raises_failure_written_by_others(monkeypatch):
    """再抢到单飞后发现其它线程已登记失败：直接抛同一失败，不打上游。"""
    state, finished = _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False, True],
        cache_values=[None],
        failure_values=[None, None, "天气接口暂时不可用"],
        degraded_values=[None],
    )
    with pytest.raises(weather.WeatherError, match="天气接口暂时不可用"):
        weather.fetch_weather("上海", timeout=0.01)
    assert state["claims"] == 2 and finished
    print("✓ 再抢占比：复用其它线程登记的失败")


def test_second_reclaim_follower_reuses_failure(monkeypatch):
    """第二次仍等待：超时后其它线程已登记失败 → 同样直接抛出。"""
    _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False, False],
        cache_values=[None],
        failure_values=[None, None, "上游故障"],
        degraded_values=[None],
    )
    with pytest.raises(weather.WeatherError, match="上游故障"):
        weather.fetch_weather("深圳", timeout=0.01)
    print("✓ 二次等待：复用失败结果")


def test_fetch_uncached_wraps_missing_location_fields(monkeypatch):
    """地理编码结果缺少经纬度：KeyError 包装为 WeatherError（不再裸抛）。"""
    incomplete = {"results": [{"name": "北京", "timezone": "Asia/Shanghai"}]}
    monkeypatch.setattr(
        weather.requests, "get", lambda *a, **k: _Response(incomplete)
    )
    with pytest.raises(weather.WeatherError, match="格式异常"):
        weather.fetch_weather("北京", timeout=0.5)
    print("✓ 缺少经纬度包装为 WeatherError")


def test_parse_weather_tolerates_none_daily_entries():
    """daily 列表里的 None 条目被跳过而不是抛异常（宽容形状）。"""
    data = {
        "current": {
            "temperature_2m": 10,
            "relative_humidity_2m": 40,
            "weather_code": 0,
        },
        "daily": {
            "time": ["2026-12-01"],
            "weather_code": [None],
            "temperature_2m_min": [None],
            "temperature_2m_max": [None],
        },
    }
    result = weather._parse_weather("北京", {"name": "北京"}, data)
    assert isinstance(result, dict) and "daily" in result
    print("✓ daily 非法条目被宽容跳过")


def test_second_wait_follower_reuses_cached(monkeypatch):
    """第二次仍等待：超时后缓存已被写入 → 直接复用缓存结果。"""
    cached = {"temperature": 9}
    _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False, False],
        cache_values=[None, None, cached],
        failure_values=[None],
        degraded_values=[None],
    )
    assert weather.fetch_weather("重庆", timeout=0.01) is cached
    print("✓ 二次等待：复用缓存结果")


def test_failure_cache_evicts_oldest_when_full(monkeypatch):
    """失败缓存满后淘汰最早一条（新 key 仍能登记）。"""
    monkeypatch.setattr(weather, "_CACHE_MAX_ENTRIES", 2)
    weather.clear_weather_cache()
    weather._store_failure("k1", "err-1")
    weather._store_failure("k2", "err-2")
    weather._store_failure("k3", "err-3")
    assert weather._failure_hit("k1") is None
    assert weather._failure_hit("k2") == "err-2"
    assert weather._failure_hit("k3") == "err-3"
    print("✓ 失败缓存满时淘汰最早条目")


def test_second_reclaim_follower_reuses_degraded(monkeypatch):
    """第二次仍等待：领先线程已降级（沿用旧数据）时复用同一份结果。"""
    degraded = {"temperature": 5, "stale": True}
    _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False, False],
        cache_values=[None],
        failure_values=[None],
        degraded_values=[None, None, degraded],
    )
    assert weather.fetch_weather("广州", timeout=0.01) is degraded
    print("✓ 二次等待：复用降级旧数据")


def test_follower_rounds_exhausted_reports_busy_without_upstream(monkeypatch):
    """等待轮次用尽仍无结果：报「繁忙」但不写失败缓存，不能各自打上游。"""
    stored = []
    state, _finished = _patch_reclaim_env(
        monkeypatch,
        claim_sequence=[False] * 10,
        cache_values=[None],
        failure_values=[None],
        degraded_values=[None],
    )
    monkeypatch.setattr(
        weather, "_store_failure", lambda key, msg: stored.append(msg)
    )
    with pytest.raises(weather.WeatherError) as excinfo:
        weather.fetch_weather("北京", timeout=0.01)
    assert "繁忙" in str(excinfo.value)
    assert state["claims"] == weather._FOLLOWER_WAIT_ROUNDS + 1
    assert stored == [], "等待耗尽不是上游故障，不能登记 60 秒失败缓存"
    print("✓ 等待轮次用尽报繁忙，不擅自打上游、不写失败缓存")


def test_non_finite_upstream_values_raise_weather_error(monkeypatch):
    """NaN / 1e400 等非有限数值不能进入缓存（JSON 序列化会 500）。"""

    def fake_get(url, params=None, timeout=None, headers=None):
        if "geocoding" in url:
            return _Response(_geo())
        data = _forecast()
        data["current"]["temperature_2m"] = float("nan")
        return _Response(data)

    monkeypatch.setattr(weather.requests, "get", fake_get)
    with pytest.raises(weather.WeatherError):
        weather.fetch_weather("北京")
    assert weather._cache_hit(weather._cache_key("北京")) is None
    print("✓ 非有限上游数值被拒绝，不进入缓存")


def test_describe_tolerates_non_finite_codes():
    assert weather._describe(float("inf")) == "未知"
    assert weather._describe(float("nan")) == "未知"
    print("✓ 非有限天气码不再抛 OverflowError")


def test_upstream_window_limits_rotating_cities(monkeypatch):
    """全进程上游窗口用尽时拒绝请求：轮换城市名不能绕过 /api/weather 的 IP 桶。"""
    now = time.monotonic()
    for _ in range(weather._UPSTREAM_MAX_PER_MINUTE):
        weather._upstream_calls.append(now)
    calls = []
    monkeypatch.setattr(
        weather.requests, "get", lambda *a, **k: calls.append(1) or _Response(_geo())
    )
    with pytest.raises(weather.UpstreamQuotaError) as excinfo:
        weather.fetch_weather("北京")
    assert "频繁" in str(excinfo.value)
    assert 1 <= excinfo.value.retry_after <= 60, (
        "额度用尽属本地限流：必须带真实恢复等待，供端点回 429"
    )
    assert calls == [], "额度用尽后不能再请求上游"
    # 失败缓存已登记：紧随其后的请求快速失败
    with pytest.raises(weather.WeatherError):
        weather.fetch_weather("北京")
    assert calls == []
    print("✓ 全进程上游窗口限制生效")


def test_geocode_prefers_trusted_alias_for_foreign_city(monkeypatch):
    """中文境外城市：zh 索引只有同名小城（伦敦→加拿大 London）时以 en 为准。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        calls.append((url, params))
        if "geocoding" in url:
            if params["language"] == "zh":
                return _Response(
                    {
                        "results": [
                            {
                                "name": "伦敦",
                                "latitude": 42.98,
                                "longitude": -81.25,
                                "timezone": "America/Toronto",
                                "population": 422324,
                                "feature_code": "PPL",
                            }
                        ]
                    }
                )
            return _Response(
                {
                    "results": [
                        {
                            "name": "London",
                            "latitude": 51.5,
                            "longitude": -0.12,
                            "timezone": "Europe/London",
                            "population": 8961989,
                            "feature_code": "PPLC",
                        }
                    ]
                }
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    data = weather.fetch_weather("伦敦")
    assert data["timezone"] == "Europe/London"
    assert data["city"] == "London"
    languages = [params["language"] for url, params in calls if "geocoding" in url]
    assert languages == ["zh", "en"], calls
    assert calls[-1][1]["latitude"] == 51.5
    print("✓ 境外中文城市按英文别名改查")


def test_geocode_skips_alias_for_trusted_chinese_city(monkeypatch):
    """中文国内城市 zh 索引已可信：不做英文别名的二次请求。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        calls.append((url, params))
        if "geocoding" in url:
            return _Response(
                {
                    "results": [
                        {
                            "name": params["name"],
                            "latitude": 30.2,
                            "longitude": 120.1,
                            "timezone": "Asia/Shanghai",
                            "population": 7500000,
                            "feature_code": "PPLA",
                        }
                    ]
                }
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    data = weather.fetch_weather("杭州")
    assert data["timezone"] == "Asia/Shanghai"
    languages = [params["language"] for url, params in calls if "geocoding" in url]
    assert languages == ["zh"], calls
    print("✓ 可信中文城市不做别名二次请求")


def test_quota_rejection_serves_stale_without_failure_cache(monkeypatch):
    """全局额度用尽：有旧数据就降级返回，且不按城市钉死失败缓存。"""
    key = weather._cache_key("杭州")
    weather._cache[key] = (
        time.monotonic() - 1200,
        {"city": "杭州", "current": {"temperature": 20}},
    )
    now = time.monotonic()
    for _ in range(weather._UPSTREAM_MAX_PER_MINUTE):
        weather._upstream_calls.append(now)
    calls = []
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda *a, **k: calls.append(1) or _Response(_geo()),
    )
    data = weather.fetch_weather("杭州")
    assert data["stale"] is True
    assert calls == [], "额度用尽不能请求上游"
    assert "杭州" not in weather._failures, "全局额度错误不能按城市落失败缓存"
    print("✓ 额度用尽沿用旧数据且不钉死城市")


def test_upstream_concurrency_gate_reports_busy(monkeypatch):
    """在途上游请求达到上限时按繁忙失败，不再并发打 Open-Meteo。"""
    gate = threading.BoundedSemaphore(1)
    gate.acquire()
    monkeypatch.setattr(weather, "_upstream_slots", gate)
    try:
        with pytest.raises(weather.WeatherError, match="繁忙"):
            weather._fetch_weather_uncached("北京", "北京", 0.05)
    finally:
        gate.release()
    print("✓ 上游并发闸门无可用名额时报繁忙")


def test_peek_cached_weather_returns_fresh_entry():
    """端点限流前的缓存探测：只返回新鲜成功结果。"""
    weather.clear_weather_cache()
    try:
        weather._cache["北京"] = (time.monotonic(), {"city": "北京"})
        assert weather.peek_cached_weather(" 北京 ") == {"city": "北京"}
        assert weather.peek_cached_weather("   ") is None
        weather._cache["北京"] = (
            time.monotonic() - weather.CACHE_TTL_SEC - 1,
            {"city": "北京"},
        )
        assert weather.peek_cached_weather("北京") is None
    finally:
        weather.clear_weather_cache()
    print("✓ peek_cached_weather 只返回新鲜缓存")

def test_city_name_variant_guards(monkeypatch):
    """裸名 / X市 变体：单字与「沙市」这类两字市名不补查。"""
    assert weather._city_name_variant("青岛") == "青岛市"
    assert weather._city_name_variant("青岛市") == "青岛"
    assert weather._city_name_variant("沙市") == ""
    assert weather._city_name_variant("中") == ""
    assert weather._city_name_variant("") == ""
    print("✓ 城市名变体生成边界正确")


def test_geocode_prefers_trusted_city_variant_for_bare_name(monkeypatch):
    """裸名只命中同名村庄时补查「X市」并在可信候选里选行政中心。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        if "geocoding" in url:
            calls.append((params.get("name"), params.get("language")))
            if params.get("name") == "青岛":
                # 同名村庄：无人口、非行政中心 → 不可信
                return _Response(
                    {
                        "results": [
                            {
                                "name": "青岛",
                                "latitude": 39.33,
                                "longitude": 121.73,
                                "timezone": "Asia/Shanghai",
                                "feature_code": "PPL",
                            }
                        ]
                    }
                )
            return _Response(
                {
                    "results": [
                        {
                            "name": "青岛市",
                            "latitude": 36.07,
                            "longitude": 120.38,
                            "timezone": "Asia/Shanghai",
                            "population": 9498000,
                            "feature_code": "PPLA2",
                        }
                    ]
                }
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    data = weather.fetch_weather("青岛")
    assert data["city"] == "青岛市"
    assert calls == [("青岛", "zh"), ("青岛市", "zh")]
    print("✓ 裸名命中同名村庄时补查「X市」可信候选")


def test_geocode_variant_strips_city_suffix(monkeypatch):
    """「杭州市」在 zh 索引零结果时补查裸名「杭州」。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        if "geocoding" in url:
            calls.append(params.get("name"))
            if params.get("name") == "杭州":
                return _Response(_geo("杭州"))
            return _Response({"results": []})
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    data = weather.fetch_weather("杭州市")
    assert data["city"] == "杭州"
    assert calls == ["杭州市", "杭州"]
    print("✓ 「X市」零结果时回退裸名补查")


def test_geocode_keeps_untrusted_candidates_when_variant_empty(monkeypatch):
    """两个变体都只有不可信候选时保留原名结果（小镇 / 景区仍可查询）。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        if "geocoding" in url:
            calls.append(params.get("name"))
            if params.get("name") == "某某镇":
                return _Response(
                    {
                        "results": [
                            {
                                "name": "某某镇",
                                "latitude": 30.0,
                                "longitude": 120.0,
                                "feature_code": "PPL",
                            }
                        ]
                    }
                )
            return _Response({"results": []})
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    data = weather.fetch_weather("某某镇")
    assert data["city"] == "某某镇"
    assert calls == ["某某镇", "某某镇市"]
    print("✓ 变体补查失败时保留原名候选")


def test_washington_alias_resolves_to_district_of_columbia(monkeypatch):
    """「华盛顿」查 en 索引的 Washington D.C.，不能选中宾州同名小镇。"""
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        if "geocoding" in url:
            calls.append((params.get("name"), params.get("language")))
            if params.get("name") == "Washington D.C.":
                return _Response(
                    {
                        "results": [
                            {
                                "name": "Washington D.C.",
                                "latitude": 38.895,
                                "longitude": -77.036,
                                "timezone": "America/New_York",
                                "population": 689545,
                                "feature_code": "PPLC",
                            }
                        ]
                    }
                )
            return _Response(
                {
                    "results": [
                        {
                            "name": "Washington",
                            "latitude": 40.174,
                            "longitude": -80.246,
                            "timezone": "America/New_York",
                            "population": 13497,
                            "feature_code": "PPLA2",
                        }
                    ]
                }
            )
        return _Response(_forecast())

    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.clear_weather_cache()
    data = weather.fetch_weather("华盛顿")
    assert data["city"] == "Washington D.C."
    assert ("Washington D.C.", "en") in calls
    print("✓ 华盛顿解析到特区而非宾州同名小镇")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    for case in (
        test_stale_fallback_merges_concurrent_requests,
        test_fetch_weather_and_cache,
        test_forecast_uses_location_timezone,
        test_unknown_city_raises,
        test_request_failure_raises,
        test_http_error_maps_to_weather_error,
        test_malformed_payload_raises,
        test_cache_is_bounded_and_prunes_expired,
        test_concurrent_misses_share_upstream_request,
        test_leader_recheck_uses_result_written_by_other_thread,
        test_leader_recheck_raises_failure_written_by_other_thread,
        test_follower_uses_cached_result_or_failure,
        test_follower_reclaims_single_flight_after_timeout,
        test_cache_key_ignores_case_and_extra_space,
        test_failure_cache_prevents_repeat_requests,
        test_failure_cache_expires,
        test_success_clears_cached_failure,
        test_stale_cache_served_when_upstream_fails,
        test_stale_fallback_does_not_refresh_cache_timestamp,
        test_stale_cache_expires,
        test_unknown_city_does_not_use_stale_cache,
        test_helpers_tolerate_bad_shapes,
        test_reclaim_leader_reuses_cached_result,
        test_reclaim_leader_raises_failure_written_by_others,
        test_second_reclaim_follower_reuses_failure,
        test_second_reclaim_follower_reuses_degraded,
        test_second_wait_follower_reuses_cached,
        test_fetch_uncached_wraps_missing_location_fields,
        test_failure_cache_evicts_oldest_when_full,
        test_follower_rounds_exhausted_reports_busy_without_upstream,
        test_busy_gate_does_not_cache_failure,
        test_follower_wait_exhaustion_not_cached,
        test_non_finite_upstream_values_raise_weather_error,
        test_upstream_window_limits_rotating_cities,
        test_geocode_prefers_trusted_alias_for_foreign_city,
        test_geocode_skips_alias_for_trusted_chinese_city,
        test_city_name_variant_guards,
        test_geocode_prefers_trusted_city_variant_for_bare_name,
        test_geocode_variant_strips_city_suffix,
        test_geocode_keeps_untrusted_candidates_when_variant_empty,
        test_washington_alias_resolves_to_district_of_columbia,
        test_quota_rejection_serves_stale_without_failure_cache,
        test_upstream_concurrency_gate_reports_busy,
        test_degraded_reuse_window_and_expiry,
        test_shared_cache_hit_avoids_upstream,
        test_success_cache_write_is_shared,
        test_shared_cache_failure_enters_cooldown,
        test_shared_cache_ignores_stale_or_invalid_entry,
        test_shared_hit_anchors_age_to_fetch_time,
        test_shared_fill_keeps_negative_stamp_on_fresh_boot,
        test_shared_fill_respects_cache_limits,
        test_shared_cache_success_resets_redis_availability,
    ):
        mp = pytest.MonkeyPatch()
        try:
            # 直跑不执行 autouse fixture：手动隔离天气缓存与共享 Redis，
            # 并复位短期记忆的 Redis 可用性标记（成功 / 失败路径会改它）
            from context import short_term_memory as _st_module
            from context import storage as _storage_module

            mp.setattr(
                _storage_module,
                "get_shared_redis_client",
                lambda *a, **k: None,
            )
            _st_module.reset_redis_degraded()
            weather.clear_weather_cache()
            case(mp)
        finally:
            weather.clear_weather_cache()
            _st_module.reset_redis_degraded()
            mp.undo()
    test_empty_city_raises()
    test_parse_weather_tolerates_none_daily_entries()
    test_weather_codes_include_freezing_drizzle()
    test_describe_tolerates_non_finite_codes()
    test_peek_cached_weather_returns_fresh_entry()
    with pytest.MonkeyPatch.context() as _mp:
        from context import storage as _storage_module

        _mp.setattr(
            _storage_module, "get_shared_redis_client", lambda *a, **k: None
        )
        weather.clear_weather_cache()
        test_shared_backfill_keeps_newer_local_entry()
    print("\n全部通过")
