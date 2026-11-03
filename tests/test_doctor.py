"""
doctor 环境自检测试（无需 API Key、不访问网络）

运行：python tests/test_doctor.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.doctor import (
    STATUS_OK,
    STATUS_WARN,
    check_knowledge_base,
    check_model_files,
    check_python,
    run_doctor,
)


def test_python_ok():
    assert check_python().status == STATUS_OK
    print("✓ Python 版本检查")


def test_model_files_missing_dir():
    result = check_model_files(Path(tempfile.mkdtemp()) / "not-exist")
    assert result.status == STATUS_WARN
    print("✓ 模型目录缺失 → 警告")


def test_model_files_complete():
    d = Path(tempfile.mkdtemp())
    for f in [
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "modules.json",
        "1_Pooling/config.json",
        "vocab.txt",
    ]:
        p = d / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    result = check_model_files(d)
    assert result.status == STATUS_OK, result.detail
    print("✓ 模型文件完整 → 通过")


def test_model_files_missing_metadata_warns():
    """只复制权重文件（缺 modules.json）时应给出 mean pooling 回退警告"""
    d = Path(tempfile.mkdtemp())
    for f in ["config.json", "model.safetensors", "tokenizer.json"]:
        (d / f).write_text("x", encoding="utf-8")
    result = check_model_files(d)
    assert result.status == STATUS_WARN
    assert "modules.json" in result.detail
    print("✓ 缺少 modules.json → 警告（提示 mean pooling 回退）")


def test_knowledge_base_missing():
    result = check_knowledge_base(Path(tempfile.mkdtemp()) / "milvus_lite.db")
    assert result.status == STATUS_WARN
    print("✓ 知识库未初始化 → 警告")


def test_run_doctor_without_network():
    results = asyncio.run(run_doctor(include_network=False))
    names = [r.name for r in results]
    assert "Python 版本" in names
    assert "核心依赖" in names
    assert "大模型配置" in names
    assert "嵌入模型文件" in names
    assert "RAG 知识库" in names
    assert "LLM 连通性" in names
    print(f"✓ run_doctor 汇总正常（共 {len(results)} 项，跳过网络）")


if __name__ == "__main__":
    test_python_ok()
    test_model_files_missing_dir()
    test_model_files_complete()
    test_model_files_missing_metadata_warns()
    test_knowledge_base_missing()
    test_run_doctor_without_network()
    print("\n全部通过")
