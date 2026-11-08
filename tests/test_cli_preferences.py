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


def test_preferences_hides_blank_and_null_items(monkeypatch):
    """文本与 --json 口径同面板：null / 空白串 / 全空列表不列出，列表内空项剔除。"""
    store = _FakeStore(
        {
            "hotel_brands": ["东航", None, "  "],
            "home_location": "   ",
            "airlines": [""],
            "budget_level": 0,
        }
    )
    code, out = _run_list(monkeypatch, store)
    assert code == 0
    assert "2 条偏好" in out, out
    assert "东航" in out and "None" not in out
    assert "home_location" not in out and "airlines" not in out
    assert "0" in out, "0 是合法偏好值"

    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_preferences_list_standalone("u-prefs", as_json=True)
    payload = json.loads(buf.getvalue())
    assert payload["count"] == 2
    assert set(payload["preferences"]) == {"hotel_brands", "budget_level"}
    assert payload["preferences"]["budget_level"] == 0
    print("✓ preferences 隐藏空白 / null 项（文本与 JSON 同口径）")


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


class _WriteStore(_FakeStore):
    """支持 save_preference / delete_preference 的假存储。"""

    def __init__(self, preferences=(), save_error=None, delete_error=None):
        super().__init__(preferences)
        self.save_error = save_error
        self.delete_error = delete_error
        self.saved = []
        self.deleted = []

    def save_preference(self, pref_type, value):
        if self.save_error:
            raise RuntimeError("disk full")
        self.preferences[pref_type] = value
        self.saved.append((pref_type, value))

    def get_preference(self, pref_type=None):
        if self.fail:
            raise RuntimeError("db down")
        if pref_type is None:
            return dict(self.preferences)
        return self.preferences.get(pref_type)

    def delete_preference(self, pref_type):
        if self.delete_error:
            raise RuntimeError("disk error")
        existed = pref_type in self.preferences
        self.preferences.pop(pref_type, None)
        if existed:
            self.deleted.append(pref_type)
        return existed


def test_pref_set_text_and_json(monkeypatch):
    store = _WriteStore({"home_location": "北京"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone("u1", "home_location", "上海")
    assert code == 0
    assert "已设置偏好 常驻地：上海" in out.getvalue()
    assert store.saved == [("home_location", "上海")] and store.closed

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone(
            "u1", "hotel_brands", "汉庭", as_json=True
        )
    data = json.loads(out.getvalue())
    assert code == 0
    assert data["type"] == "hotel_brands" and data["value"] == "汉庭"
    print("✓ pref-set 文本 / JSON")


def test_pref_delete_text_and_json(monkeypatch):
    store = _WriteStore({"home_location": "北京"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_delete_standalone("u1", "home_location")
    assert code == 0
    assert "已删除偏好 常驻地" in out.getvalue()
    assert store.deleted == ["home_location"]

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_delete_standalone("u1", "home_location")
    assert code == 1 and "未设置或已删除" in out.getvalue()

    store2 = _WriteStore({"home_location": "北京"})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store2
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_delete_standalone(
            "u1", "home_location", as_json=True
        )
    data = json.loads(out.getvalue())
    assert code == 0 and data["deleted"] is True
    print("✓ pref-delete 文本 / JSON / 未设置")


def test_pref_write_validation_and_failures(monkeypatch):
    store = _WriteStore({})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    cases = [
        (
            cli.run_preference_set_standalone,
            ("bad id", "home_location", "上海"),
            "用户 ID",
        ),
        (
            cli.run_preference_set_standalone,
            ("u1", "bad type", "上海"),
            "偏好类型",
        ),
        (
            cli.run_preference_set_standalone,
            ("u1", "home_location", "  "),
            "不能为空",
        ),
        (
            cli.run_preference_delete_standalone,
            ("bad id", "home_location"),
            "用户 ID",
        ),
        (
            cli.run_preference_delete_standalone,
            ("u1", "bad type"),
            "偏好类型",
        ),
    ]
    for func, args, needle in cases:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(*args)
        assert code == 1 and needle in out.getvalue(), args
    assert store.saved == [] and store.deleted == []

    save_fail = _WriteStore({}, save_error=True)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: save_fail
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone(
            "u1", "home_location", "上海", as_json=True
        )
    data = json.loads(out.getvalue())
    assert code == 1 and "保存偏好失败" in data["error"]

    delete_fail = _WriteStore({"home_location": "北京"}, delete_error=True)
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: delete_fail
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_delete_standalone("u1", "home_location")
    assert code == 1 and "删除偏好失败" in out.getvalue()

    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone("u1", "home_location", "上海")
    assert code == 1 and "打开长期记忆失败" in out.getvalue()
    print("✓ pref-set / pref-delete 校验与失败路径")


def test_pref_write_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_preference_set_standalone",
        lambda user_id, pref_type, value, as_json=False: (
            calls.append(("set", user_id, pref_type, value, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_preference_delete_standalone",
        lambda user_id, pref_type, as_json=False: (
            calls.append(("del", user_id, pref_type, as_json)) or 0
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "pref-set", "u1", "home_location", "上海", "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("set", "u1", "home_location", "上海", True)

    monkeypatch.setattr(
        sys, "argv", ["cli.py", "pref-delete", "u1", "home_location"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("del", "u1", "home_location", False)

    for argv in (
        ["cli.py", "pref-set"],
        ["cli.py", "pref-set", "u1", "home_location"],
        ["cli.py", "pref-set", "u1", "home_location", "上海", "extra"],
        ["cli.py", "pref-delete"],
        ["cli.py", "pref-delete", "u1"],
        ["cli.py", "pref-delete", "u1", "home_location", "extra"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法" in out.getvalue()
    print("✓ pref-set / pref-delete dispatch")


def test_pref_set_rejects_oversized_value(monkeypatch):
    """与 Web 的 PreferencePayload 同一上限：超过 500 字的值直接拒绝。"""
    store = _WriteStore({})
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone("u1", "memo", "长" * 501)
    assert code == 1
    assert "500" in out.getvalue()
    assert store.saved == [], "超长偏好值不应落库"
    # 500 字以内仍可保存
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.run_preference_set_standalone("u1", "memo", "长" * 500)
    assert code == 0 and store.saved == [("memo", "长" * 500)]
    print("✓ pref-set 拒绝超过 500 字的值")


if __name__ == "__main__":
    for case in (
        test_preferences_list_output,
        test_preferences_list_empty,
        test_preferences_list_read_failure,
        test_preferences_list_open_failure,
        test_preferences_rejects_bad_user_id,
        test_preferences_dispatch_strict,
        test_preferences_dispatch_json_flexible,
        test_pref_set_text_and_json,
        test_pref_delete_text_and_json,
        test_pref_write_validation_and_failures,
        test_pref_write_dispatch,
        test_pref_set_rejects_oversized_value,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    print("\n全部通过")
