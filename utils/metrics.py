"""进程内请求指标（无第三方依赖）。

中间件为 ``/api`` 路由请求与 Host 白名单拒绝的探测记录计数与状态；耗时
分布只统计真实经过路由的请求——Host 拒绝与未匹配路径的 404 不受限流、
可被扫描无限次触发，只计请求数，避免稀释 P50 / P95。首页、静态资源与
``/metrics`` 抓取本身不计入（``GET /api/metrics`` 返回快照），便于本地
观测与压测。耗时 P50 / P95 基于最近 ``MAX_DURATION_SAMPLES`` 个请求
（内存有界）。多进程 / 多实例部署时各进程独立统计（见 README 已知限制）。
"""
import math
import re
import threading
import time
from collections import Counter, deque
from typing import Any

# 基数上限：路径可能包含会话 ID，超出后归入 __other__；方法名只保留
# 标准 HTTP 方法（其余归 __other__，见 record——先到先占的固定预算会被
# 伪造方法名占满）
MAX_PATHS = 100
_STANDARD_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)
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
        # 真正计时过的请求数：平均耗时只能用这个做分母——流式（SSE）请求
        # 只加 requests_total，若用总请求数作分母，流式越多平均值越接近 0
        self._duration_count = 0
        # SSE 长连接的耗时不计入延迟分位（详见 record 的 streaming 参数）；
        # 另有独立计数记录「HTTP 200 但以 error 事件收尾」的流式回合
        self._sse_errors = 0
        # 偏好快照缓存（PostgreSQL 后端）：命中 / 未命中 / 因跨进程版本
        # 变化而失效，进程内累计（多实例各自统计）
        self._pref_cache_hits = 0
        self._pref_cache_misses = 0
        self._pref_cache_version_invalidations = 0

    def record(
        self,
        method: str,
        path: str,
        status: int,
        elapsed_ms: float,
        streaming: bool = False,
        count_only: bool = False,
    ) -> None:
        """记录一次请求。

        ``streaming=True``（SSE 长连接）时只计请求数与状态，不计入耗时
        统计——流式会话的整段持续时间会把延迟分位完全支配。
        ``count_only=True`` 用于 Host 拒绝探测与未匹配路径的 404：它们不受
        限流、可被无限次触发，计入最近样本窗口会把 P50 / P95 稀释到近 0。
        """
        # 路径由 uvicorn percent-decode，可能含控制字符；指标 key 里清理掉
        safe_path = re.sub(r"[\x00-\x1f\x7f]", "?", str(path))[:200]
        elapsed = max(0.0, float(elapsed_ms))
        with self._lock:
            self._total += 1
            self._by_status[int(status)] += 1
            # 方法名来自请求行、客户端可任意构造：只区分标准方法与
            # __other__（先到先占的固定预算会被 16 个伪造方法永久占满，
            # 之后真实 GET/POST 全部折叠进 __other__）
            method_text = str(method).upper()[:16] or "UNKNOWN"
            safe_method = (
                method_text if method_text in _STANDARD_METHODS else _OTHER
            )
            self._by_method[safe_method] += 1
            if len(self._by_path) < MAX_PATHS or safe_path in self._by_path:
                self._by_path[safe_path] += 1
            else:
                self._by_path[_OTHER] += 1
            if not streaming and not count_only:
                self._duration_sum_ms += elapsed
                self._duration_count += 1
                self._duration_max_ms = max(self._duration_max_ms, elapsed)
                self._durations.append(elapsed)

    def record_sse_error(self) -> None:
        """记录一次以 error 事件收尾的流式回合（HTTP 已发出 200）。

        访问日志与按状态码的指标只能看到 200，对话错误率需要一个独立
        口径，否则永远为 0。
        """
        with self._lock:
            self._sse_errors += 1

    def record_preference_cache(
        self, hit: bool, *, version_changed: bool = False
    ) -> None:
        """记录一次偏好快照缓存读取（PostgreSQL 后端）。

        ``version_changed`` 表示未命中是「快照仍在 TTL 内、因跨进程共享
        版本变化被迫回源」——它是 misses 的子集，用于评估 Redis 版本键
        挡下的陈旧读取（TTL 过期、版本读取失败都不计入）。
        """
        with self._lock:
            if hit:
                self._pref_cache_hits += 1
                return
            self._pref_cache_misses += 1
            if version_changed:
                self._pref_cache_version_invalidations += 1

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
                "avg_duration_ms": (
                    round(self._duration_sum_ms / self._duration_count, 2)
                    if self._duration_count
                    else 0.0
                ),
                "max_duration_ms": round(self._duration_max_ms, 2),
                "p50_duration_ms": round(_percentile(samples, 0.50), 2),
                "p95_duration_ms": round(_percentile(samples, 0.95), 2),
                "duration_samples": len(samples),
                "sse_errors": self._sse_errors,
                "preference_cache": {
                    "hits": self._pref_cache_hits,
                    "misses": self._pref_cache_misses,
                    "version_invalidations": (
                        self._pref_cache_version_invalidations
                    ),
                },
            }

    def render_prometheus(self) -> str:
        """渲染 Prometheus 文本格式（每次调用是一份一致的快照）。"""
        with self._lock:
            total = self._total
            statuses = sorted(self._by_status.items())
            methods = self._by_method.most_common()
            paths = self._by_path.most_common()
            avg_seconds = (
                self._duration_sum_ms / self._duration_count / 1000
                if self._duration_count
                else 0.0
            )
            max_seconds = self._duration_max_ms / 1000
            samples = sorted(self._durations)
            p50_seconds = _percentile(samples, 0.50) / 1000
            p95_seconds = _percentile(samples, 0.95) / 1000
            sample_count = len(samples)
            sse_errors = self._sse_errors
            pref_hits = self._pref_cache_hits
            pref_misses = self._pref_cache_misses
            pref_version_invalidations = (
                self._pref_cache_version_invalidations
            )
            uptime = time.monotonic() - self._started_at

        lines = [
            "# HELP aligo_requests_total Total HTTP requests to /api routes (plus Host-rejected probes) recorded by this process; static assets, / and /metrics scrapes are not counted.",
            "# TYPE aligo_requests_total counter",
            f"aligo_requests_total {total}",
            "# HELP aligo_requests_by_status_total Requests grouped by HTTP status.",
            "# TYPE aligo_requests_by_status_total counter",
            "# HELP aligo_sse_errors_total Streaming turns that ended with an error event.",
            "# TYPE aligo_sse_errors_total counter",
            f"aligo_sse_errors_total {sse_errors}",
            "# HELP aligo_preference_cache_hits_total Preference snapshot cache hits (process).",
            "# TYPE aligo_preference_cache_hits_total counter",
            f"aligo_preference_cache_hits_total {pref_hits}",
            "# HELP aligo_preference_cache_misses_total Preference snapshot cache misses (process).",
            "# TYPE aligo_preference_cache_misses_total counter",
            f"aligo_preference_cache_misses_total {pref_misses}",
            "# HELP aligo_preference_cache_version_invalidations_total Misses a shared preference version change caused while the snapshot was still fresh (subset of misses).",
            "# TYPE aligo_preference_cache_version_invalidations_total counter",
            "aligo_preference_cache_version_invalidations_total "
            f"{pref_version_invalidations}",
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
            self._duration_count = 0
            self._sse_errors = 0
            self._pref_cache_hits = 0
            self._pref_cache_misses = 0
            self._pref_cache_version_invalidations = 0


REQUEST_METRICS = RequestMetrics()
