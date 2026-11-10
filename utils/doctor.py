"""
环境自检（doctor）

借鉴 MiQroForge 的 diagnose 脚本思路：一条命令检查项目运行所需的全部前置条件，
便于新环境排障（面试演示、换电脑、CI 之外的手工验证）。

用法：
    python cli.py doctor

检查项：
1. Python 版本
2. 核心依赖与 RAG 可选依赖
3. 大模型配置（API Key / 模型 / 接口地址）
4. 记忆存储后端（默认进程内 + JSON；配置后检查 Redis / PostgreSQL 连通性）
5. 本地嵌入模型文件完整性（modules.json 等元数据缺失会导致 SentenceTransformer
   回退 mean pooling 并打印警告）
6. RAG 知识库状态（是否已初始化 / 片段数）
7. LLM 服务连通性（有密钥时）
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_DIR = PROJECT_ROOT / "data" / "models" / "bge-small-zh-v1.5"


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    status: str  # ok / warn / fail / skip
    detail: str


def check_python() -> CheckResult:
    """检查 Python 版本（要求 >= 3.10）。"""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        return CheckResult("Python 版本", STATUS_OK, version)
    return CheckResult("Python 版本", STATUS_FAIL, f"{version}（需要 >= 3.10）")


def check_dependencies() -> list[CheckResult]:
    """检查核心依赖与 RAG 可选依赖。"""
    core = {
        "agentscope": "agentscope",
        "rich": "rich",
        "yaml": "PyYAML",
        "ddgs": "ddgs",
    }
    rag = {
        "pymilvus": "pymilvus",
        "sentence_transformers": "sentence-transformers",
        "milvus_lite": "milvus-lite",
    }

    # find_spec 只能证明「找得到包」，装坏（缺子依赖 / 二进制不匹配）时 import
    # 会失败但 doctor 仍报「已安装」——核心依赖必须实际导入才算可用
    broken_core: list[str] = []
    for mod, pkg in core.items():
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 - 断依赖包同样算不可用
            broken_core.append(f"{pkg}（{str(exc)[:60]}）")
    if broken_core:
        core_result = CheckResult(
            "核心依赖",
            STATUS_FAIL,
            "无法导入: " + "、".join(broken_core) + "（pip install -r requirements.txt）",
        )
    else:
        core_result = CheckResult("核心依赖", STATUS_OK, "agentscope / rich / PyYAML / ddgs 已安装")

    missing_rag = [pkg for mod, pkg in rag.items() if importlib.util.find_spec(mod) is None]
    if missing_rag:
        rag_result = CheckResult(
            "RAG 依赖",
            STATUS_WARN,
            "缺少: " + ", ".join(missing_rag) + "（知识库功能不可用；pip install -r requirements.txt）",
        )
    else:
        rag_result = CheckResult("RAG 依赖", STATUS_OK, "pymilvus / sentence-transformers / milvus-lite 已安装")

    return [core_result, rag_result]


def _redact_secret(text: object) -> str:
    """遮蔽连接串里的密码。

    psycopg / redis 的异常会回显完整 DSN，doctor 输出常被粘贴到工单或
    聊天里，凭据不能跟着带出去。覆盖 URI、缺 scheme 的 user:pass@ 粘贴
    与 password= 关键字形态（见 utils.log_redact）。
    """
    from utils.log_redact import redact_secret

    return redact_secret(text)


def check_config() -> CheckResult:
    """检查大模型配置（API Key 是否就绪）。"""
    from config import LLM_CONFIG

    api_key = LLM_CONFIG["api_key"]
    model_name = LLM_CONFIG["model_name"]
    base_url = LLM_CONFIG["base_url"]
    if api_key:
        # 空 / 纯空白的环境变量在 config 里会回退 config_local.py：
        # 这里按 strip 后判断，避免误报「密钥来自 环境变量」
        env_key = os.environ.get("LLM_API_KEY", "").strip()
        source = "环境变量" if env_key else "config_local.py"
        return CheckResult(
            "大模型配置",
            STATUS_OK,
            f"{model_name} @ {base_url}（密钥来自 {source}）",
        )
    return CheckResult(
        "大模型配置",
        STATUS_FAIL,
        "未配置 API Key：设置环境变量 LLM_API_KEY，或创建 config_local.py（API_KEY = \"sk-...\"）",
    )


def check_storage() -> CheckResult:
    """检查记忆存储：报告实际生效的后端，并测试配置后端的连通性。"""
    from config import STORAGE_CONFIG
    from context.storage import describe_backends

    redis_url = STORAGE_CONFIG.get("redis_url") or ""
    dsn = STORAGE_CONFIG.get("postgres_dsn") or ""
    # describe_backends 与应用启动时走同一套选择 / 冷却逻辑，报告才和实际一致
    backends = describe_backends()
    parts: list[str] = []
    failures: list[str] = []

    if backends["short_term"] == "redis":
        if backends.get("short_term_degraded"):
            failures.append(
                "Redis 运行中故障：短期记忆已暂时回退进程内（60 秒后自动重试）"
            )
        else:
            try:
                import redis

                client = redis.Redis.from_url(
                    redis_url, socket_connect_timeout=1.5, socket_timeout=1.5
                )
                client.ping()
                parts.append("短期记忆 · Redis 可用")
            except Exception as exc:  # noqa: BLE001 - 连通性检查失败属于检查结果
                failures.append(f"Redis 不可用：{_redact_secret(exc)[:120]}")
    else:
        parts.append("短期记忆 · 进程内")
        if redis_url:
            failures.append("Redis 已配置但当前不可用（已回退进程内）")

    if backends["long_term"] == "postgres":
        try:
            from context.postgres_long_term_memory import PostgresLongTermMemory

            # 自检探测给 DDL / 查询设上限：只有 connect_timeout 挡不住
            # _ensure_schema 在网络半通时无限等待
            memory = PostgresLongTermMemory(
                "__doctor_probe__", dsn, statement_timeout_ms=3000
            )
            memory.close()
            parts.append("长期记忆 · PostgreSQL 可用")
        except Exception as exc:  # noqa: BLE001 - 连通性检查失败属于检查结果
            failures.append(f"PostgreSQL 不可用：{_redact_secret(exc)[:120]}")
    else:
        parts.append("长期记忆 · JSON 文件")
        if dsn:
            failures.append("PostgreSQL 已配置但当前不可用（已回退 JSON）")

    if failures:
        return CheckResult("记忆存储", STATUS_WARN, "；".join(parts + failures))
    return CheckResult("记忆存储", STATUS_OK, "；".join(parts))


def check_skills() -> CheckResult:
    """检查技能目录：源码运行 / wheel 安装后都应能发现技能脚本。"""
    from utils.skill_paths import resolve_skills_root, skill_scripts

    root = resolve_skills_root()
    scripts = skill_scripts(root)
    if scripts:
        return CheckResult("技能目录", STATUS_OK, f"{len(scripts)} 个技能（{root}）")
    if not root.is_dir():
        return CheckResult(
            "技能目录",
            STATUS_FAIL,
            f"未找到 {root}；请检查安装，或用 ALIGO_SKILLS_DIR 指定技能目录",
        )
    return CheckResult(
        "技能目录",
        STATUS_FAIL,
        f"{root} 下没有可加载的技能（缺少 */script/agent.py）",
    )


def _versions_equal(left: str, right: str) -> bool:
    """按 PEP 440 规范化比较版本：1.6.0-rc1 与 1.6.0rc1 视为同一版本。

    版本号本身不是合法 PEP 440（如带后缀的自定义字符串）时退回严格字符串
    比较，避免规范化异常把「一致的版本」误报成不一致。
    """
    if left == right:
        return True
    try:
        from packaging.version import Version

        return Version(left) == Version(right)
    except Exception:  # noqa: BLE001 - 非法版本号退回字符串比较
        return False


def check_version_consistency() -> CheckResult:
    """检查 pyproject/安装元数据、静态资源版本与 ``APP_VERSION`` 是否一致。"""
    from utils.version import APP_VERSION

    problems: list[str] = []
    checked: list[str] = []

    pyproject = PROJECT_ROOT / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"无法读取 pyproject.toml：{exc}")
        else:
            match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if match:
                checked.append("pyproject")
                if not _versions_equal(match.group(1), APP_VERSION):
                    problems.append(f"pyproject.toml 为 {match.group(1)}")
            else:
                problems.append("pyproject.toml 中未找到 version")
    else:
        # wheel 安装没有源码 pyproject.toml：改用 dist-info 元数据校验
        try:
            from importlib.metadata import PackageNotFoundError, version

            installed = version("aligo-travel-assistant")
        except PackageNotFoundError:
            installed = None
        except Exception:  # noqa: BLE001 - 元数据损坏时按未安装处理
            installed = None
        if installed:
            checked.append("wheel 元数据")
            if not _versions_equal(installed, APP_VERSION):
                problems.append(f"wheel 元数据为 {installed}")

    index_html = PROJECT_ROOT / "webapp" / "static" / "index.html"
    if index_html.is_file():
        try:
            html = index_html.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"无法读取 index.html：{exc}")
        else:
            versions = set(re.findall(r"\?v=([0-9][0-9A-Za-z.\-]*)", html))
            if versions:
                checked.append("静态资源")
                mismatched = sorted(
                    version
                    for version in versions
                    if not _versions_equal(version, APP_VERSION)
                )
                if mismatched:
                    problems.append(f"index.html 静态资源为 {', '.join(mismatched)}")
            else:
                problems.append("index.html 未找到 ?v= 静态资源版本")

    if problems:
        return CheckResult(
            "版本一致性",
            STATUS_WARN,
            f"utils/version.py 为 {APP_VERSION}；" + "；".join(problems),
        )
    if not checked:
        return CheckResult(
            "版本一致性", STATUS_SKIP, "未找到 pyproject.toml / 安装元数据 / 静态资源版本，跳过"
        )
    return CheckResult(
        "版本一致性",
        STATUS_OK,
        f"{APP_VERSION}（{' / '.join(checked)} 一致）",
    )


def _default_model_dir() -> Path:
    """按 RAG 技能的同一口径解析嵌入模型目录。

    锚点取技能目录所在的安装根（源码安装 = 仓库根；wheel 安装 = sys.prefix，
    技能装在 ``<root>/.claude/skills``），再拼 ``RAG_CONFIG["embedding_model"]``。
    此前固定用本文件上两级的 ``data/models``：wheel 安装时不指向实际路径，
    必然误报「未下载」。
    """
    from utils.skill_paths import resolve_skills_root

    anchor = Path(resolve_skills_root()).parent.parent
    configured = ""
    try:
        from config import RAG_CONFIG

        configured = str(RAG_CONFIG.get("embedding_model") or "")
    except Exception:  # noqa: BLE001 - 配置缺失时退回默认目录名
        configured = ""
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = anchor / candidate
        return candidate
    return anchor / "data" / "models" / "bge-small-zh-v1.5"


_HF_MODEL_ID_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def check_model_files(model_dir: Path | str | None = None) -> CheckResult:
    """检查本地嵌入模型文件完整性。"""
    configured = ""
    try:
        from config import RAG_CONFIG

        configured = str(RAG_CONFIG.get("embedding_model") or "")
    except Exception:  # noqa: BLE001 - 配置缺失按默认目录处理
        configured = ""
    model_path = Path(model_dir) if model_dir else _default_model_dir()
    if (
        model_dir is None
        and configured
        and not model_path.exists()
        and configured.count("/") == 1
        and not configured.startswith((".", "/"))
        and _HF_MODEL_ID_RE.match(configured)
    ):
        # RAG 技能支持 HuggingFace 模型 ID：本地无缓存是首次下载，不是缺文件
        return CheckResult(
            "嵌入模型文件",
            STATUS_OK,
            f"使用 HuggingFace 模型 {configured}（本地无缓存时首次运行自动下载）",
        )
    required = ["config.json", "model.safetensors", "tokenizer.json"]
    recommended = ["modules.json", "1_Pooling/config.json", "vocab.txt"]

    if not model_path.exists():
        return CheckResult(
            "嵌入模型文件",
            STATUS_WARN,
            f"未下载: {model_path}（RAG 功能不可用；下载方式见 README）",
        )

    missing_required = [f for f in required if not (model_path / f).exists()]
    if missing_required:
        return CheckResult(
            "嵌入模型文件",
            STATUS_FAIL,
            "缺少关键文件: " + ", ".join(missing_required),
        )

    missing_recommended = [f for f in recommended if not (model_path / f).exists()]
    if missing_recommended:
        return CheckResult(
            "嵌入模型文件",
            STATUS_WARN,
            "缺少 " + ", ".join(missing_recommended)
            + "（可运行，但会出现 mean pooling 回退警告；请从 HuggingFace 下载完整模型仓库文件）",
        )

    return CheckResult("嵌入模型文件", STATUS_OK, str(model_path))


def check_knowledge_base(db_path: Path | str | None = None) -> CheckResult:
    """检查 RAG 知识库是否已初始化（复用 knowledge_status，避免重复实现）。"""
    from utils.knowledge_stats import knowledge_status

    status = knowledge_status(db_path)
    if not status["available"]:
        reason = str(status.get("reason", "未知原因"))
        if reason == "知识库未初始化":
            return CheckResult(
                "RAG 知识库",
                STATUS_WARN,
                "未初始化：python .claude/skills/ask-question/script/init_knowledge_base.py",
            )
        if reason == "集合不存在":
            return CheckResult("RAG 知识库", STATUS_WARN, "数据库存在，但缺少知识集合（请重新初始化）")
        return CheckResult("RAG 知识库", STATUS_WARN, f"无法读取知识库: {reason[:120]}")

    count = int(status.get("documents", 0))
    if count <= 0:
        return CheckResult("RAG 知识库", STATUS_WARN, "知识集合为空（请运行初始化脚本）")
    if status.get("dimension_mismatch"):
        # 换嵌入模型后集合维度与当前模型不一致：检索会静默返回空，比未初始化更隐蔽
        return CheckResult(
            "RAG 知识库",
            STATUS_WARN,
            f"已初始化 {count} 个片段，但集合维度（{status.get('dimension')}）"
            "与当前嵌入模型不一致，请重新初始化知识库",
        )
    return CheckResult("RAG 知识库", STATUS_OK, f"已初始化，{count} 个知识片段")


async def check_llm_connectivity() -> CheckResult:
    """检查 LLM 服务连通性（未配置密钥时跳过）。"""
    from config import LLM_CONFIG
    from utils.llm_resilience import run_health_check

    if not LLM_CONFIG["api_key"]:
        return CheckResult("LLM 连通性", STATUS_SKIP, "未配置 API Key，已跳过")

    ok, message = await run_health_check(
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        model_name=LLM_CONFIG["model_name"],
        timeout_sec=10.0,
    )
    if ok:
        return CheckResult("LLM 连通性", STATUS_OK, f"{LLM_CONFIG['model_name']} 可用")
    return CheckResult("LLM 连通性", STATUS_FAIL, message[:150])


def run_doctor_offline() -> list[CheckResult]:
    """执行不依赖网络的同步检查项（供线程池调用，避免阻塞事件循环）。"""
    results: list[CheckResult] = [check_python()]
    results.extend(check_dependencies())
    results.append(check_config())
    results.append(check_version_consistency())
    results.append(check_skills())
    results.append(check_storage())
    results.append(check_model_files())
    results.append(check_knowledge_base())
    return results


async def run_doctor(include_network: bool = True) -> list[CheckResult]:
    """执行全部自检项。

    同步检查（含冷启动打开 Milvus）放到线程池，避免在 Web 事件循环上
    阻塞对话 SSE 与其它接口。

    Args:
        include_network: 是否包含 LLM 连通性检查（测试中可关闭，避免真实请求）
    """
    results = await asyncio.to_thread(run_doctor_offline)
    if include_network:
        results.append(await check_llm_connectivity())
    else:
        results.append(CheckResult("LLM 连通性", STATUS_SKIP, "已跳过（未请求网络）"))
    return results


def has_failures(results: list[CheckResult]) -> bool:
    """是否存在必须处理的问题（fail）。"""
    return any(r.status == STATUS_FAIL for r in results)


def build_report_table(results: list[CheckResult]):
    """构建自检结果表格（rich.Table）。"""
    from rich.table import Table

    icons = {
        STATUS_OK: ("✓", "green"),
        STATUS_WARN: ("⚠", "yellow"),
        STATUS_FAIL: ("✗", "red"),
        STATUS_SKIP: ("—", "dim"),
    }
    table = Table(title="环境自检 (doctor)", show_header=True, header_style="bold magenta")
    table.add_column("检查项", style="cyan", width=16)
    table.add_column("状态", width=6)
    table.add_column("详情", style="white")

    for result in results:
        icon, color = icons.get(result.status, ("?", "white"))
        table.add_row(result.name, f"[{color}]{icon}[/{color}]", result.detail)
    return table


def summarize(results: list[CheckResult]) -> tuple[str, str]:
    """返回结论文本与 rich 样式。"""
    if has_failures(results):
        return "✗ 存在必须处理的问题，请根据上表修复", "red"
    if any(r.status == STATUS_WARN for r in results):
        return "⚠ 环境可用，但存在建议修复的警告", "yellow"
    return "✓ 环境检查全部通过", "green"
