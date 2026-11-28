"""
请求指标测试（无需 API Key、不访问网络）

运行：python tests/test_metrics.py
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.metrics import MAX_DURATION_SAMPLES, MAX_PATHS, RequestMetrics


def test_record_and_snapshot():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 12.0)
    metrics.record("POST", "/api/chat", 500, 30.0)
    metrics.record("GET", "/api/health", 200, 18.0)
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 3
    assert snapshot["by_status"] == {"200": 2, "500": 1}
    assert snapshot["by_method"] == {"GET": 2, "POST": 1}
    assert snapshot["by_path"]["/api/health"] == 2
    assert snapshot["avg_duration_ms"] == 20.0
    assert snapshot["max_duration_ms"] == 30.0
    assert snapshot["p50_duration_ms"] == 18.0
    assert snapshot["p95_duration_ms"] == 30.0
    assert snapshot["duration_samples"] == 3
    assert snapshot["uptime_sec"] >= 0
    print("✓ 请求计数 / 状态 / 方法 / 耗时快照")


def test_percentiles_use_bounded_recent_samples():
    """P50 / P95 只基于最近 N 个请求；max 保留全量历史。"""
    metrics = RequestMetrics()
    for _ in range(200):
        metrics.record("GET", "/api/slow", 200, 1000.0)
    for _ in range(MAX_DURATION_SAMPLES):
        metrics.record("GET", "/api/fast", 200, 1.0)
    snapshot = metrics.snapshot()
    assert snapshot["duration_samples"] == MAX_DURATION_SAMPLES
    assert snapshot["p50_duration_ms"] == 1.0
    assert snapshot["p95_duration_ms"] == 1.0, "早期慢请求应已滑出样本窗口"
    assert snapshot["max_duration_ms"] == 1000.0, "最大值仍保留全量历史"
    # 平均耗时为全生命周期口径（与百分位窗口不同），显式固化契约
    assert snapshot["avg_duration_ms"] == 167.5
    print("✓ P50 / P95 使用有界近期样本，max / avg 保留历史")


def test_average_duration_excludes_streaming_requests():
    """平均耗时只除以真正计时的请求数：SSE 只计请求数、不进分母。"""
    metrics = RequestMetrics()
    metrics.record("POST", "/api/chat/stream", 200, 0.0, streaming=True)
    metrics.record("GET", "/api/health", 200, 10.0)
    metrics.record("POST", "/api/chat/stream", 200, 0.0, streaming=True)
    metrics.record("GET", "/api/health", 200, 30.0)
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 4
    assert snapshot["avg_duration_ms"] == 20.0, "流式请求不能稀释平均耗时"
    prometheus = metrics.render_prometheus()
    assert (
        'aligo_request_duration_seconds{quantile="avg"} 0.020000' in prometheus
    ), "Prometheus 的 avg 同样只按计时请求计算"
    print("✓ 平均耗时不把 SSE 请求计入分母")


def test_count_only_skips_duration_samples():
    """Host 拒绝 / 未匹配 404 探测只计请求数，不进延迟样本窗口。"""
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 10.0)
    metrics.record("GET", "__host_rejected__", 400, 0.0, count_only=True)
    metrics.record("GET", "__unmatched__", 404, 0.1, count_only=True)
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 3
    assert snapshot["by_status"] == {"200": 1, "400": 1, "404": 1}
    assert snapshot["duration_samples"] == 1
    assert snapshot["p50_duration_ms"] == 10.0
    assert snapshot["p95_duration_ms"] == 10.0
    print("✓ 探测流量只计计数、不稀释延迟分位")


def test_path_cardinality_is_bounded():
    metrics = RequestMetrics()
    for index in range(MAX_PATHS + 50):
        metrics.record("GET", f"/api/sessions/s{index}", 200, 1.0)
    snapshot = metrics.snapshot()
    assert len(snapshot["by_path"]) <= 20, "快照只返回 Top 20 路径"
    # 计数仍然完整：其余路径归入 __other__
    full = metrics._by_path  # noqa: SLF001 - 直接断言内部聚合
    assert len(full) <= MAX_PATHS + 1
    assert full["__other__"] == 50
    print("✓ 高基数路径归入 __other__，聚合有界")


def test_method_cardinality_is_bounded():
    """HTTP 方法由客户端指定：非标准方法一律归 __other__，不占固定预算。"""
    metrics = RequestMetrics()
    for index in range(50):
        metrics.record(f"X{index}", "/api/health", 200, 1.0)
    metrics.record("GET", "/api/health", 200, 1.0)
    full = metrics.snapshot()["by_method"]
    assert full.get("__other__") == 50, full
    assert full.get("GET") == 1, "伪造方法不能把真实方法挤出统计"
    assert not any(key.startswith("X") for key in full), full
    assert len(full) <= 8
    print("✓ 非标准方法统一归 __other__（真实方法不受影响）")


def test_control_characters_are_sanitized():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/x\nfake-line", 200, 1.0)
    path = next(iter(metrics.snapshot()["by_path"]))
    assert "\n" not in path
    assert "?" in path
    # 清洗口径与访问日志 _safe_log_text 对齐：C1（NEL 会断行破坏
    # Prometheus 文本格式）与 U+2028/2029 同样替换
    metrics.record("GET", "/api/x\u0085y\u2028z", 200, 1.0)
    paths = [p for p in metrics.snapshot()["by_path"] if "x" in p]
    assert all("\u0085" not in p and "\u2028" not in p for p in paths), paths
    assert "/api/x?y?z" in paths, paths
    print("✓ 指标路径清理控制字符")


def test_prometheus_render():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 12.0)
    metrics.record("POST", "/api/chat", 500, 30.0)
    metrics.record("GET", '/api/x"y', 200, 18.0)
    text = metrics.render_prometheus()
    assert "aligo_requests_total 3" in text
    assert 'aligo_requests_by_status_total{status="200"} 2' in text
    assert 'aligo_requests_by_method_total{method="GET"} 2' in text
    assert 'aligo_requests_by_path_total{path="/api/x\\"y"} 1' in text
    assert 'aligo_request_duration_seconds{quantile="p95"} 0.030000' in text
    assert "aligo_uptime_seconds" in text
    print("✓ Prometheus 文本指标（标签转义 / 百分位）")


def test_prometheus_families_stay_grouped():
    """同一 metric family 的样本必须紧跟 HELP / TYPE（文本格式 0.0.4）。

    中间插入其它族时，解析器会把后半段样本当成新的 untyped 族。
    """
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 1.0)
    metrics.record("POST", "/api/chat", 500, 2.0)
    metrics.record("GET", "/api/health", 200, 1.0)
    lines = metrics.render_prometheus().splitlines()

    def family(line: str) -> str:
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            return line.split()[2]
        return line.split("{")[0].split()[0]

    seen: list[str] = []
    for line in lines:
        name = family(line)
        if seen and seen[-1] == name:
            continue
        assert name not in seen, f"metric family {name} 被拆成多段"
        seen.append(name)
    print("✓ Prometheus 指标族连续输出")


def test_prometheus_omits_duration_stats_without_samples():
    metrics = RequestMetrics()
    text = metrics.render_prometheus()
    assert "aligo_request_duration_samples 0" in text
    assert "aligo_request_duration_seconds{" not in text, "无样本时不应输出 0 耗时"
    print("✓ 无样本时不输出耗时统计")


def test_uptime_uses_monotonic_clock():
    metrics = RequestMetrics()
    metrics._started_at = time.monotonic() - 3.0  # noqa: SLF001
    uptime = metrics.snapshot()["uptime_sec"]
    assert 3.0 <= uptime < 60, "uptime 应基于单调时钟，墙钟回拨不影响"
    print("✓ uptime 基于单调时钟")


def test_reset():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 1.0)
    metrics.reset()
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 0
    assert snapshot["by_status"] == {}
    assert snapshot["duration_samples"] == 0
    assert snapshot["p50_duration_ms"] == 0.0
    assert snapshot["p95_duration_ms"] == 0.0
    print("✓ 重置指标")


def test_preference_cache_metrics():
    """偏好快照缓存命中 / 未命中 / 版本失效计数进入快照与 Prometheus。"""
    metrics = RequestMetrics()
    metrics.record_preference_cache(True)
    metrics.record_preference_cache(True)
    metrics.record_preference_cache(False)
    metrics.record_preference_cache(False, version_changed=True)
    assert metrics.snapshot()["preference_cache"] == {
        "hits": 2,
        "misses": 2,
        "version_invalidations": 1,
    }
    body = metrics.render_prometheus()
    assert "aligo_preference_cache_hits_total 2" in body
    assert "aligo_preference_cache_misses_total 2" in body
    assert "aligo_preference_cache_version_invalidations_total 1" in body
    metrics.reset()
    assert metrics.snapshot()["preference_cache"] == {
        "hits": 0,
        "misses": 0,
        "version_invalidations": 0,
    }
    print("✓ 偏好缓存命中率指标")


if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_record_and_snapshot()
    test_percentiles_use_bounded_recent_samples()
    test_path_cardinality_is_bounded()
    test_average_duration_excludes_streaming_requests()
    test_count_only_skips_duration_samples()
    test_method_cardinality_is_bounded()
    test_control_characters_are_sanitized()
    test_prometheus_render()
    test_prometheus_families_stay_grouped()
    test_prometheus_omits_duration_stats_without_samples()
    test_uptime_uses_monotonic_clock()
    test_reset()
    test_preference_cache_metrics()
    print("\n全部通过")
