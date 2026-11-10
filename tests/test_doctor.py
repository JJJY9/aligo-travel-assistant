"""
doctor 环境自检测试（无需 API Key、不访问网络）

运行：python tests/test_doctor.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    check_dependencies,
    check_knowledge_base,
    check_model_files,
    check_python,
    check_skills,
    check_storage,
    check_version_consistency,
    run_doctor,
)


def test_storage_defaults(monkeypatch):
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    storage_module.reset_failure_cache()
    try:
        result = check_storage()
    finally:
        storage_module.reset_failure_cache()
    assert result.status == STATUS_OK, result.detail
    assert "进程内" in result.detail and "JSON" in result.detail
    print("✓ 存储后端未配置 → 本地默认实现")


def test_storage_unreachable_warns(monkeypatch):
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    storage_module.reset_failure_cache()
    try:
        result = check_storage()
    finally:
        storage_module.reset_failure_cache()
    assert result.status == STATUS_WARN
    assert "Redis" in result.detail and "回退" in result.detail
    print("✓ Redis 不可达 → 警告并提示回退")


def test_storage_runtime_degraded_warns(monkeypatch):
    """Redis 运行中故障回退进程内时，doctor 要明确提示而不是报告可用。"""
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(
        storage_module,
        "describe_backends",
        lambda: {
            "short_term": "redis",
            "long_term": "json",
            "short_term_configured": True,
            "long_term_configured": False,
            "short_term_degraded": True,
        },
    )
    result = check_storage()
    assert result.status == STATUS_WARN
    assert "回退进程内" in result.detail
    print("✓ Redis 运行中故障 → doctor 警告回退")


def test_python_ok():
    assert check_python().status == STATUS_OK
    print("✓ Python 版本检查")


def test_version_compare_normalizes_pep440():
    """版本比较按 PEP 440：1.6.0-rc1 与 1.6.0rc1、1.6 与 1.6.0 视为一致。"""
    from utils.doctor import _versions_equal

    assert _versions_equal("1.6.0-rc1", "1.6.0rc1") is True
    assert _versions_equal("1.6", "1.6.0") is True
    assert _versions_equal("1.6.1", "1.6.0") is False
    assert _versions_equal("自定义版本", "自定义版本") is True
    assert _versions_equal("自定义版本", "1.6.0") is False
    print("✓ 版本一致性按 PEP 440 规范化比较")


def test_model_dir_follows_rag_config(monkeypatch):
    """doctor 的嵌入模型目录与 RAG 同一口径（技能根上一级 + RAG_CONFIG）。"""
    from config import RAG_CONFIG
    from utils.doctor import _default_model_dir
    from utils.skill_paths import resolve_skills_root

    # 用与 doctor 硬编码 fallback 不同的值：删掉「读取 RAG_CONFIG」分支
    # 后回退到默认目录，断言必然失败
    monkeypatch.setitem(
        RAG_CONFIG, "embedding_model", "data/models/custom-test-embed"
    )
    expected = Path(resolve_skills_root()).parent.parent / "data/models/custom-test-embed"
    assert _default_model_dir() == expected
    print("✓ 嵌入模型目录与 RAG 技能同一解析口径")


def test_version_consistency(monkeypatch):
    from utils import version as version_module

    assert check_version_consistency().status == STATUS_OK
    monkeypatch.setattr(version_module, "APP_VERSION", "0.0.0")
    result = check_version_consistency()
    assert result.status == STATUS_WARN
    assert "0.0.0" in result.detail and "pyproject" in result.detail
    print("✓ 版本一致性检查（不一致时警告）")


def test_version_check_skips_without_pyproject(monkeypatch, tmp_path):
    import importlib.metadata

    from utils import doctor as doctor_module

    monkeypatch.setattr(doctor_module, "PROJECT_ROOT", tmp_path)

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    result = check_version_consistency()
    assert result.status == STATUS_SKIP
    print("✓ 无 pyproject / 安装元数据 / 静态资源时跳过版本一致性检查")


def test_version_check_wheel_mode_checks_static(monkeypatch, tmp_path):
    """wheel 安装没有 pyproject：仍校验 dist-info 元数据与静态资源版本。"""
    import importlib.metadata

    from utils import doctor as doctor_module
    from utils.version import APP_VERSION

    static = tmp_path / "webapp" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text(
        f'<script src="/static/app.js?v={APP_VERSION}"></script>', encoding="utf-8"
    )
    monkeypatch.setattr(doctor_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: APP_VERSION)

    result = check_version_consistency()
    assert result.status == STATUS_OK
    assert "wheel 元数据" in result.detail and "静态资源" in result.detail
    assert "pyproject" not in result.detail

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.0")
    result = check_version_consistency()
    assert result.status == STATUS_WARN
    assert "wheel 元数据为 0.0.0" in result.detail
    print("✓ wheel 模式校验安装元数据与静态资源版本")


def test_version_check_handles_non_utf8_files(monkeypatch, tmp_path):
    from utils import doctor as doctor_module

    (tmp_path / "pyproject.toml").write_bytes(b'version = "1.4.1"\n# \xd6\xd0\xce\xc4\n')
    monkeypatch.setattr(doctor_module, "PROJECT_ROOT", tmp_path)
    result = check_version_consistency()
    assert result.status == STATUS_WARN
    assert "无法读取 pyproject.toml" in result.detail
    print("✓ 非 UTF-8 文件不会让 doctor 崩溃")


def test_version_check_detects_static_version_mismatch(monkeypatch, tmp_path):
    from utils import doctor as doctor_module
    from utils.version import APP_VERSION

    (tmp_path / "pyproject.toml").write_text(
        f'version = "{APP_VERSION}"', encoding="utf-8"
    )
    static = tmp_path / "webapp" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text(
        '<script src="/static/app.js?v=1.3.0"></script>', encoding="utf-8"
    )
    monkeypatch.setattr(doctor_module, "PROJECT_ROOT", tmp_path)
    result = check_version_consistency()
    assert result.status == STATUS_WARN
    assert "1.3.0" in result.detail and "静态资源" in result.detail
    print("✓ index.html 静态资源版本不一致时警告")


def test_skills_check_reports_repo_skills():
    result = check_skills()
    assert result.status == STATUS_OK, result.detail
    assert "个技能" in result.detail
    print("✓ 技能目录检查（源码运行）")


def test_skills_check_missing_root(monkeypatch):
    from utils import skill_paths

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(
            skill_paths, "resolve_skills_root", lambda: Path(tmp) / "missing"
        )
        result = check_skills()
    assert result.status == STATUS_FAIL
    assert "ALIGO_SKILLS_DIR" in result.detail
    print("✓ 技能目录缺失 → 失败并提示 ALIGO_SKILLS_DIR")


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


def test_knowledge_base_reason_variants(monkeypatch):
    """知识库检查的各类 reason / 空集合 / 正常分支。"""
    from utils import knowledge_stats

    def _status(reason=None, documents=None):
        data = {"collection": "c", "available": reason is None}
        if reason is not None:
            data["reason"] = reason
        if documents is not None:
            data["documents"] = documents
        return data

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda path=None: _status("集合不存在"),
    )
    result = check_knowledge_base(Path("x"))
    assert result.status == STATUS_WARN and "缺少知识集合" in result.detail

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda path=None: _status("连接被拒绝" * 60),
    )
    result = check_knowledge_base(Path("x"))
    assert result.status == STATUS_WARN
    assert result.detail.startswith("无法读取知识库:")
    assert len(result.detail) <= len("无法读取知识库: ") + 120

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda path=None: _status(None, 0),
    )
    result = check_knowledge_base(Path("x"))
    assert result.status == STATUS_WARN and "知识集合为空" in result.detail

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda path=None: _status(None, 3),
    )
    result = check_knowledge_base(Path("x"))
    assert result.status == STATUS_OK and "3 个知识片段" in result.detail


def test_redact_secret_masks_passwords():
    """doctor 输出里的连接串密码必须被遮蔽（psycopg 异常会回显 DSN）。"""
    from utils.doctor import _redact_secret

    masked = _redact_secret("postgresql://aligo:s3cret@db:5432/aligo")
    assert "s3cret" not in masked and "://***@" in masked
    # 缺 scheme 的粘贴错误（DATABASE_URL 少了 postgresql://）同样遮蔽
    bare = _redact_secret("missing \"=\" after \"aligo:S3cretPw@db.internal:5432/aligo\"")
    assert "S3cretPw" not in bare, bare
    bare_only_pw = _redact_secret("redis://:S3cret@127.0.0.1:6379/0")
    assert "S3cret" not in bare_only_pw
    keyword = _redact_secret("host=db password=s3cret user=aligo")
    assert "s3cret" not in keyword and "password=***" in keyword
    token = _redact_secret("call https://gw/v1?api_key=SECRET&x=1")
    assert "SECRET" not in token and "?x=1" not in token
    print("✓ doctor 遮蔽连接串密码（URI / 无 scheme / 关键字 / 查询串）")


def test_check_config_key_source_strips_env(monkeypatch):
    """LLM_API_KEY 为纯空白时按回退 config_local.py 报告来源。"""
    from config import LLM_CONFIG
    from utils import doctor as doctor_module

    monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")
    monkeypatch.setenv("LLM_API_KEY", "   ")
    result = doctor_module.check_config()
    assert "config_local.py" in result.detail, result.detail
    monkeypatch.setenv("LLM_API_KEY", "  sk-env  ")
    result = doctor_module.check_config()
    assert "环境变量" in result.detail, result.detail
    print("✓ check_config 按 strip 后判断密钥来源")


def test_parse_doctor_args_case_insensitive():
    """doctor / weather 的 flag 与其他命令同一口径：大小写不敏感。"""
    from cli import _parse_doctor_args

    assert _parse_doctor_args(["--JSON", "--Offline"]) == (True, False)
    assert _parse_doctor_args(["--NO-NETWORK"]) == (False, False)
    assert _parse_doctor_args(["--bogus"]) is None
    print("✓ doctor 参数大小写不敏感")


def test_model_files_accepts_huggingface_id(monkeypatch):
    """配置为 HuggingFace 模型 ID（BAAI/xxx）时不误报「未下载」。"""
    from config import RAG_CONFIG
    from utils import doctor as doctor_module

    monkeypatch.setitem(RAG_CONFIG, "embedding_model", "BAAI/bge-small-zh-v1.5")
    result = doctor_module.check_model_files()
    assert result.status == STATUS_OK, result.detail
    assert "HuggingFace" in result.detail
    print("✓ 嵌入模型 HF ID 不误报未下载")


def test_knowledge_base_dimension_mismatch_warns(monkeypatch):
    """集合维度与当前嵌入模型不一致时报警告（检索会静默返回空）。"""
    from utils import knowledge_stats

    monkeypatch.setattr(
        knowledge_stats,
        "knowledge_status",
        lambda path=None: {
            "collection": "c",
            "available": True,
            "documents": 3,
            "dimension": 1024,
            "dimension_mismatch": True,
        },
    )
    result = check_knowledge_base(Path("x"))
    assert result.status == STATUS_WARN
    assert "不一致" in result.detail and "1024" in result.detail
    print("✓ 知识库维度不一致 → 警告")


def test_storage_probe_sets_statement_timeout(monkeypatch):
    """PostgreSQL 自检探测必须给 DDL / 查询设 statement_timeout。"""
    from config import STORAGE_CONFIG
    from context import storage as storage_module
    from context import postgres_long_term_memory as pg_module
    from utils import doctor as doctor_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "postgresql://u:p@h/db")
    monkeypatch.setattr(
        storage_module,
        "describe_backends",
        lambda: {
            "short_term": "memory",
            "long_term": "postgres",
            "short_term_configured": False,
            "long_term_configured": True,
            "short_term_degraded": False,
        },
    )
    captured = {}

    class _Probe:
        def __init__(self, user_id, dsn, statement_timeout_ms=None):
            captured["timeout"] = statement_timeout_ms

        def close(self):
            pass

    monkeypatch.setattr(pg_module, "PostgresLongTermMemory", _Probe)
    result = doctor_module.check_storage()
    assert captured.get("timeout") == 3000
    assert result.status == STATUS_OK, result.detail
    print("✓ PostgreSQL 探测设置语句超时")


def test_config_and_dependencies_branches(monkeypatch):
    """缺少 API Key / 核心依赖 import 失败时的失败分支。"""
    from config import LLM_CONFIG
    from utils import doctor as doctor_module

    monkeypatch.setitem(LLM_CONFIG, "api_key", "")
    result = doctor_module.check_config()
    assert result.status == STATUS_FAIL and "未配置 API Key" in result.detail

    real_import = doctor_module.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "agentscope":
            raise ImportError("broken install")
        return real_import(name, *args, **kwargs)

    # find_spec 只证明找得到包：装坏（缺子依赖 / 二进制不匹配）必须实际
    # import 才发现，这里直接让 agentscope 导入失败
    monkeypatch.setattr(doctor_module.importlib, "import_module", fake_import)
    results = doctor_module.check_dependencies()
    core = next(item for item in results if item.name == "核心依赖")
    assert core.status == STATUS_FAIL and "agentscope" in core.detail


def test_llm_connectivity_branches(monkeypatch):
    """无密钥跳过 / 连通 / 失败三种状态。"""
    from config import LLM_CONFIG
    from utils import doctor as doctor_module
    from utils import llm_resilience

    monkeypatch.setitem(LLM_CONFIG, "api_key", "")
    skipped = asyncio.run(doctor_module.check_llm_connectivity())
    assert skipped.status == STATUS_SKIP and "未配置 API Key" in skipped.detail

    monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")

    async def fake_health(**kwargs):
        return True, "ok"

    monkeypatch.setattr(llm_resilience, "run_health_check", fake_health)
    ok = asyncio.run(doctor_module.check_llm_connectivity())
    assert ok.status == STATUS_OK and "可用" in ok.detail

    async def fake_fail(**kwargs):
        return False, "连接超时" * 40

    monkeypatch.setattr(llm_resilience, "run_health_check", fake_fail)
    failed = asyncio.run(doctor_module.check_llm_connectivity())
    assert failed.status == STATUS_FAIL
    assert len(failed.detail) <= 150


def test_report_and_summary_statuses():
    """汇总结论与表格覆盖全部状态（含未知状态）。"""
    from utils.doctor import (
        CheckResult,
        build_report_table,
        has_failures,
        summarize,
    )

    ok = CheckResult("A", STATUS_OK, "d")
    warn = CheckResult("B", STATUS_WARN, "d")
    fail = CheckResult("C", STATUS_FAIL, "d")
    skip = CheckResult("D", STATUS_SKIP, "d")
    unknown = CheckResult("E", "weird", "d")

    assert has_failures([ok, warn, skip]) is False
    assert has_failures([ok, fail]) is True
    assert summarize([ok, skip]) == ("✓ 环境检查全部通过", "green")
    text, style = summarize([ok, warn])
    assert style == "yellow" and "警告" in text
    text, style = summarize([fail, warn])
    assert style == "red" and "必须处理" in text

    table = build_report_table([ok, warn, fail, skip, unknown])
    assert table.row_count == 5


def test_run_doctor_without_network():
    results = asyncio.run(run_doctor(include_network=False))
    names = [r.name for r in results]
    assert "Python 版本" in names
    assert "核心依赖" in names
    assert "大模型配置" in names
    assert "版本一致性" in names
    assert "技能目录" in names
    assert "记忆存储" in names
    assert "嵌入模型文件" in names
    assert "RAG 知识库" in names
    assert "LLM 连通性" in names
    print(f"✓ run_doctor 汇总正常（共 {len(results)} 项，跳过网络）")


def test_doctor_json_output(monkeypatch):
    """doctor --json 输出机器可读结果；存在 fail 时退出码 1。"""
    import cli
    from utils import doctor as doctor_module
    from utils.doctor import CheckResult

    async def fake_ok(include_network=True):
        return [CheckResult("Python 版本", STATUS_OK, "3.12.10")]

    monkeypatch.setattr(doctor_module, "run_doctor", fake_ok)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_doctor_standalone(as_json=True)
    payload = json.loads(buf.getvalue())
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["checks"] == [
        {"name": "Python 版本", "status": STATUS_OK, "detail": "3.12.10"}
    ]

    async def fake_fail(include_network=True):
        return [CheckResult("故障项", STATUS_FAIL, "boom")]

    monkeypatch.setattr(doctor_module, "run_doctor", fake_fail)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.run_doctor_standalone(as_json=True)
    payload = json.loads(buf.getvalue())
    assert code == 1
    assert payload["status"] == "fail"
    assert payload["checks"][0]["name"] == "故障项"
    print("✓ doctor --json 输出与退出码")


def test_doctor_offline_flag(monkeypatch):
    import cli

    calls = {}

    def fake_standalone(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_doctor_standalone", fake_standalone)
    monkeypatch.setattr(sys, "argv", ["cli.py", "doctor", "--json", "--offline"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert calls == {"as_json": True, "include_network": False}
    print("✓ doctor --offline 跳过网络检查")


def test_doctor_rejects_unknown_flag(monkeypatch, capsys):
    import cli

    monkeypatch.setattr(sys, "argv", ["cli.py", "doctor", "--bogus"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "用法" in capsys.readouterr().out
    print("✓ doctor 未知参数退出码 2")


def test_python_version_below_requirement(monkeypatch):
    class _OldVersion(tuple):
        major = 3
        minor = 9
        micro = 5

    monkeypatch.setattr(sys, "version_info", _OldVersion((3, 9, 5)))
    result = check_python()
    assert result.status == STATUS_FAIL and ">= 3.10" in result.detail
    print("✓ Python 版本低于要求 → 失败")


def test_dependencies_missing_rag(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec

    def fake(name, *args, **kwargs):
        if name in {"pymilvus", "sentence_transformers", "milvus_lite"}:
            return None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    results = check_dependencies()
    rag = next(item for item in results if item.name == "RAG 依赖")
    assert rag.status == STATUS_WARN and "缺少" in rag.detail
    print("✓ RAG 依赖缺失 → 警告")


def test_storage_redis_ping_failure(monkeypatch):
    """describe_backends 报告 Redis 后端但 ping 失败 → 警告（不是静默可用）。"""
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setitem(STORAGE_CONFIG, "postgres_dsn", "")
    monkeypatch.setattr(
        storage_module,
        "describe_backends",
        lambda: {
            "short_term": "redis",
            "long_term": "json",
            "short_term_degraded": False,
        },
    )
    result = check_storage()
    assert result.status == STATUS_WARN and "Redis 不可用" in result.detail
    print("✓ Redis 后端 ping 失败 → 警告")


def test_storage_postgres_probe_failure(monkeypatch):
    """describe_backends 报告 PostgreSQL 后端但连接探测失败 → 警告。"""
    import context.postgres_long_term_memory as pg_module
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "")
    monkeypatch.setitem(
        STORAGE_CONFIG, "postgres_dsn", "postgresql://u:p@127.0.0.1:1/db"
    )
    monkeypatch.setattr(
        storage_module,
        "describe_backends",
        lambda: {
            "short_term": "memory",
            "long_term": "postgres",
            "short_term_degraded": False,
        },
    )

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("db down")

    monkeypatch.setattr(pg_module, "PostgresLongTermMemory", _Boom)
    result = check_storage()
    assert result.status == STATUS_WARN and "PostgreSQL 不可用" in result.detail
    print("✓ PostgreSQL 探测失败 → 警告")


def test_skills_root_without_scripts(monkeypatch):
    from utils import skill_paths

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(skill_paths, "resolve_skills_root", lambda: Path(tmp))
        monkeypatch.setattr(skill_paths, "skill_scripts", lambda root: [])
        result = check_skills()
    assert result.status == STATUS_FAIL and "没有可加载的技能" in result.detail
    print("✓ 技能目录为空 → 失败并说明缺少 agent.py")


def test_version_check_missing_version_and_static_tag(monkeypatch):
    from utils import doctor as doctor_module

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            "[project]" + chr(10) + 'name = "x"' + chr(10), encoding="utf-8"
        )
        static = root / "webapp" / "static"
        static.mkdir(parents=True)
        (static / "index.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(doctor_module, "PROJECT_ROOT", root)
        result = check_version_consistency()
    assert result.status == STATUS_WARN
    assert "未找到 version" in result.detail and "?v=" in result.detail
    print("✓ pyproject 缺 version / index.html 缺静态版本 → 警告")


def test_model_files_missing_required_fail():
    with tempfile.TemporaryDirectory() as tmp:
        # 只有推荐文件、缺必需文件（config/model/tokenizer）
        (Path(tmp) / "modules.json").write_text("[]", encoding="utf-8")
        result = check_model_files(Path(tmp))
    assert result.status == STATUS_FAIL and "缺少关键文件" in result.detail
    print("✓ 模型目录缺必需文件 → 失败")


def test_run_doctor_includes_network_check(monkeypatch):
    from utils import doctor as doctor_module
    from utils.doctor import CheckResult

    async def fake_connectivity():
        return CheckResult("LLM 连通性", STATUS_SKIP, "测试跳过")

    monkeypatch.setattr(doctor_module, "check_llm_connectivity", fake_connectivity)
    results = asyncio.run(run_doctor(include_network=True))
    llm = next(item for item in results if item.name == "LLM 连通性")
    assert llm.detail == "测试跳过"
    print("✓ include_network=True 时执行连通性检查")


def test_storage_postgres_configured_but_json(monkeypatch):
    """PostgreSQL 配置了但后端已回退 JSON → 明确提示，不假装可用。"""
    from config import STORAGE_CONFIG
    from context import storage as storage_module

    monkeypatch.setitem(STORAGE_CONFIG, "redis_url", "")
    monkeypatch.setitem(
        STORAGE_CONFIG, "postgres_dsn", "postgresql://u:p@127.0.0.1:1/db"
    )
    monkeypatch.setattr(
        storage_module,
        "describe_backends",
        lambda: {
            "short_term": "memory",
            "long_term": "json",
            "short_term_degraded": False,
        },
    )
    result = check_storage()
    assert result.status == STATUS_WARN and "已回退 JSON" in result.detail
    print("✓ PostgreSQL 配置但回退 JSON → 警告")


def test_version_check_unreadable_index(monkeypatch):
    """index.html 无法解码时提示具体文件，而不是崩溃。"""
    from utils import doctor as doctor_module
    from utils.version import APP_VERSION

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            f'version = "{APP_VERSION}"', encoding="utf-8"
        )
        static = root / "webapp" / "static"
        static.mkdir(parents=True)
        (static / "index.html").write_bytes(b"<html>" + bytes([255]) + b"</html>")
        monkeypatch.setattr(doctor_module, "PROJECT_ROOT", root)
        result = check_version_consistency()
    assert result.status == STATUS_WARN and "无法读取 index.html" in result.detail
    print("✓ index.html 无法读取 → 警告")


def test_default_model_dir_falls_back(monkeypatch):
    """未配置嵌入模型目录时使用默认的 bge-small-zh-v1.5。"""
    from config import RAG_CONFIG
    from utils import doctor as doctor_module

    monkeypatch.setitem(RAG_CONFIG, "embedding_model", "")
    path = doctor_module._default_model_dir()
    assert path.name == "bge-small-zh-v1.5", path
    print("✓ 默认嵌入模型目录回退")


if __name__ == "__main__":
    test_python_ok()
    test_model_files_missing_dir()
    test_model_files_complete()
    test_model_files_missing_metadata_warns()
    test_model_files_missing_required_fail()
    test_knowledge_base_missing()
    test_redact_secret_masks_passwords()
    test_report_and_summary_statuses()
    for case in (
        test_storage_defaults,
        test_storage_unreachable_warns,
        test_storage_runtime_degraded_warns,
        test_knowledge_base_reason_variants,
        test_config_and_dependencies_branches,
        test_model_files_accepts_huggingface_id,
        test_knowledge_base_dimension_mismatch_warns,
        test_storage_probe_sets_statement_timeout,
        test_llm_connectivity_branches,
        test_python_version_below_requirement,
        test_dependencies_missing_rag,
        test_storage_redis_ping_failure,
        test_storage_postgres_probe_failure,
        test_skills_root_without_scripts,
        test_version_check_missing_version_and_static_tag,
        test_run_doctor_includes_network_check,
        test_check_config_key_source_strips_env,
        test_storage_postgres_configured_but_json,
        test_version_check_unreadable_index,
        test_default_model_dir_falls_back,
    ):
        mp = pytest.MonkeyPatch()
        try:
            case(mp)
        finally:
            mp.undo()
    test_run_doctor_without_network()
    test_version_compare_normalizes_pep440()
    mp = pytest.MonkeyPatch()
    try:
        test_model_dir_follows_rag_config(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_version_consistency(mp)
    finally:
        mp.undo()
    mp = pytest.MonkeyPatch()
    try:
        test_doctor_json_output(mp)
    finally:
        mp.undo()
    print("\n全部通过")