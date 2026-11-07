"""utils.timestamps 排序工具测试（无需 API Key、不访问网络）。

运行：python tests/test_timestamps.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

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
    naive_dt = datetime(2026, 11, 2, 10, 0)
    utc_instant = datetime.fromtimestamp(naive_dt.timestamp(), tz=timezone.utc)
    assert naive_key == utc_instant.isoformat(), "naive 键必须等于该墙钟时刻的真实 UTC"
    print("✓ naive 与本机墙钟时刻的真实 UTC 键一致")


def test_z_suffix_accepted():
    """合法 UTC 写法 ...Z（Python 3.10 的 fromisoformat 不支持）也能解析。"""
    assert to_aware("2026-11-02T10:00:00Z") == datetime(
        2026, 11, 2, 10, 0, tzinfo=timezone.utc
    )
    assert to_aware("2026-11-02T10:00:00z") == datetime(
        2026, 11, 2, 10, 0, tzinfo=timezone.utc
    )
    assert chronological_key("2026-11-02T10:00:00Z") == chronological_key(
        "2026-11-02T10:00:00+00:00"
    )
    print("✓ Z 后缀时间戳解析为 UTC")


def test_naive_uses_dst_offset_of_that_moment():
    """夏令时地区：冬季 naive 时间戳按冬季偏移解释，不能用当前（夏季）定值偏移。"""
    if sys.platform == "win32":
        print("✓ 跳过：Windows 没有 tzset（该场景由 Linux CI 覆盖）")
        return
    import time

    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        winter = to_aware("2026-01-15T12:00:00")
        summer = to_aware("2026-07-15T12:00:00")
        assert winter is not None and winter.utcoffset() == timedelta(hours=1), winter
        assert summer is not None and summer.utcoffset() == timedelta(hours=2), summer
        # 同一真实时刻的 naive 与 Z 写法键一致（冬季 12:00 本地 = 11:00Z）
        assert chronological_key("2026-01-15T12:00:00") == chronological_key(
            "2026-01-15T11:00:00Z"
        )
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    print("✓ naive 时间戳按当日 DST 偏移解释")


if __name__ == "__main__":
    test_to_aware_parses_naive_and_aware()
    test_chronological_key_compares_by_instant()
    test_naive_and_aware_same_instant_share_key()
    test_z_suffix_accepted()
    test_naive_uses_dst_offset_of_that_moment()
    print("\n全部通过")
