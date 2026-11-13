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


def test_storage_urls_strip_whitespace_and_bom():
    """REDIS_URL / DATABASE_URL 去空白与 BOM；非法额度告警并钳到 1。"""
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(repr(config.STORAGE_CONFIG['redis_url']));"
        "print(repr(config.STORAGE_CONFIG['postgres_dsn']));"
        "print(config.RATE_LIMIT_CONFIG['kb_requests']);"
        "print(config.RATE_LIMIT_CONFIG['chat_requests'])"
    )
    env = dict(os.environ)
    env["REDIS_URL"] = "  redis://localhost:6379/0\n"
    env["DATABASE_URL"] = "\ufeffpostgresql://u:p@h/db\n"
    env["KB_RATE_LIMIT"] = "0"
    env["CHAT_RATE_LIMIT"] = "2,000"
    # Windows \u975e\u63a7\u5236\u53f0\u7ba1\u9053\u9ed8\u8ba4\u7528 ANSI \u4ee3\u7801\u9875\u5199 stderr\uff08GBK\uff09\uff0c\u4e0b\u9762\u6309 UTF-8
    # \u89e3\u7801\u65ad\u8a00\u4e2d\u6587\u544a\u8b66\u4f1a\u5931\u8d25\uff1b\u5f3a\u5236\u5b50\u8fdb\u7a0b stdio \u7f16\u7801\uff0c\u4fdd\u8bc1\u6d4b\u8bd5\u8de8\u5e73\u53f0\u786e\u5b9a
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().split("\n")
    assert lines[0] == repr("redis://localhost:6379/0"), lines
    assert lines[1] == repr("postgresql://u:p@h/db"), lines
    assert lines[2] == "1", "0 额度应被钳到 1"
    assert lines[3] == "30", "非法整数回退默认值"
    assert "2,000" in result.stderr and "不是整数" in result.stderr, result.stderr
    print("✓ 存储 URL 去空白 / BOM，非法额度告警并钳制")


def test_llm_request_timeout_env():
    """模型 read 超时默认 120s，可用 LLM_REQUEST_TIMEOUT_SEC 覆盖。"""
    script = (
        "import sys;"
        f"sys.path.append(r'{project_root}');"
        "import config;"
        "print(config.SYSTEM_CONFIG['timeout'])"
    )
    env = dict(os.environ)
    env.pop("LLM_REQUEST_TIMEOUT_SEC", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "120", result.stdout

    env["LLM_REQUEST_TIMEOUT_SEC"] = "45"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "45", result.stdout
    print("✓ 模型超时默认 120s 且可用环境变量覆盖")


def test_config_local_loaded_without_cwd_on_sys_path():
    """console script / wheel 启动（sys.path 不含 CWD）仍加载运行目录的 config_local.py。"""
    tmp = tempfile.mkdtemp(prefix="chailv_cfg_")
    Path(tmp, "config_local.py").write_text(
        'API_KEY = "sk-file-local"\n', encoding="utf-8"
    )
    script = (
        "import importlib.util, os, sys;"
        "cwd = os.getcwd();"
        "sys.path = [p for p in sys.path if p not in ('', cwd)];"
        f"spec = importlib.util.spec_from_file_location('config', r'{project_root}/config.py');"
        "config = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(config);"
        "print(repr(config.LLM_CONFIG['api_key']))"
    )
    env = dict(os.environ)
    env.pop("LLM_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr("sk-file-local"), result.stdout
    print("✓ sys.path 无 CWD 时按显式路径加载 config_local.py")


if __name__ == "__main__":
    test_api_key_is_stripped_and_blank_falls_back()
    test_storage_urls_strip_whitespace_and_bom()
    test_llm_request_timeout_env()
    test_config_local_loaded_without_cwd_on_sys_path()
    test_broken_config_local_import_is_not_swallowed()
    test_empty_env_model_and_base_url_fall_back()
    test_create_chat_model_max_retries_param()
    test_kb_import_rate_limit_env()
    test_read_rate_limit_env()
    print("\n全部通过")
