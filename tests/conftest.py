"""pytest 全局夹具。

合并体积预检（``merged_backup_limit_problem``）会读取全局知识库来把
「恢复后带知识库导出」计入体积。开发机上的 skill 知识库是真实 Milvus
实例：逐用例扫描既慢、又与其它测试（以及外部进程的 Milvus Lite 文件锁）
互相干扰。单测默认把「现有知识库片段」视为空，需要知识语义的用例自行
monkeypatch ``utils.user_import._existing_knowledge_rows`` / 知识库规划。
"""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_knowledge_library(monkeypatch):
    from utils import user_import

    monkeypatch.setattr(
        user_import, "_existing_knowledge_rows", lambda: []
    )
    # 预览的知识可用性探针会加载真实嵌入模型：单测默认替换为 no-op，
    # 需要验证模型不可用的用例自行覆盖该探针
    monkeypatch.setattr(
        user_import, "_knowledge_ready_probe", lambda: None
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
