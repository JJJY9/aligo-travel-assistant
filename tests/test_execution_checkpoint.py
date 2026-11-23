"""执行检查点存储测试（v2.0，纯 Python）。

覆盖：落盘 / 读取、步骤合并、TTL 过期、损坏文件清理、条目上限、
键哈希（不把原始 key 用作路径）与清除。

运行：python tests/test_execution_checkpoint.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.execution_checkpoint import CheckpointStore


def _store(**kwargs):
    root = tempfile.mkdtemp(prefix="chailv_ckpt_")
    return CheckpointStore(root=root, **kwargs), root


def test_save_load_roundtrip_and_step_merge():
    store, root = _store()
    try:
        store.save("u1:s1:req1", {"status": "running"})
        state = store.load("u1:s1:req1")
        assert state["status"] == "running"
        assert state["key"] == "u1:s1:req1"
        store.update_step("u1:s1:req1", "event_collection", {"origin": "北京"})
        store.update_step("u1:s1:req1", "preference", {"hotel": "汉庭"})
        state = store.load("u1:s1:req1")
        assert state["status"] == "running", "顶层字段不能被步骤写入覆盖"
        assert state["steps"]["event_collection"] == {"origin": "北京"}
        assert state["steps"]["preference"] == {"hotel": "汉庭"}
        assert store.load("missing") is None
        store.clear("u1:s1:req1")
        assert store.load("u1:s1:req1") is None
        print("✓ 检查点落盘 / 步骤合并 / 清除")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ttl_expiry_removes_file():
    store, root = _store(ttl_sec=10)
    try:
        store.save("k", {"status": "ok"})
        path = next(Path(root).glob("*.json"))
        old = time.time() - 60
        os.utime(path, (old, old))
        assert store.load("k") is None
        assert not path.exists(), "过期文件应被清理"
        print("✓ 检查点 TTL 过期清理")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_corrupted_file_is_cleaned():
    store, root = _store()
    try:
        store.save("k", {"status": "ok"})
        path = next(Path(root).glob("*.json"))
        path.write_text("{not-json", encoding="utf-8")
        assert store.load("k") is None
        assert not path.exists(), "损坏文件应移出"
        print("✓ 损坏检查点按不存在处理")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_max_entries_prunes_oldest():
    store, root = _store(max_entries=2)
    try:
        for index in range(3):
            store.save(f"k{index}", {"status": "ok", "index": index})
        assert len(store) == 2, len(store)
        print("✓ 检查点条目上限淘汰")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_key_is_hashed_on_disk():
    store, root = _store()
    try:
        tricky = "../../escape:key"
        store.save(tricky, {"status": "ok"})
        files = list(Path(root).glob("*.json"))
        assert len(files) == 1
        assert "escape" not in files[0].name, "原始 key 不能直接作为文件名"
        assert store.load(tricky)["status"] == "ok"
        print("✓ 检查点键哈希落盘")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_save_load_roundtrip_and_step_merge()
    test_ttl_expiry_removes_file()
    test_corrupted_file_is_cleaned()
    test_max_entries_prunes_oldest()
    test_key_is_hashed_on_disk()
    print("\n全部通过")
