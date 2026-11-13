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
from utils.log_redact import redact_local_paths, redact_paths, redact_secret


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


def test_redact_secret_keeps_url_punctuation_passwords_out():
    """密码含 / : @ 等 URL 标点（未编码）时也必须整段遮蔽，只保留主机便于排查。"""
    bare = redact_secret("aligo:pa/ss@127.0.0.1:5432/aligo")
    assert "pa/ss" not in bare, bare
    assert "127.0.0.1:5432/aligo" in bare, bare

    url = redact_secret("postgresql://aligo:sec/ret@db.internal/aligo")
    assert "sec/ret" not in url, url
    assert "postgresql://***@db.internal/aligo" in url, url

    multi = redact_secret("postgresql://aligo:p@ss@db:5432/x")
    assert "p@ss" not in multi and "ss@" not in multi, multi
    assert "db:5432" in multi, multi

    colon = redact_secret("redis://:pa:ss@cache:6379/0")
    assert "pa:ss" not in colon, colon
    assert "cache:6379" in colon, colon
    print("✓ redact_secret 遮蔽含 URL 标点 / 多个 @ 的密码")


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
    # 容器 / 构建机常用根（/app /usr /build …）同样不回显
    container = redact_paths(
        "依赖 /app/.venv/lib/python3.12/site-packages/x.py "
        "与 /usr/lib/python3/x.py 以及 /build/workspace/rag_knowledge"
    )
    assert "/app/" not in container and "/usr/" not in container
    assert "/build/" not in container and "<路径>" in container
    # 正斜杠写法的 Windows 盘符路径（D:/…）按同一规则遮蔽
    forward = redact_paths("技能目录 D:/Code/chailv/skills 与 E:/work/x.py")
    assert "D:/Code" not in forward and "E:/work" not in forward
    assert "<路径>" in forward
    print("✓ redact_paths 遮蔽内部路径 / URL 且保留瞬时限关键词")


def test_redact_local_paths_keeps_urls_intact():
    """只遮蔽路径：URL 的协议与主机保留（诊断报告需要），凭据交给 redact_secret。"""
    raw = (
        r"模型目录 C:\Users\alice\.cache\huggingface 与 /usr/share/model "
        "网关 https://llm.example/v1/chat"
    )
    cleaned = redact_local_paths(raw)
    assert "alice" not in cleaned and "/usr/share" not in cleaned
    assert cleaned.count("<路径>") == 2
    assert "https://llm.example/v1/chat" in cleaned, cleaned
    print("✓ redact_local_paths 只遮蔽路径、保留 URL")


def test_redact_local_paths_covers_paths_with_spaces():
    """含空格的 Windows / macOS 路径不能只遮到第一个空格、留下目录尾部。"""
    cases = {
        r"open C:\Program Files\Python312\python.exe failed": "open <路径> failed",
        r"open C:\Program Files (x86)\Common Files\a.dll failed": "open <路径> failed",
        r"open C:\Users\John Doe\models\bge\config.json failed": "open <路径> failed",
        r"open \\server\share\My Documents\a.txt failed": "open <路径> failed",
        "load 'C:\\Users\\John Doe\\My File.txt' failed": "load '<路径>' failed",
        "load '/Users/John Doe/model/x' failed": "load '<路径>' failed",
    }
    for raw, expected in cases.items():
        cleaned = redact_local_paths(raw)
        assert cleaned == expected, (raw, cleaned)

    # 自然语言句子不能被跨空格吞掉：瞬时限关键词必须保留
    prose = redact_paths(r"open C:\Program Files is missing (Connection refused)")
    assert "Connection refused" in prose and "is missing" in prose, prose
    assert "C:" not in prose, prose
    print("✓ 含空格路径整体遮蔽且不吞掉错误关键词")

def test_redaction_keeps_url_after_spacey_cjk_path():
    """含空格续段规则不能把紧跟在路径后的 URL scheme 当成路径片段吃掉。"""
    raw = (
        r"加载失败: C:\Users\alice\.cache\model 与 "
        "https://huggingface.co/BAAI/bge (Connection refused)"
    )
    cleaned = redact_paths(raw)
    assert "alice" not in cleaned, cleaned
    assert "huggingface.co" not in cleaned and "http(s)://***" in cleaned, cleaned
    assert "<路径>" in cleaned and "Connection refused" in cleaned
    print("✓ 路径后的 URL 仍被整体遮蔽")

if __name__ == "__main__":
    test_redact_secret_strips_userinfo_and_query()
    test_redact_secret_keeps_url_punctuation_passwords_out()
    test_redact_paths_hides_internal_paths_and_urls()
    test_redact_local_paths_keeps_urls_intact()
    test_redact_local_paths_covers_paths_with_spaces()
    test_redaction_keeps_url_after_spacey_cjk_path()
    print("caplog 用例需 pytest 运行")
