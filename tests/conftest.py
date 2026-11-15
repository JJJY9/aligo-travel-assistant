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
