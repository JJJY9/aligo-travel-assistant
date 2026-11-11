"""日志脱敏测试（无需 API Key、不访问网络）。

运行：python tests/test_log_redact.py
"""
import logging
import os
import sys
import tempfile

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from context.long_term_memory import LongTermMemory
from utils.log_redact import redact_paths, redact_secret


def test_redact_secret_strips_userinfo_and_query():
    redacted = redact_secret(
        "connect postgresql://u:SE%CRET@db:5432/x?password=s&sslmode=require"
    )
    assert "SE%CRET" not in redacted
    assert "password" not in redacted
    assert "postgresql://***@db:5432/x" in redacted
    assert redact_secret("plain error") == "plain error"
    assert redact_secret("redis://127.0.0.1:6379/0") == "redis://127.0.0.1:6379/0"
    print("✓ redact_secret 去 userinfo 与查询串")


def test_preference_and_title_values_not_logged(caplog):
    """偏好值 / 会话标题是用户隐私内容：不进 INFO 日志。"""
    memory = LongTermMemory("u-logredact", tempfile.mkdtemp())
    with caplog.at_level(logging.INFO):
        memory.save_preference("memo", "敏感偏好内容ABC")
        memory.add_chat_message("user", "hi", "s1")
        memory.set_session_title("s1", "敏感标题内容XYZ")
    assert "敏感偏好内容ABC" not in caplog.text
    assert "敏感标题内容XYZ" not in caplog.text
    assert "Saved preference: memo" in caplog.text
    print("✓ 偏好值 / 标题不进日志")


def test_redact_paths_hides_internal_paths_and_urls():
    """技能错误文本回流给用户：路径 / 上游 URL / 凭据都要遮蔽，瞬时限关键词保留。"""
    raw = (
        r"加载失败: C:\Users\alice\.cache\model\1_Pooling\config.json "
        "与 /home/alice/.cache/huggingface/models--bge (Connection refused, timeout=8) "
        "https://huggingface.co/BAAI/bge/resolve/main/model.safetensors?token=SECRET"
    )
    cleaned = redact_paths(raw)
    assert "alice" not in cleaned
    assert "huggingface.co" not in cleaned
    assert "token=SECRET" not in cleaned
    assert "<路径>" in cleaned and "http(s)://***" in cleaned
    # 编排层据此判定瞬时错误并重试：关键词不能被抹掉
    assert "Connection refused" in cleaned and "timeout=8" in cleaned
    assert redact_paths("plain error") == "plain error"
    print("✓ redact_paths 遮蔽内部路径 / URL 且保留瞬时限关键词")


if __name__ == "__main__":
    test_redact_secret_strips_userinfo_and_query()
    test_redact_paths_hides_internal_paths_and_urls()
    print("caplog 用例需 pytest 运行")
