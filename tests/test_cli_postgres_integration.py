"""
CLI 会话 / 行程命令 + PostgreSQL 集成测试（需要 TEST_POSTGRES_DSN）

CI 中通过 service 容器自动启用；未提供 DSN 时跳过。验证 CLI 在 PostgreSQL
后端下的会话列表、归档 / 恢复、单会话导出与行程 CSV 导出。

运行：
    TEST_POSTGRES_DSN=postgresql://aligo:aligo@localhost:5432/aligo \\
        pytest tests/test_cli_postgres_integration.py -q
"""
import contextlib
import io
import os
import sys
import tempfile
import uuid

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import cli
from context import storage
from context.postgres_long_term_memory import PostgresLongTermMemory

TEST_POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN, reason="未设置 TEST_POSTGRES_DSN"
)


def _capture(func, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(*args, **kwargs)
    return code, buf.getvalue()


def test_cli_commands_with_postgres(monkeypatch):
    user_id = f"cli-pg-{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(storage.STORAGE_CONFIG, "postgres_dsn", TEST_POSTGRES_DSN)
    storage.reset_failure_cache()
    try:
        try:
            probe = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        except Exception as exc:  # noqa: BLE001 - 测试环境无 PG 时跳过
            pytest.skip(f"PostgreSQL 不可用: {exc}")
        probe.close()

        store = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
        try:
            store.add_chat_message("user", "PG CLI 会话问题", "s1")
            store.add_chat_message("assistant", "回答", "s1")
            store.save_trip_history(
                {
                    "origin": "北京",
                    "destination": "上海",
                    "start_date": "2026-12-05",
                    "end_date": "2026-12-07",
                    "purpose": "出差",
                }
            )
        finally:
            store.close()

        try:
            code, out = _capture(cli.run_sessions_list_standalone, user_id)
            assert code == 0 and "s1" in out and "PG CLI 会话问题" in out

            code, out = _capture(
                cli.run_session_archive_standalone, user_id, "s1", True
            )
            assert code == 0 and "已归档会话 s1" in out
            code, out = _capture(
                cli.run_sessions_list_standalone, user_id, archived_only=True
            )
            assert code == 0 and "s1" in out and "[归档]" in out

            target = os.path.join(tempfile.mkdtemp(), "session.md")
            code, out = _capture(
                cli.run_session_export_standalone, user_id, "s1", target
            )
            assert code == 0
            with open(target, encoding="utf-8") as handle:
                assert "PG CLI 会话问题" in handle.read()

            code, out = _capture(
                cli.run_trips_export_standalone, user_id, None, True
            )
            assert code == 0 and "北京,上海" in out
            print("✓ CLI 会话 / 行程命令在 PostgreSQL 后端下正常")
        finally:
            cleanup = PostgresLongTermMemory(user_id, TEST_POSTGRES_DSN)
            try:
                cleanup.delete_all()
            finally:
                cleanup.close()
    finally:
        storage.reset_failure_cache()


def test_postgres_trip_dedupe_empty_end_date():
    """end_date 为空串与 NULL 同为「无返程」：重复保存更新而不是新增。"""
    memory = PostgresLongTermMemory(
        f"trips-empty-{uuid.uuid4().hex[:8]}", TEST_POSTGRES_DSN
    )
    try:
        first = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "",
                "purpose": "出差",
            }
        )
        second = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "上海",
                "start_date": "2026-12-01",
                "end_date": "",
                "purpose": "会议",
            }
        )
        assert second == first, "空串返程应同样按出发日归一后去重"
        trips = memory.get_trip_history(limit=None)
        assert len(trips) == 1 and trips[0]["purpose"] == "会议"

        other = memory.save_trip_history(
            {
                "origin": "北京",
                "destination": "杭州",
                "start_date": "2026-12-05",
                "end_date": "2026-12-06",
                "purpose": "出差",
            }
        )
        with pytest.raises(ValueError, match="已存在相同行程"):
            memory.update_trip(
                other,
                {
                    "destination": "上海",
                    "start_date": "2026-12-01",
                    "end_date": "",
                },
            )
        print("✓ PostgreSQL 空串返程按出发日归一（保存去重 / 更新冲突）")
    finally:
        memory.delete_all()
        memory.close()


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    try:
        test_cli_commands_with_postgres(mp)
    finally:
        mp.undo()
    test_postgres_trip_dedupe_empty_end_date()
    print("\n全部通过")
