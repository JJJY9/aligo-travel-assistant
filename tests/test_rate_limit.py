"""
滑动窗口限流器单元测试（无需 API Key）

运行：python tests/test_rate_limit.py
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.rate_limit import RateLimiter


def test_window_and_keys():
    limiter = RateLimiter(limit=2, window_sec=0.3)
    assert limiter.check("user-a") is None
    assert limiter.check("user-a") is None
    retry_after = limiter.check("user-a")
    assert retry_after is not None and retry_after > 0

    # 不同 key 互不影响
    assert limiter.check("user-b") is None

    # 窗口过期后恢复
    time.sleep(0.35)
    assert limiter.check("user-a") is None
    print("✓ 窗口计数 / 多 key / 窗口恢复")


def test_reset():
    limiter = RateLimiter(limit=1, window_sec=60)
    assert limiter.check("k") is None
    assert limiter.check("k") is not None
    limiter.reset()
    assert limiter.check("k") is None
    print("✓ 重置限流状态")


def test_key_eviction_bounds_memory():
    """大量随机 key（如轮换用户 ID）不应导致内存无限增长。"""
    limiter = RateLimiter(limit=1, window_sec=0.2, max_keys=8)
    for i in range(50):
        limiter.check(f"user-{i}")
    assert len(limiter) <= 8, f"key 数量应被限制在上限内，实际 {len(limiter)}"

    # 窗口过期后再触发一次扫描，过期 key 应被清空
    time.sleep(0.25)
    limiter.check("fresh")
    assert len(limiter) == 1, f"过期 key 应被清理，实际 {len(limiter)}"
    print("✓ key 数量有上限且过期 key 会被清理")


def test_status_reports_remaining():
    limiter = RateLimiter(limit=2, window_sec=60)
    assert limiter.status("k") == (2, 0.0)
    assert limiter.check("k") is None
    remaining, reset = limiter.status("k")
    assert remaining == 1 and 0 < reset <= 60
    limiter.check("k")
    assert limiter.status("k")[0] == 0
    print("✓ status 报告剩余次数 / 重置时间")


if __name__ == "__main__":
    test_window_and_keys()
    test_reset()
    test_key_eviction_bounds_memory()
    test_status_reports_remaining()
    print("\n全部通过")
