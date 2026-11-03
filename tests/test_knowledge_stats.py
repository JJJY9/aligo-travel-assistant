"""
知识库状态测试（无需 API Key；本地已初始化知识库时验证片段数）

运行：python tests/test_knowledge_stats.py
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.knowledge_stats import DEFAULT_COLLECTION, knowledge_status


def test_missing_database():
    with tempfile.TemporaryDirectory() as tmp:
        status = knowledge_status(Path(tmp) / "missing.db")
    assert status["available"] is False
    assert status["reason"] == "知识库未初始化"
    assert status["collection"] == DEFAULT_COLLECTION
    assert status["path"].endswith("missing.db")
    print("✓ 知识库缺失时返回不可用")


def test_default_knowledge_base_if_present():
    status = knowledge_status()
    if not status["available"]:
        pytest.skip(f"本地未初始化知识库：{status.get('reason')}")
    assert status["documents"] > 0
    print(f"✓ 本地知识库：{status['documents']} 片段")


if __name__ == "__main__":
    test_missing_database()
    status = knowledge_status()
    if status["available"]:
        print(f"✓ 本地知识库：{status['documents']} 片段")
    else:
        print(f"（本地知识库不可用：{status.get('reason')}）")
    print("\n全部通过")
