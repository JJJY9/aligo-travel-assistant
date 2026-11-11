"""配置读取与模型构造的环境变量测试（子进程隔离，无需 API Key / 网络）。

运行：python tests/test_config_env.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def _run_config(cwd: str, env_key: str) -> str:
    """在指定 cwd 下导入 config，返回 LLM_CONFIG['api_key'] 的 repr。"""
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(repr(config.LLM_CONFIG['api_key']))"
    )
    env = dict(os.environ)
    env.pop("LLM_API_KEY", None)
    if env_key is not None:
        env["LLM_API_KEY"] = env_key
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_api_key_is_stripped_and_blank_falls_back():
    tmp = tempfile.mkdtemp(prefix="chailv_cfg_")
    Path(tmp, "config_local.py").write_text(
        'API_KEY = "sk-local-valid"\n', encoding="utf-8"
    )
    # 尾随空格 / 换行必须被去掉（否则 httpx 拒绝 Authorization 头）
    assert _run_config(tmp, "  sk-env-key\n") == repr("sk-env-key")
    # 纯空白视为未设置：回退 config_local.py
    assert _run_config(tmp, "   ") == repr("sk-local-valid")
    # 完全未设置同样回退
    assert _run_config(tmp, None) == repr("sk-local-valid")
    print("✓ API Key strip / 空白回退 config_local")


def test_broken_config_local_import_is_not_swallowed():
    """config_local.py 自身 import 失败不能被当成「文件不存在」静默吞掉。"""
    tmp = tempfile.mkdtemp(prefix="chailv_cfg_")
    Path(tmp, "config_local.py").write_text(
        "import some_nonexistent_dependency_xyz\nAPI_KEY = 'sk-valid'\n",
        encoding="utf-8",
    )
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config"
    )
    env = dict(os.environ)
    env.pop("LLM_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp,
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "some_nonexistent_dependency_xyz" in (result.stderr or b"").decode(
        "utf-8", errors="replace"
    )
    print("✓ config_local 内部导入失败向上暴露")


def test_empty_env_model_and_base_url_fall_back():
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(config.LLM_CONFIG['model_name']);"
        "print(config.LLM_CONFIG['base_url'])"
    )
    env = dict(os.environ)
    env["LLM_MODEL_NAME"] = "  "
    env["LLM_BASE_URL"] = ""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "deepseek-chat", lines
    assert lines[1] == "https://api.deepseek.com", lines
    print("✓ 空 LLM_MODEL_NAME / LLM_BASE_URL 回退默认值")


def test_create_chat_model_max_retries_param():
    from config_agentscope import create_chat_model

    default_model = create_chat_model(api_key="sk-test")
    assert default_model.client.max_retries == 0, (
        "默认关闭 SDK 重试：应用层 retry_with_backoff 才是唯一重试入口，"
        "叠加会把一次调用放大成 12 个完整请求且用量不可见"
    )
    no_retry = create_chat_model(api_key="sk-test", max_retries=0)
    assert no_retry.client.max_retries == 0
    explicit = create_chat_model(api_key="sk-test", max_retries=1)
    assert explicit.client.max_retries == 1
    print("✓ create_chat_model 默认关闭 SDK 重试，支持显式覆盖")


def test_kb_import_rate_limit_env():
    """KB_IMPORT_RATE_LIMIT 控制知识库导入的独立限流桶（默认 1200）。"""
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(config.RATE_LIMIT_CONFIG['kb_import_requests'])"
    )
    env = dict(os.environ)
    env.pop("KB_IMPORT_RATE_LIMIT", None)
    default = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert default.returncode == 0, default.stderr
    assert default.stdout.strip() == "1200"
    env["KB_IMPORT_RATE_LIMIT"] = "7"
    configured = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert configured.returncode == 0, configured.stderr
    assert configured.stdout.strip() == "7"
    print("✓ KB_IMPORT_RATE_LIMIT 可配置（默认 1200）")


def test_read_rate_limit_env():
    """READ_RATE_LIMIT 控制面板数据读接口的独立限流桶（默认 600）。"""
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(config.RATE_LIMIT_CONFIG['read_requests'])"
    )
    env = dict(os.environ)
    env.pop("READ_RATE_LIMIT", None)
    default = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert default.returncode == 0, default.stderr
    assert default.stdout.strip() == "600"
    env["READ_RATE_LIMIT"] = "9"
    configured = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert configured.returncode == 0, configured.stderr
    assert configured.stdout.strip() == "9"
    print("✓ READ_RATE_LIMIT 可配置（默认 600）")


if __name__ == "__main__":
    test_api_key_is_stripped_and_blank_falls_back()
    test_broken_config_local_import_is_not_swallowed()
    test_empty_env_model_and_base_url_fall_back()
    test_create_chat_model_max_retries_param()
    test_kb_import_rate_limit_env()
    test_read_rate_limit_env()
    print("\n全部通过")
