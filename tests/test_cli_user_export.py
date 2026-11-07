"""`python cli.py user-export` 测试（无需 API Key、不访问网络）。"""
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
    def __init__(self):
        self.fail = False
        self.closed = False

    def get_preference(self):
        if self.fail:
            raise RuntimeError("db down")
        return {"home_location": "北京", "hotel_brands": ["汉庭"]}

    def get_trip_history(self, limit=None):
        return [
            {
                "trip_id": "trip_1",
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-05",
                "end_date": "2026-12-07",
                "purpose": "出差",
            }
        ]

    def get_statistics(self):
        return {"total_trips": 1, "total_messages": 2, "total_queries": 1}

    def get_session_titles(self):
        return {"s1": "出差规划"}

    def get_pinned_sessions(self):
        return ["s1"]

    def get_archived_sessions(self):
        return []

    def get_chat_history(self, limit=None, session_id=None):
        return [
            {"role": "user", "content": "你好", "timestamp": "2026-11-06T10:00:00"},
            {"role": "assistant", "content": "您好", "timestamp": "2026-11-06T10:00:01"},
        ]

    def close(self):
        self.closed = True


def _run(user_id, target=None, with_knowledge=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_user_export_standalone(
            user_id, target, with_knowledge=with_knowledge
        )
    return code, buf.getvalue()


def test_user_export_writes_json_and_markdown(monkeypatch):
    import tempfile
    from pathlib import Path

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    out_dir = Path(tempfile.mkdtemp())
    code, out = _run("u-export", str(out_dir))
    assert code == 0 and store.closed
    files = sorted(out_dir.iterdir())
    assert len(files) == 2
    json_path = next(path for path in files if path.suffix == ".json")
    md_path = next(path for path in files if path.suffix == ".md")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["user_id"] == "u-export"
    assert data["preferences"]["home_location"] == "北京"
    assert data["trip_history"][0]["destination"] == "上海"
    assert data["session_titles"] == {"s1": "出差规划"}, "导出需包含会话标题"
    assert data["pinned_sessions"] == ["s1"]
    assert data["archived_sessions"] == []
    markdown = md_path.read_text(encoding="utf-8")
    assert "## 用户偏好" in markdown and "## 历史行程" in markdown
    assert "你好" in markdown
    assert "已导出：" in out
    print("✓ user-export 写出 JSON + Markdown")


def test_user_export_default_dir_and_failures(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    code, out = _run("u-export")
    assert code == 0
    files = os.listdir(os.path.join(workdir, "exports"))
    assert any(name.endswith(".json") for name in files)
    assert any(name.endswith(".md") for name in files)

    assert cli.run_user_export_standalone("-bad") == 1

    store.fail = True
    code, out = _run("u-fail", workdir)
    assert code == 1 and "读取用户数据失败" in out

    # 写入失败：目标目录的父级是一个普通文件（mkdir 抛 OSError）
    store.fail = False
    blocker = os.path.join(workdir, "blocker")
    with open(blocker, "w", encoding="utf-8") as handle:
        handle.write("x")
    code, out = _run("u-write-fail", os.path.join(blocker, "sub"))
    assert code == 1 and "写入失败" in out

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    store.fail = True
    code, out = _run("u-fail", workdir)
    assert code == 1 and "读取用户数据失败" in out

    def boom(*args, **kwargs):
        raise RuntimeError("open down")

    monkeypatch.setattr(storage, "create_long_term_memory", boom)
    code, out = _run("u-fail", workdir)
    assert code == 1 and "打开长期记忆失败" in out
    print("✓ user-export 默认目录 / 非法用户 / 读取与打开失败")


def test_user_export_write_failure_reports(monkeypatch):
    """输出目录被文件占用：写入失败明确报错并退出 1。"""
    import os
    import tempfile

    store = _FakeStore()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a: store)
    blocker = os.path.join(tempfile.mkdtemp(), "blocker")
    with io.open(blocker, "w", encoding="utf-8") as handle:
        handle.write("x")
    code, out = _run("u1", target=blocker)
    assert code == 1 and "写入失败" in out
    print("✓ user-export 写入失败友好报错")


def test_user_export_dispatch(monkeypatch):
    import tempfile

    workdir = tempfile.mkdtemp()
    monkeypatch.chdir(workdir)
    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", "user-export", "u1"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert os.path.isdir(os.path.join(workdir, "exports"))

    for argv in (
        ["cli.py", "user-export"],
        ["cli.py", "user-export", "-u1"],
        ["cli.py", "user-export", "u1", "a", "b"],
        ["cli.py", "user-export", "u1", "--x"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2, argv
        assert "用法：python cli.py user-export" in buf.getvalue()
    print("✓ dispatch 层 user-export 正常执行 / 严格参数")


def test_user_export_with_knowledge(monkeypatch):
    """--with-knowledge：知识库片段写入同一份 JSON；导出失败则中止。"""
    import tempfile
    from pathlib import Path

    from utils import knowledge_admin as admin_module

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    docs = [{"content": "知识片段", "metadata": {"category": "住宿"}}]
    monkeypatch.setattr(admin_module, "export_documents", lambda: docs)
    out_dir = Path(tempfile.mkdtemp())
    code, out = _run("u-kb", str(out_dir), with_knowledge=True)
    assert code == 0
    json_path = next(path for path in out_dir.iterdir() if path.suffix == ".json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["knowledge"] == docs

    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(admin_module, "export_documents", boom)
    code, out = _run("u-kb", str(Path(tempfile.mkdtemp())), with_knowledge=True)
    assert code == 1 and "读取知识库失败" in out, "导出失败不得产出缺知识段的备份"
    print("✓ user-export --with-knowledge 一体化备份")


def test_user_export_with_knowledge_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_user_export_standalone",
        lambda user_id, target_dir=None, with_knowledge=False: (
            calls.append((user_id, target_dir, with_knowledge)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "user-export", "u1", "--with-knowledge"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", None, True)
    print("✓ user-export dispatch --with-knowledge")

if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_writes_json_and_markdown(mp)
        mp.undo()
        mp = pytest.MonkeyPatch()
        test_user_export_default_dir_and_failures(mp)
        mp.undo()
        mp = pytest.MonkeyPatch()
        test_user_export_dispatch(mp)
        test_user_export_with_knowledge(mp)
        test_user_export_with_knowledge_dispatch(mp)
    finally:
        mp.undo()
    print("\n全部通过")
