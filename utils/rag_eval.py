"""
RAG 检索离线评测

用固定的差旅问题集检验知识库检索质量：对每个问题，Top-K 片段中只要出现
任一预期关键词即视为命中，输出 hit@1 / hit@3 / hit@5 与 MRR。

只检索、不调用大模型，结果可重复且不产生 token 费用；知识库未初始化时
由调用方（`python cli.py eval`）提示跳过。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class EvalCase:
    """一条评测用例：问题 + 命中任一关键词即算相关。"""

    query: str
    keywords: tuple[str, ...]


# 用例对应 .claude/skills/ask-question/data 下的企业差旅知识库；
# 关键词选可区分度高的数字 / 专有表述，避免“报销”这类高频词造成误判。
EVAL_CASES: tuple[EvalCase, ...] = (
    EvalCase("北京出差的住宿标准是多少？", ("500元", "一线城市住宿")),
    EvalCase("杭州、南京这类二线城市住宿标准呢？", ("400元", "二线城市住宿")),
    EvalCase("出差午餐和晚餐每餐能报销多少？", ("100元", "午餐和晚餐")),
    EvalCase("出差结束后多久内必须提交报销？", ("30个自然日", "30天")),
    EvalCase("报销需要准备哪些材料？", ("审批单", "增值税发票")),
    EvalCase("国内机票建议提前多久预订？", ("7-14天",)),
    EvalCase("发票抬头和税号有什么要求？", ("公司全称", "统一社会信用代码")),
    EvalCase("哪些费用属于个人消费不予报销？", ("个人消费", "白条")),
    EvalCase("住宿超标准了怎么审批？", ("超标30%", "超标准")),
    EvalCase("绿色出差有哪些建议？", ("绿色出行", "低碳")),
    EvalCase("报销审批通过后多久到账？", ("3-5个工作日", "工资卡")),
    EvalCase("出差申请需要提前几个工作日提交？", ("3个工作日",)),
    EvalCase("业务招待费怎么报销？", ("业务招待", "500元")),
    EvalCase("签证费用可以报销吗？", ("签证费",)),
)


def evaluate_retrieval(
    search: Callable[[str, int], list[dict[str, Any]]],
    cases: tuple[EvalCase, ...] = EVAL_CASES,
    top_k: int = 5,
) -> dict[str, Any]:
    """运行评测。

    Args:
        search: ``search(query, top_k) -> [{"content": ...}, ...]``
        cases: 评测用例
        top_k: 每个问题检索的片段数（同时决定 hit@K 的上限）

    Returns:
        {"total", "hit1", "hit3", "hit5", "mrr", "rows": [{"query", "rank"}]}
    """
    ranks: list[int | None] = []
    rows: list[dict[str, Any]] = []

    for case in cases:
        documents = search(case.query, top_k) or []
        rank: int | None = None
        for index, document in enumerate(documents[:top_k], start=1):
            text = str(document.get("content", "")).lower()
            if any(keyword.lower() in text for keyword in case.keywords):
                rank = index
                break
        ranks.append(rank)
        rows.append({"query": case.query, "rank": rank})

    total = len(ranks)

    def hit_at(k: int) -> float:
        if not total:
            return 0.0
        return sum(1 for rank in ranks if rank is not None and rank <= k) / total

    mrr = sum(1.0 / rank for rank in ranks if rank) / total if total else 0.0
    return {
        "total": total,
        "hit1": hit_at(1),
        "hit3": hit_at(3),
        "hit5": hit_at(5),
        "mrr": mrr,
        "rows": rows,
    }
