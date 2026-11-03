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


if __name__ == "__main__":
    test_window_and_keys()
    test_reset()
    print("\n全部通过")
