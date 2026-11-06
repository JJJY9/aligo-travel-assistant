"""
`python cli.py preferences` 测试（无需 API Key、不访问网络）

运行：python tests/test_cli_preferences.py
"""
import contextlib
import io
import json
import os
import sys

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage


class _FakeStore:
    def __init__(self, preferences):
        self.preferences = dict(preferences)
        self.fail = False
        self.closed = False

    def get_preference(self):
        if self.fail:
            raise RuntimeError("db down")
        return dict(self.preferences)

    def close(self):
        self.closed = True


def _run_list(monkeypatch, store):
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *args, **kwargs: store)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preferences_list_standalone("u-prefs")
    return code, out.getvalue()


def test_preferences_list_json(monkeypatch):
    """--json 输出原始偏好值（列表 / 布尔 / 0 保持原类型）。"""
    store = _FakeStore(
        {
            "home_location": "北京",
            "hotel_brands": ["汉庭", "如家"],
            "eco_mode": True,
            "budget_level": 0,
        }
    )
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preferences_list_standalone("u-prefs", as_json=True)
    assert code == 0
    data = json.loads(out.getvalue())
    assert data["user_id"] == "u-prefs" and data["count"] == 4
    assert data["preferences"]["hotel_brands"] == ["汉庭", "如家"]
    assert data["preferences"]["eco_mode"] is True
    assert data["preferences"]["budget_level"] == 0
    print("✓ preferences --json 保留原始类型")


def test_preferences_dispatch_json_and_bad_args(monkeypatch):
    store = _FakeStore({"home_location": "北京"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "preferences", "u1", "--json"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and '"home_location"' in buf.getvalue()

    monkeypatch.setattr(
        sys, "argv", ["cli.py", "preferences", "u1", "--json", "x"]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and "用法" in buf.getvalue()
    print("✓ dispatch 层 preferences --json / 多余参数拒绝")


def test_preferences_list_output(monkeypatch):
    store = _FakeStore(
        {
            "home_location": "北京",
            "hotel_brands": ["汉庭", "如家"],
            "seat_preference": "靠窗",
            "eco_mode": True,
            "budget_level": 0,
        }
    )
    code, out = _run_list(monkeypatch, store)
    assert code == 0 and store.closed
    assert "共 5 条偏好" not in out and "5 条偏好" in out
    assert "常驻地：北京" in out
    assert "酒店偏好：汉庭、如家" in out
    assert "座位偏好：靠窗" in out
    assert "环保模式：是" in out or "eco_mode：是" in out
    assert "预算等级：0" in out or "budget_level：0" in out
    print("✓ preferences 列表（中文标签 / 列表拼接 / 布尔）")


def test_preferences_list_empty(monkeypatch):
    code, out = _run_list(monkeypatch, _FakeStore({}))
    assert code == 0 and "还没有偏好记录" in out
    print("✓ preferences 空列表")


def test_preferences_list_read_failure(monkeypatch):
    store = _FakeStore({})
    store.fail = True
    code, out = _run_list(monkeypatch, store)
    assert code == 1 and "读取偏好失败" in out and store.closed
    print("✓ preferences 读取失败返回 1")


def test_preferences_list_open_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preferences_list_standalone("u-prefs")
    assert code == 1 and "打开长期记忆失败" in out.getvalue()
    print("✓ preferences 打开存储失败返回 1")


def test_preferences_rejects_bad_user_id(monkeypatch):
    for user_id in ("bad id", "-u1", "a" * 65):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run_preferences_list_standalone(user_id)
        assert code == 1 and "用户 ID" in out.getvalue()
    print("✓ preferences 非法用户 ID")


def test_preferences_dispatch_json_flexible(monkeypatch):
    """--json 位置灵活、大小写不敏感、可重复（与 stats/sessions 一致）。"""
    store = _FakeStore({"home_location": "北京"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    for argv in (
        ["cli.py", "preferences", "--json", "u1"],
        ["cli.py", "preferences", "u1", "--json", "--json"],
        ["cli.py", "preferences", "u1", "--JSON"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0, argv
        assert '"home_location"' in buf.getvalue(), argv
    print("✓ dispatch 层 preferences --json 位置 / 大小写 / 重复")


def test_preferences_dispatch_strict(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_preferences_list_standalone",
        lambda user, **kwargs: calls.append(user) or 0,
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "preferences", "u1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == ["u1"]

    for argv in (
        ["cli.py", "preferences"],
        ["cli.py", "preferences", "-u1"],
        ["cli.py", "preferences", "bad id"],
        ["cli.py", "preferences", "u1", "extra"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py preferences <user_id>" in out.getvalue()
    print("✓ preferences 严格参数解析")


if __name__ == "__main__":
    for case in (
        test_preferences_list_output,
        test_preferences_list_empty,
        test_preferences_list_read_failure,
        test_preferences_list_open_failure,
        test_preferences_rejects_bad_user_id,
        test_preferences_dispatch_strict,
        test_preferences_dispatch_json_flexible,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
