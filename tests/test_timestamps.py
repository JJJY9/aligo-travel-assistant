"""utils.timestamps 排序工具测试（无需 API Key、不访问网络）。

运行：python tests/test_timestamps.py
"""
import os
import sys
from datetime import datetime, timezone

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.timestamps import chronological_key, to_aware


def test_to_aware_parses_naive_and_aware():
    naive = to_aware("2026-11-02T10:00:00")
    assert naive is not None and naive.tzinfo is not None
    aware = to_aware("2026-11-02T07:00:00+00:00")
    assert aware == datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)
    assert to_aware("") is None and to_aware(None) is None
    assert to_aware("not-a-date") is None
    print("✓ naive 补齐本机时区，带时区 / 非法输入处理正确")


def test_chronological_key_compares_by_instant():
    # 02:00+09:00 = 前一日 17:00Z，早于 16:00Z；但字符串序恰好相反
    earlier = chronological_key("2026-11-02T02:00:00+09:00")  # 2026-11-01T17:00Z
    later = chronological_key("2026-11-02T16:00:00+00:00")  # 2026-11-02T16:00Z
    raw_earlier = "2026-11-02T02:00:00+09:00"
    raw_later = "2026-11-02T16:00:00+00:00"
    assert raw_earlier < raw_later  # 字符串序把它们排错了方向
    assert earlier < later, "按键应换算成 UTC 后比较"
    assert chronological_key("") == ""
    print("✓ 不同时区表示按真实时刻排序（字符串序不可靠）")


def test_naive_and_aware_same_instant_share_key():
    naive_key = chronological_key("2026-11-02T10:00:00")
    aware = datetime.fromisoformat("2026-11-02T10:00:00").replace(
        tzinfo=datetime.now().astimezone().tzinfo
    )
    assert naive_key == chronological_key(aware.isoformat())
    print("✓ naive 与本机时区的同一墙钟时刻键一致")


if __name__ == "__main__":
    test_to_aware_parses_naive_and_aware()
    test_chronological_key_compares_by_instant()
    test_naive_and_aware_same_instant_share_key()
    print("\n全部通过")
