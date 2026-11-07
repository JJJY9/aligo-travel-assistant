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
        # 用单调时钟计算 uptime：系统时间回拨 / 校时不会让指标倒退或变负
        self._started_at = time.monotonic()
        self._total = 0
        self._by_status: Counter[int] = Counter()
        self._by_method: Counter[str] = Counter()
        self._by_path: Counter[str] = Counter()
        self._duration_sum_ms = 0.0
        self._duration_max_ms = 0.0
        self._durations: deque[float] = deque(maxlen=MAX_DURATION_SAMPLES)
        # SSE 长连接的耗时不计入延迟分位（详见 record 的 streaming 参数）；
        # 另有独立计数记录「HTTP 200 但以 error 事件收尾」的流式回合
        self._sse_errors = 0

    def record(
        self,
        method: str,
        path: str,
        status: int,
        elapsed_ms: float,
        streaming: bool = False,
    ) -> None:
        """记录一次请求。

        ``streaming=True``（SSE 长连接）时只计请求数与状态，不计入耗时
        统计——流式会话的整段持续时间会把延迟分位完全支配。
        """
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
            if not streaming:
                self._duration_sum_ms += elapsed
                self._duration_max_ms = max(self._duration_max_ms, elapsed)
                self._durations.append(elapsed)

    def record_sse_error(self) -> None:
        """记录一次以 error 事件收尾的流式回合（HTTP 已发出 200）。

        访问日志与按状态码的指标只能看到 200，对话错误率需要一个独立
        口径，否则永远为 0。
        """
        with self._lock:
            self._sse_errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total
            samples = sorted(self._durations)
            return {
                "uptime_sec": round(time.monotonic() - self._started_at, 1),
                "requests_total": total,
                "by_status": {str(key): value for key, value in sorted(self._by_status.items())},
                "by_method": dict(self._by_method.most_common(20)),
                "by_path": dict(self._by_path.most_common(20)),
                "avg_duration_ms": round(self._duration_sum_ms / total, 2) if total else 0.0,
                "max_duration_ms": round(self._duration_max_ms, 2),
                "p50_duration_ms": round(_percentile(samples, 0.50), 2),
                "p95_duration_ms": round(_percentile(samples, 0.95), 2),
                "duration_samples": len(samples),
                "sse_errors": self._sse_errors,
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
            sse_errors = self._sse_errors
            uptime = time.monotonic() - self._started_at

        lines = [
            "# HELP aligo_requests_total Total HTTP requests recorded by this process.",
            "# TYPE aligo_requests_total counter",
            f"aligo_requests_total {total}",
            "# HELP aligo_requests_by_status_total Requests grouped by HTTP status.",
            "# TYPE aligo_requests_by_status_total counter",
            "# HELP aligo_sse_errors_total Streaming turns that ended with an error event.",
            "# TYPE aligo_sse_errors_total counter",
            f"aligo_sse_errors_total {sse_errors}",
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
            ]
        )
        # 没有样本时省略数值行：输出 0 会被误读成“测得 0 秒”
        if sample_count:
            lines.extend(
                [
                    f'aligo_request_duration_seconds{{quantile="avg"}} {avg_seconds:.6f}',
                    f'aligo_request_duration_seconds{{quantile="max"}} {max_seconds:.6f}',
                    f'aligo_request_duration_seconds{{quantile="p50"}} {p50_seconds:.6f}',
                    f'aligo_request_duration_seconds{{quantile="p95"}} {p95_seconds:.6f}',
                ]
            )
        lines.extend(
            [
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
            self._started_at = time.monotonic()
            self._total = 0
            self._by_status.clear()
            self._by_method.clear()
            self._by_path.clear()
            self._duration_sum_ms = 0.0
            self._duration_max_ms = 0.0
            self._durations.clear()
            self._sse_errors = 0


REQUEST_METRICS = RequestMetrics()
