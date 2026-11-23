"""执行检查点存储测试（v2.0，纯 Python）。

覆盖：落盘 / 读取、步骤合并、TTL 过期、损坏文件清理、条目上限、
目录/键哈希（不把原始 key 用作路径或写入文件）与清除。

运行：python tests/test_execution_checkpoint.py
"""
import hashlib
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
        assert state["key_hash"] == hashlib.sha256(b"u1:s1:req1").hexdigest()
        assert "key" not in state, "原始键（含用户 / 会话标识）不能写进检查点文件"
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


def test_raw_key_not_written_to_disk():
    store, root = _store()
    try:
        tricky = "user-秘密:session-1:turn-1"
        store.save(tricky, {"status": "ok"})
        raw = next(Path(root).glob("*.json")).read_text(encoding="utf-8")
        assert "秘密" not in raw, "用户标识不能落盘"
        assert "session-1" not in raw, "会话标识不能落盘"
        assert store.load(tricky)["status"] == "ok"
        print("✓ 检查点文件不写入原始键")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_default_root_follows_data_dir():
    old = os.environ.get("ALIGO_DATA_DIR")
    root = tempfile.mkdtemp(prefix="chailv_ckpt_env_")
    os.environ["ALIGO_DATA_DIR"] = root
    try:
        store = CheckpointStore()
        assert store.root == Path(root) / "runtime" / "checkpoints"
        assert store.root.is_dir()
        print("✓ 默认检查点目录跟随数据根目录")
    finally:
        if old is None:
            os.environ.pop("ALIGO_DATA_DIR", None)
        else:
            os.environ["ALIGO_DATA_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)


def test_init_prunes_expired_and_stale_temp():
    root = tempfile.mkdtemp(prefix="chailv_ckpt_init_")
    try:
        store = CheckpointStore(root=root, ttl_sec=10)
        store.save("old", {"status": "ok"})
        path = next(Path(root).glob("*.json"))
        stale_ts = time.time() - 3600
        os.utime(path, (stale_ts, stale_ts))
        stale_tmp = Path(root) / ".stale.json.abc.tmp"
        stale_tmp.write_text("{}", encoding="utf-8")
        os.utime(stale_tmp, (stale_ts, stale_ts))
        # 进程重启（同目录重建）：初始化就要清理过期检查点与崩溃遗留的
        # 临时文件，而不是等下一次写入才淘汰
        CheckpointStore(root=root, ttl_sec=10)
        assert not path.exists(), "过期检查点应在初始化时清理"
        assert not stale_tmp.exists(), "崩溃遗留的临时文件应在初始化时清理"
        print("✓ 初始化清理过期 / 临时检查点")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_scope_removes_only_matching_session():
    """会话删除按作用域清理：只删该 user:session 的检查点。"""
    store, root = _store()
    try:
        store.save("u1:s1:t1:d", {"status": "ok"}, scope="u1:s1")
        store.update_step("u1:s1:t2:d", "a", {"ok": True}, scope="u1:s1")
        store.save("u1:s2:t1:d", {"status": "ok"}, scope="u1:s2")
        store.save("legacy", {"status": "ok"})
        assert store.clear_scope("u1:s1") == 2
        assert store.load("u1:s1:t1:d") is None
        assert store.load("u1:s1:t2:d") is None
        assert store.load("u1:s2:t1:d") is not None
        assert store.load("legacy") is not None, "无作用域的旧检查点不应被误删"
        print("✓ 按会话作用域清理检查点")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_session_checkpoints_uses_default_store():
    """会话删除的清理入口跟随数据根目录（不要求调用方构造存储）。"""
    from utils.execution_checkpoint import clear_session_checkpoints

    old = os.environ.get("ALIGO_DATA_DIR")
    root = tempfile.mkdtemp(prefix="chailv_ckpt_clean_")
    os.environ["ALIGO_DATA_DIR"] = root
    try:
        store = CheckpointStore()
        store.save("u:s:t:d", {"status": "ok"}, scope="u:s")
        assert clear_session_checkpoints("u", "s") == 1
        assert store.load("u:s:t:d") is None
        print("✓ 会话删除清理入口使用默认存储")
    finally:
        if old is None:
            os.environ.pop("ALIGO_DATA_DIR", None)
        else:
            os.environ["ALIGO_DATA_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)


def test_default_store_cleans_stale_legacy_checkpoints():
    """默认根只清理旧 CWD 目录中已过 TTL 的检查点，近期文件保留。"""
    import utils.execution_checkpoint as checkpoint_module

    old_env = os.environ.get("ALIGO_DATA_DIR")
    old_cwd = os.getcwd()
    base = tempfile.mkdtemp(prefix="chailv_ckpt_legacy_")
    legacy = Path(base) / "data" / "runtime" / "checkpoints"
    legacy.mkdir(parents=True)
    stale = legacy / "stale.json"
    fresh = legacy / "fresh.json"
    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    old_ts = time.time() - 7200
    os.utime(stale, (old_ts, old_ts))
    old_package_root = checkpoint_module.package_root
    try:
        checkpoint_module.package_root = lambda: Path(base) / "package"
        os.environ["ALIGO_DATA_DIR"] = str(Path(base) / "newdata")
        os.chdir(base)
        CheckpointStore()
        assert not stale.exists(), "过期的旧目录检查点应被清理"
        assert fresh.exists(), "TTL 内的旧目录文件不能误删"
    finally:
        checkpoint_module.package_root = old_package_root
        os.chdir(old_cwd)
        if old_env is None:
            os.environ.pop("ALIGO_DATA_DIR", None)
        else:
            os.environ["ALIGO_DATA_DIR"] = old_env
        shutil.rmtree(base, ignore_errors=True)
    print("✓ 默认存储清理旧目录过期检查点")


def test_explicit_root_does_not_clean_legacy_checkpoints():
    """显式 root（测试 / 自定义部署）不应触碰 CWD 下的旧目录。"""
    old_cwd = os.getcwd()
    base = tempfile.mkdtemp(prefix="chailv_ckpt_explicit_")
    legacy = Path(base) / "data" / "runtime" / "checkpoints"
    legacy.mkdir(parents=True)
    old = legacy / "old.json"
    old.write_text("{}", encoding="utf-8")
    old_ts = time.time() - 7200
    os.utime(old, (old_ts, old_ts))
    try:
        os.chdir(base)
        CheckpointStore(root=Path(base) / "explicit")
        assert old.exists(), "显式 root 不得清理旧目录"
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(base, ignore_errors=True)
    print("✓ 显式 root 不清理旧目录")


if __name__ == "__main__":
    import _stdio

    _stdio.ensure_utf8()
    test_save_load_roundtrip_and_step_merge()
    test_ttl_expiry_removes_file()
    test_corrupted_file_is_cleaned()
    test_max_entries_prunes_oldest()
    test_key_is_hashed_on_disk()
    test_raw_key_not_written_to_disk()
    test_default_root_follows_data_dir()
    test_init_prunes_expired_and_stale_temp()
    test_clear_scope_removes_only_matching_session()
    test_clear_session_checkpoints_uses_default_store()
    test_default_store_cleans_stale_legacy_checkpoints()
    test_explicit_root_does_not_clean_legacy_checkpoints()
    print("\n全部通过")
