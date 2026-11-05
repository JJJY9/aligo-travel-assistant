"""进程内请求指标（无第三方依赖）。

中间件为每个请求记录计数与耗时；``GET /api/metrics`` 返回快照，便于本地观测
与压测。耗时 P50 / P95 基于最近 ``MAX_DURATION_SAMPLES`` 个请求（内存有界）。
多进程 / 多实例部署时各进程独立统计（见 README 已知限制）。
"""
import math
import re
import threading
import time
from collections import Counter, deque
from typing import Any

# 基数上限：路径可能包含会话 ID、方法名由客户端任意指定，超出后归入 __other__
MAX_PATHS = 100
MAX_METHODS = 16
# 耗时百分位只统计最近 N 个请求，内存有界；进程启动早期样本不足时按已有样本计算
MAX_DURATION_SAMPLES = 1000
_OTHER = "__other__"


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """最近秩法百分位：返回第 ceil(n * fraction) 个样本（1 起）。"""
    if not sorted_values:
        return 0.0
    rank = max(1, min(len(sorted_values), math.ceil(len(sorted_values) * fraction)))
    return sorted_values[rank - 1]


def _escape_label(value: str) -> str:
    """转义 Prometheus 标签值（反斜杠、双引号、换行）。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


class RequestMetrics:
    """线程安全的请求计数 / 耗时聚合。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._total = 0
        self._by_status: Counter[int] = Counter()
        self._by_method: Counter[str] = Counter()
        self._by_path: Counter[str] = Counter()
        self._duration_sum_ms = 0.0
        self._duration_max_ms = 0.0
        self._durations: deque[float] = deque(maxlen=MAX_DURATION_SAMPLES)

    def record(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        # 路径由 uvicorn percent-decode，可能含控制字符；指标 key 里清理掉
        safe_path = re.sub(r"[\x00-\x1f\x7f]", "?", str(path))[:200]
        elapsed = max(0.0, float(elapsed_ms))
        with self._lock:
            self._total += 1
            self._by_status[int(status)] += 1
            safe_method = str(method)[:16] or "UNKNOWN"
            if len(self._by_method) < MAX_METHODS or safe_method in self._by_method:
                self._by_method[safe_method] += 1
            else:
                self._by_method[_OTHER] += 1
            if len(self._by_path) < MAX_PATHS or safe_path in self._by_path:
                self._by_path[safe_path] += 1
            else:
                self._by_path[_OTHER] += 1
            self._duration_sum_ms += elapsed
            self._duration_max_ms = max(self._duration_max_ms, elapsed)
            self._durations.append(elapsed)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total
            samples = sorted(self._durations)
            return {
                "uptime_sec": round(time.time() - self._started_at, 1),
                "requests_total": total,
                "by_status": {str(key): value for key, value in sorted(self._by_status.items())},
                "by_method": dict(self._by_method.most_common(20)),
                "by_path": dict(self._by_path.most_common(20)),
                "avg_duration_ms": round(self._duration_sum_ms / total, 2) if total else 0.0,
                "max_duration_ms": round(self._duration_max_ms, 2),
                "p50_duration_ms": round(_percentile(samples, 0.50), 2),
                "p95_duration_ms": round(_percentile(samples, 0.95), 2),
                "duration_samples": len(samples),
            }

    def render_prometheus(self) -> str:
        """渲染 Prometheus 文本格式（每次调用是一份一致的快照）。"""
        with self._lock:
            total = self._total
            statuses = sorted(self._by_status.items())
            methods = self._by_method.most_common()
            paths = self._by_path.most_common()
            avg_seconds = (
                self._duration_sum_ms / total / 1000 if total else 0.0
            )
            max_seconds = self._duration_max_ms / 1000
            samples = sorted(self._durations)
            p50_seconds = _percentile(samples, 0.50) / 1000
            p95_seconds = _percentile(samples, 0.95) / 1000
            sample_count = len(samples)
            uptime = time.time() - self._started_at

        lines = [
            "# HELP aligo_requests_total Total HTTP requests recorded by this process.",
            "# TYPE aligo_requests_total counter",
            f"aligo_requests_total {total}",
            "# HELP aligo_requests_by_status_total Requests grouped by HTTP status.",
            "# TYPE aligo_requests_by_status_total counter",
        ]
        for status, value in statuses:
            lines.append(
                f'aligo_requests_by_status_total{{status="{_escape_label(str(status))}"}} {value}'
            )
        lines.extend(
            [
                "# HELP aligo_requests_by_method_total Requests grouped by HTTP method.",
                "# TYPE aligo_requests_by_method_total counter",
            ]
        )
        for method, value in methods:
            lines.append(
                f'aligo_requests_by_method_total{{method="{_escape_label(method)}"}} {value}'
            )
        lines.extend(
            [
                "# HELP aligo_requests_by_path_total Requests grouped by path (top cardinality capped).",
                "# TYPE aligo_requests_by_path_total counter",
            ]
        )
        for path, value in paths:
            lines.append(
                f'aligo_requests_by_path_total{{path="{_escape_label(path)}"}} {value}'
            )
        lines.extend(
            [
                "# HELP aligo_request_duration_seconds Request duration statistics.",
                "# TYPE aligo_request_duration_seconds gauge",
                f"aligo_request_duration_seconds{{quantile=\"avg\"}} {avg_seconds:.6f}",
                f"aligo_request_duration_seconds{{quantile=\"max\"}} {max_seconds:.6f}",
                f"aligo_request_duration_seconds{{quantile=\"p50\"}} {p50_seconds:.6f}",
                f"aligo_request_duration_seconds{{quantile=\"p95\"}} {p95_seconds:.6f}",
                "# HELP aligo_request_duration_samples Number of durations used for percentiles.",
                "# TYPE aligo_request_duration_samples gauge",
                f"aligo_request_duration_samples {sample_count}",
                "# HELP aligo_uptime_seconds Process uptime in seconds.",
                "# TYPE aligo_uptime_seconds gauge",
                f"aligo_uptime_seconds {uptime:.1f}",
            ]
        )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空统计（测试或运维需要时使用）。"""
        with self._lock:
            self._started_at = time.time()
            self._total = 0
            self._by_status.clear()
            self._by_method.clear()
            self._by_path.clear()
            self._duration_sum_ms = 0.0
            self._duration_max_ms = 0.0
            self._durations.clear()


REQUEST_METRICS = RequestMetrics()
