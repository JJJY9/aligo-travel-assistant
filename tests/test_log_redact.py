"""日志脱敏测试（无需 API Key、不访问网络）。

运行：python tests/test_log_redact.py
"""
import logging
import os
import sys
import tempfile
import time

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


def test_redact_secret_masks_bare_api_keys():
    """网关错误体原样引用请求密钥（Incorrect API key provided: sk-…）。"""
    for prefix in ("sk", "pk", "rk", "ak"):
        key = f"{prefix}-live0123456789abcdef"
        redacted = redact_secret(f"Error: Incorrect API key provided: {key}")
        assert key not in redacted, redacted
        assert "***" in redacted, redacted
    # 正常文本与短横线单词不受影响
    assert redact_secret("risk-based approach") == "risk-based approach"
    assert redact_secret("任务 sk-learn 流程") == "任务 sk-learn 流程"
    print("✓ redact_secret 遮蔽裸 API Key")


def test_redact_secret_masks_auth_headers_and_colon_forms():
    """Authorization 头、JSON / dict repr / YAML 冒号形态的凭据都要遮蔽。"""
    header = redact_secret(
        "401 Unauthorized: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    )
    assert "eyJhbGciOiJIUzI1NiJ9" not in header, header
    assert "Authorization: Bearer ***" in header, header

    basic = redact_secret("Proxy-Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in basic, basic

    for text, secret in (
        ('{"api_key": "abcd1234efgh5678"}', "abcd1234efgh5678"),
        ("'api_key': 'abcd1234efgh5678'", "abcd1234efgh5678"),
        ("api-key: 0123456789abcdef", "0123456789abcdef"),
        ("password: hunter2", "hunter2"),
    ):
        redacted = redact_secret(text)
        assert secret not in redacted, (text, redacted)

    # 无关字段与自然语言不被误伤
    assert redact_secret("max_tokens: 100") == "max_tokens: 100"
    assert redact_secret("risk-based approach: ok") == "risk-based approach: ok"
    assert redact_secret("任务 sk-learn 流程") == "任务 sk-learn 流程"
    print("✓ redact_secret 覆盖 Authorization 头与冒号形态凭据")


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


def test_redact_secret_masks_libpq_quoted_tokens():
    """psycopg 解析 DSN 失败时把参数值整段回显在引号里（含未编码密码）。"""
    spaced = (
        'unexpected spaces found in "pa ss", '
        "use percent-encoded spaces (%20) instead"
    )
    cleaned = redact_secret(spaced)
    assert "pa ss" not in cleaned, cleaned
    assert "unexpected spaces found in" in cleaned, cleaned
    assert "percent-encoded" in cleaned, cleaned

    missing_eq = 'missing "=" after "aligo:pa ss" in connection info string'
    cleaned = redact_secret(missing_eq)
    assert "pa ss" not in cleaned, cleaned
    assert 'missing "=" after' in cleaned, cleaned
    print("✓ redact_secret 遮蔽 libpq 引号内 DSN 参数")


def test_redact_paths_covers_macos_and_nix_roots():
    """macOS 框架 / 外置卷与 NixOS 的常见安装根同样属于本机路径。"""
    for raw in (
        "/Library/Frameworks/Python.framework/Versions/3.12/lib/"
        "python3.12/site-packages/x.py",
        "/Volumes/Data/models/bge-small-zh-v1.5/config.json",
        "/nix/store/abcd1234-source/utils/x.py",
    ):
        cleaned = redact_local_paths(raw)
        assert cleaned == "<路径>", (raw, cleaned)
        quoted = redact_local_paths(f"load '{raw}' failed")
        assert quoted == "load '<路径>' failed", (raw, quoted)
    print("✓ macOS /Library、/Volumes 与 /nix/store 路径被遮蔽")


def test_redact_secret_url_scheme_is_bounded_for_linear_time():
    """scheme 超过 RFC 事实上限时不按 URL 处理；长文本脱敏保持近线性。"""
    long_scheme = "x" * 60 + "://u:SECRET123456@host/db"
    cleaned = redact_secret(long_scheme)
    assert "SECRET123456" not in cleaned, cleaned

    overlong = "x" * 80 + "://u:SECRET123456@host/db"
    cleaned = redact_secret(overlong)
    assert cleaned.startswith("x" * 80), "超长 scheme 不应被当作 URL 前缀"
    assert "SECRET123456" not in cleaned, cleaned

    # 无界 scheme 下 64k 无分隔文本是 O(n²)（数十秒）；有界后应为毫秒级
    started = time.perf_counter()
    redact_secret("a" * 200_000)
    assert time.perf_counter() - started < 5.0
    print("✓ URL scheme 限长，长文本脱敏近线性")


_DIRECT_RUN_SKIP: dict[str, str] = {
}

if __name__ == "__main__":
    import _stdio
    import _tmp_registry

    _stdio.ensure_utf8()
    _tmp_registry.install()
    test_redact_secret_strips_userinfo_and_query()
    test_redact_secret_keeps_url_punctuation_passwords_out()
    test_redact_secret_masks_bare_api_keys()
    test_redact_secret_masks_auth_headers_and_colon_forms()
    test_redact_secret_masks_libpq_quoted_tokens()
    test_redact_secret_url_scheme_is_bounded_for_linear_time()
    test_redact_paths_hides_internal_paths_and_urls()
    test_redact_paths_covers_macos_and_nix_roots()
    test_redact_local_paths_keeps_urls_intact()
    test_redact_local_paths_covers_paths_with_spaces()
    test_redaction_keeps_url_after_spacey_cjk_path()
    # caplog 用例用共享替身真实执行：日志脱敏断言不能跳过（假绿）
    import _caplog_shim

    _shim = _caplog_shim.CaplogShim()
    try:
        test_preference_and_title_values_not_logged(_shim)
    finally:
        _shim.close()
    if _DIRECT_RUN_SKIP:
        print(f"直跑未纳入 {len(_DIRECT_RUN_SKIP)} 个用例（需 pytest 专用夹具或真实服务）")
