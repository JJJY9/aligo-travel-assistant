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
    print("✓ P50 / P95 使用有界近期样本，max 保留历史")


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
    """HTTP 方法由客户端指定（h11 接受任意 token），同样要有基数上限。"""
    metrics = RequestMetrics()
    for index in range(50):
        metrics.record(f"X{index}", "/api/health", 200, 1.0)
    snapshot = metrics.snapshot()
    assert len(snapshot["by_method"]) <= 20
    full = metrics._by_method  # noqa: SLF001 - 直接断言内部聚合
    assert len(full) <= 16 + 1
    assert full["__other__"] == 50 - 16
    print("✓ 方法基数有上限（超出归 __other__）")


def test_control_characters_are_sanitized():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/x\nfake-line", 200, 1.0)
    path = next(iter(metrics.snapshot()["by_path"]))
    assert "\n" not in path
    assert "?" in path
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


if __name__ == "__main__":
    test_record_and_snapshot()
    test_percentiles_use_bounded_recent_samples()
    test_path_cardinality_is_bounded()
    test_method_cardinality_is_bounded()
    test_control_characters_are_sanitized()
    test_prometheus_render()
    test_prometheus_omits_duration_stats_without_samples()
    test_uptime_uses_monotonic_clock()
    test_reset()
    print("\n全部通过")
