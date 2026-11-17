"""直跑清单的知识库隔离（与 ``tests/conftest.py`` 的 autouse 夹具同口径）。

把全局知识库路径指向不存在的文件（与 CI「无库」一致）：直跑时
``import_documents`` 的 dry-run / 布局扫描不会打开真实 Milvus，结论与
pytest / CI 一致。需要真实 / 临时库的用例已自行 monkeypatch
``knowledge_db_path`` 或 ``import_documents``，不受影响。

用法（直跑 ``__main__`` 的 kb_fixture 块内，紧随两个 setattr 之后）::

    import _direct_run_kb  # 直跑脚本目录（tests/）已在 sys.path

    _direct_run_kb.isolate(kb_fixture)

``kb_fixture.undo()`` 时路径自动恢复；临时目录由 ``_tmp_registry``
（``_stdio.ensure_utf8()`` 会顺带安装）在进程退出时回收。
"""
import tempfile
from pathlib import Path


def isolate(fixture) -> Path:
    """在给定 ``MonkeyPatch`` 上安装知识库路径隔离（undo 时恢复）。"""
    from utils import knowledge_admin

    missing = (
        Path(tempfile.mkdtemp(prefix="chailv_direct_kb_")) / "milvus_lite.db"
    )
    fixture.setattr(
        knowledge_admin,
        "knowledge_db_path",
        lambda *args, **kwargs: missing,
    )
    return missing
