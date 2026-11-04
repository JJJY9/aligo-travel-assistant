"""
请求指标测试（无需 API Key、不访问网络）

运行：python tests/test_metrics.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.metrics import MAX_PATHS, RequestMetrics


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
    assert snapshot["uptime_sec"] >= 0
    print("✓ 请求计数 / 状态 / 方法 / 耗时快照")


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


def test_control_characters_are_sanitized():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/x\nfake-line", 200, 1.0)
    path = next(iter(metrics.snapshot()["by_path"]))
    assert "\n" not in path
    assert "?" in path
    print("✓ 指标路径清理控制字符")


def test_reset():
    metrics = RequestMetrics()
    metrics.record("GET", "/api/health", 200, 1.0)
    metrics.reset()
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 0
    assert snapshot["by_status"] == {}
    print("✓ 重置指标")


if __name__ == "__main__":
    test_record_and_snapshot()
    test_path_cardinality_is_bounded()
    test_control_characters_are_sanitized()
    test_reset()
    print("\n全部通过")
