"""pytest 全局夹具。

合并体积预检（``merged_backup_limit_problem``）会读取全局知识库来把
「恢复后带知识库导出」计入体积。开发机上的 skill 知识库是真实 Milvus
实例：逐用例扫描既慢、又与其它测试（以及外部进程的 Milvus Lite 文件锁）
互相干扰。单测默认把「现有知识库片段」视为空，需要知识语义的用例自行
monkeypatch ``utils.user_import._existing_knowledge_rows`` / 知识库规划。
"""
import pytest


def _ensure_utf8_stdio() -> None:
    """Windows GBK 控制台 / 管道下 ``pytest -s``（关闭捕获）时，用例里的
    ✓ 等字符会抛 UnicodeEncodeError 把整份运行变成失败：导入期先切
    UTF-8（与直跑清单的 ``tests/_stdio.ensure_utf8`` 同口径，不支持时
    保持原状）。默认捕获模式下 stdout 已被 pytest 替换，reconfigure 为
    no-op。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_ensure_utf8_stdio()


@pytest.fixture
def hermetic_kb_path(tmp_path_factory):
    """知识库隔离夹具注入的「不存在的库路径」（供回归用例断言相等）。"""
    return tmp_path_factory.mktemp("hermetic_kb") / "milvus_lite.db"


@pytest.fixture(autouse=True)
def _hermetic_knowledge_library(monkeypatch, hermetic_kb_path, request):
    from utils import knowledge_admin, user_import

    monkeypatch.setattr(
        user_import, "_existing_knowledge_rows", lambda: []
    )
    # 预览的知识可用性探针会加载真实嵌入模型：单测默认替换为 no-op，
    # 需要验证模型不可用的用例自行覆盖该探针
    monkeypatch.setattr(
        user_import, "_knowledge_ready_probe", lambda: None
    )
    # 规划路径（import_documents 的 dry-run / 布局扫描）同样不能打开
    # 真实全局库：此前只替换 existing 快照，开发机上单测仍会扫描真实
    # Milvus（与 CI「无库」行为不一致，还可能被其它进程的文件锁干扰）。
    # 把库路径指向不存在的文件，与 CI 一致；需要真实 / 临时知识库的
    # 用例自行 monkeypatch knowledge_db_path 或 import_documents 覆盖。
    # 例外：test_knowledge_admin_integration.py 按设计读写本地真实库
    #（自带 _skip_reason 在无库环境跳过），不能被重定向。
    node_path = getattr(request.node, "path", None)
    if (
        node_path is not None
        and node_path.name == "test_knowledge_admin_integration.py"
    ):
        return
    monkeypatch.setattr(
        knowledge_admin,
        "knowledge_db_path",
        lambda *args, **kwargs: hermetic_kb_path,
    )


@pytest.fixture(autouse=True)
def _clean_created_tempdirs(monkeypatch):
    """回收用例用 ``tempfile.mkdtemp()`` 自建的临时目录。

    这些目录里常含记忆 JSON（用户数据样本），此前每个用例跑完都留在
    系统临时目录里（pytest 只回收 ``tmp_path``，直跑清单也只清自己
    注入的夹具目录）。wrap ``mkdtemp`` 在用例结束时统一 rmtree。
    """
    import shutil
    import tempfile

    real_mkdtemp = tempfile.mkdtemp
    created: list[str] = []

    def tracked_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracked_mkdtemp)
    yield
    # 用例可能 chdir 进了自建目录（Windows 上目录非空 / 被占用时
    # rmtree 会失败）：先离开再删，monkeypatch 之后仍会恢复原 cwd
    import os

    cwd = os.path.realpath(os.getcwd())
    for path in created:
        real = os.path.realpath(path)
        try:
            inside = os.path.commonpath([cwd, real]) == real
        except ValueError:
            inside = False
        if inside:
            os.chdir(tempfile.gettempdir())
            cwd = os.path.realpath(os.getcwd())
        shutil.rmtree(path, ignore_errors=True)
