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
4. 本地嵌入模型文件完整性（modules.json 等元数据缺失会导致 SentenceTransformer
   回退 mean pooling 并打印警告）
5. RAG 知识库状态（是否已初始化 / 片段数）
6. LLM 服务连通性（有密钥时）
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_DIR = PROJECT_ROOT / "data" / "models" / "bge-small-zh-v1.5"
DEFAULT_KB_DB = (
    PROJECT_ROOT
    / ".claude"
    / "skills"
    / "ask-question"
    / "data"
    / "rag_knowledge"
    / "milvus_lite.db"
)
COLLECTION_NAME = "business_travel_knowledge"


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

    missing_core = [pkg for mod, pkg in core.items() if importlib.util.find_spec(mod) is None]
    if missing_core:
        core_result = CheckResult(
            "核心依赖",
            STATUS_FAIL,
            "缺少: " + ", ".join(missing_core) + "（pip install -r requirements.txt）",
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


def check_config() -> CheckResult:
    """检查大模型配置（API Key 是否就绪）。"""
    from config import LLM_CONFIG

    api_key = LLM_CONFIG["api_key"]
    model_name = LLM_CONFIG["model_name"]
    base_url = LLM_CONFIG["base_url"]
    if api_key:
        source = "环境变量" if os.environ.get("LLM_API_KEY") else "config_local.py"
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


def check_model_files(model_dir: Path | str | None = None) -> CheckResult:
    """检查本地嵌入模型文件完整性。"""
    model_path = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
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
    """检查 RAG 知识库是否已初始化。"""
    db = Path(db_path) if db_path else DEFAULT_KB_DB
    if not db.exists():
        return CheckResult(
            "RAG 知识库",
            STATUS_WARN,
            "未初始化：python .claude/skills/ask-question/script/init_knowledge_base.py",
        )

    try:
        from pymilvus import MilvusClient
    except ImportError:
        return CheckResult("RAG 知识库", STATUS_WARN, "数据库文件存在，但未安装 pymilvus，无法读取统计")

    client = None
    try:
        client = MilvusClient(str(db))
        if not client.has_collection(COLLECTION_NAME):
            return CheckResult("RAG 知识库", STATUS_WARN, "数据库存在，但缺少知识集合（请重新初始化）")
        stats = client.get_collection_stats(COLLECTION_NAME)
        count = stats.get("row_count", 0)
        if count <= 0:
            return CheckResult("RAG 知识库", STATUS_WARN, "知识集合为空（请运行初始化脚本）")
        return CheckResult("RAG 知识库", STATUS_OK, f"已初始化，{count} 个知识片段")
    except Exception as e:  # noqa: BLE001 - 自检场景需要兜底展示错误
        return CheckResult("RAG 知识库", STATUS_WARN, f"无法读取知识库: {str(e)[:120]}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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


async def run_doctor(include_network: bool = True) -> list[CheckResult]:
    """执行全部自检项。

    Args:
        include_network: 是否包含 LLM 连通性检查（测试中可关闭，避免真实请求）
    """
    results: list[CheckResult] = [check_python()]
    results.extend(check_dependencies())
    results.append(check_config())
    results.append(check_model_files())
    results.append(check_knowledge_base())
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
