"""`python cli.py user-export` 测试（无需 API Key、不访问网络）。"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

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


def test_user_export_refuses_oversized_backup(monkeypatch):
    """超出导入上限的备份直接拒绝，不写出永远无法恢复的文件。"""
    import tempfile
    from pathlib import Path

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    from utils import user_import as module

    monkeypatch.setattr(module, "_MAX_MESSAGES", 1)
    target = tempfile.mkdtemp()
    code, out = _run("u-oversize", target)
    assert code == 1
    assert "导出中止" in out and "导入上限" in out
    assert list(Path(target).glob("*")) == []
    print("✓ 超限备份拒绝导出")


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
    # CLI / Web 导出与体积轴统一按 LF：Windows 默认 CRLF 会让同一份
    # 数据每行多 1 字节，接近上限时两通道结论不一致
    assert b"\r\n" not in json_path.read_bytes(), "导出的 JSON 必须是 LF"
    assert b"\r\n" not in md_path.read_bytes(), "导出的 Markdown 必须是 LF"
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
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
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
        # 显式空目录（脚本变量未设置）不能静默回退到默认 exports/
        ["cli.py", "user-export", "u1", ""],
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


def test_user_export_degrades_knowledge_over_limit(monkeypatch):
    """仅知识轴超限时省略知识段继续导出，并明确提示不含知识；
    规划失败给出「稍后重试」文案；用户数据轴超限仍中止。"""
    import tempfile
    from pathlib import Path

    from utils import knowledge_admin as admin_module
    from utils import user_import as module

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    docs = [
        {"content": "片段一", "metadata": {"category": "住宿"}},
        {"content": "片段二", "metadata": {"category": "住宿"}},
    ]
    monkeypatch.setattr(admin_module, "export_documents", lambda: docs)
    monkeypatch.setattr(module, "_MAX_KNOWLEDGE", 1)
    out_dir = Path(tempfile.mkdtemp())
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = cli.run_user_export_standalone(
            "u-kb-omit", str(out_dir), with_knowledge=True
        )
    assert code == 0, buf.getvalue()
    json_path = next(path for path in out_dir.iterdir() if path.suffix == ".json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "knowledge" not in data
    assert "不含知识片段" in err.getvalue()

    # 分片规划失败（知识库忙）：同样降级，但文案引导稍后重试而非清理知识库
    monkeypatch.setattr(module, "_MAX_KNOWLEDGE", 50000)

    def planning_boom(*args, **kwargs):
        raise RuntimeError("kb busy")

    monkeypatch.setattr(admin_module, "import_documents", planning_boom)
    planning_dir = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = cli.run_user_export_standalone(
            "u-kb-planning", str(planning_dir), with_knowledge=True
        )
    assert code == 0, err.getvalue()
    assert "暂时无法核对分片体积" in err.getvalue()
    assert "稍后重试" in err.getvalue()

    # 用户数据本身超限：知识再省略也不能导出（与 Web 413 同口径）
    monkeypatch.setattr(module, "_MAX_MESSAGES", 1)  # _FakeStore 自带 2 条消息
    blocked_dir = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()) as blocked_out:
        code = cli.run_user_export_standalone(
            "u-kb-oversize", str(blocked_dir), with_knowledge=True
        )
    assert code == 1 and "导出中止" in blocked_out.getvalue()
    assert list(blocked_dir.iterdir()) == []
    print("✓ user-export 仅知识轴超限 / 规划失败时降级，用户数据超限仍中止")


def test_user_export_precheck_matches_sanitized_output(monkeypatch):
    """限额预检与落盘同口径：含孤立代理项的知识行不再误报「会被跳过」，
    写出的行仍保留（转义为合法文本），体积轴按清洗后的重切分体积计算。"""
    import tempfile
    from pathlib import Path

    from utils import knowledge_admin as admin_module

    store = _FakeStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    docs = [{"content": "坏 \ud800 片段", "metadata": {"category": "住宿"}}]
    monkeypatch.setattr(admin_module, "export_documents", lambda: docs)
    out_dir = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = cli.run_user_export_standalone(
            "u-kb-surrogate", str(out_dir), with_knowledge=True
        )
    assert code == 0, err.getvalue()
    assert "会被跳过" not in err.getvalue(), err.getvalue()
    json_path = next(path for path in out_dir.iterdir() if path.suffix == ".json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["knowledge"]) == 1
    assert "\\ud800" in data["knowledge"][0]["content"]
    print("✓ user-export 限额预检与清洗后的落盘内容同口径")


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

def test_user_export_with_knowledge_case_insensitive(monkeypatch):
    """--with-knowledge 与其它选项一致：大小写不敏感。"""
    calls = []
    monkeypatch.setattr(
        cli,
        "run_user_export_standalone",
        lambda user_id, target_dir=None, with_knowledge=False: (
            calls.append((user_id, target_dir, with_knowledge)) or 0
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["cli.py", "user-export", "u1", "--WITH-KNOWLEDGE"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls[-1] == ("u1", None, True)
    print("✓ user-export --WITH-KNOWLEDGE 大小写不敏感")


def test_user_export_refuses_when_memory_file_quarantined(monkeypatch):
    """损坏记忆文件被隔离后不能把空快照当成成功备份导出。"""
    import tempfile
    from pathlib import Path

    class _CorruptStore(_FakeStore):
        def get_recovered_corrupt_backup(self):
            return "data/memory/u-corrupt.json.corrupt-20261113-1200"

    store = _CorruptStore()
    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: store
    )
    target = tempfile.mkdtemp()
    code, out = _run("u-corrupt", target)
    assert code == 1, out
    assert "导出中止" in out and "损坏" in out, out
    assert list(Path(target).glob("*")) == [], "损坏状态不能写出空备份"
    assert store.closed
    print("✓ 记忆文件损坏隔离后拒绝导出空备份")


def test_user_export_scans_existing_quarantine_backups(monkeypatch, tmp_path):
    """更早进程留下的 .corrupt-* 备份同样拦截导出（主文件缺失即视为损坏）。"""
    class _MissingStore(_FakeStore):
        db_path = str(tmp_path / "u-old.json")

    (tmp_path / "u-old.json.corrupt-20260101-000000-1").write_text(
        "{}", encoding="utf-8"
    )
    store = _MissingStore()
    monkeypatch.setattr(storage, "create_long_term_memory", lambda *a, **k: store)
    target = tmp_path / "exports"
    code, out = _run("u-old", str(target))
    assert code == 1, out
    assert "损坏" in out, out
    assert not target.exists() or list(target.glob("*")) == []
    # 主文件恢复后不再拦截
    (tmp_path / "u-old.json").write_text("{}", encoding="utf-8")
    assert cli._recovered_corrupt_backup(_MissingStore()) is None
    print("✓ 已存在的隔离备份同样拦截导出")


def test_interactive_export_uses_restore_shape_check(monkeypatch, tmp_path):
    """交互菜单 export 与 user-export / Web 同一「恢复后形状」50MB 复核。

    #929 只迁移了 standalone 与 Web；菜单路径只看原始字节会写出导入端
    必然拒绝的备份，并打印「✓ 已导出」。
    """
    from rich.console import Console

    from utils import exporter as exporter_module
    from utils import user_import

    agent = cli.AligoCLI()
    buffer = io.StringIO()
    agent.console = Console(file=buffer, force_terminal=False, width=120)
    agent.user_id = "u1"

    class _Store:
        def export_snapshot(self):
            return {
                "preferences": {"home_location": "北京"},
                "trip_history": [],
                "chat_history": [],
                "session_titles": {},
                "pinned_sessions": [],
                "archived_sessions": [],
                "statistics": {},
            }

        def close(self):
            pass

    monkeypatch.setattr(
        storage, "create_long_term_memory", lambda *args, **kwargs: _Store()
    )
    written: list = []
    monkeypatch.setattr(
        exporter_module,
        "export_files",
        lambda data, out: written.append(data)
        or [tmp_path / "aligo_u1.json"],
    )
    monkeypatch.setattr(
        user_import,
        "export_backup_limit_problem",
        lambda backup, **kwargs: "恢复后约 51.0 MB 超过导入上限 50 MB",
    )
    agent.export_memory()
    assert "导出中止" in buffer.getvalue(), buffer.getvalue()
    assert written == [], "形状复核拒绝时不能写出文件"

    monkeypatch.setattr(
        user_import,
        "export_backup_limit_problem",
        lambda backup, **kwargs: None,
    )
    agent.export_memory()
    assert written and written[-1]["user_id"] == "u1"
    assert "已导出" in buffer.getvalue()
    print("✓ 交互导出按恢复后形状复核 50MB")


if __name__ == "__main__":
    import _tmp_registry

    _tmp_registry.install()
    import sys

    # Windows 控制台 / 重定向管道默认 GBK：✓ 等字符会抛 UnicodeEncodeError，
    # 断言通过也会以 traceback 退出；先切 UTF-8（不支持时保持原状）
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(_stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    # 每个用例独立的 MonkeyPatch：共用实例会把全局补丁（storage 工厂 /
    # _MAX_MESSAGES）泄漏到下一个用例
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_writes_json_and_markdown(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_default_dir_and_failures(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_dispatch(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_with_knowledge(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_degrades_knowledge_over_limit(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_precheck_matches_sanitized_output(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_with_knowledge_dispatch(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_refuses_when_memory_file_quarantined(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_scans_existing_quarantine_backups(
            mp, Path(tempfile.mkdtemp())
        )
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_refuses_oversized_backup(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_write_failure_reports(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_user_export_with_knowledge_case_insensitive(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_interactive_export_uses_restore_shape_check(
            mp, Path(tempfile.mkdtemp())
        )
    finally:
        mp.undo()
    print("\n全部通过")
