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
    """一条评测用例：问题 + 命中任一关键词即算相关。

    ``source`` 声明该用例对应的知识文档（``.claude/skills/ask-question/
    data/documents`` 下的文件名）：离线用例测试会校验每个关键词确实出现
    在该文档中、且每份文档都有用例覆盖——改知识库或新增文档时题库不会
    悄悄漂移，评测数字才有可比性。
    """

    query: str
    keywords: tuple[str, ...]
    source: str = ""


# 用例对应 .claude/skills/ask-question/data 下的企业差旅知识库；
# 关键词选可区分度高的数字 / 专有表述，避免“报销”这类高频词造成误判。
# 覆盖全部 8 份文档（差旅标准 / 报销制度 / 预订指南 / FAQ / 紧急处理 /
# 平台指南 / 城市贴士 / 环保倡议），由 test_rag_eval 的离线用例守护。
EVAL_CASES: tuple[EvalCase, ...] = (
    EvalCase("北京出差的住宿标准是多少？", ("500元", "一线城市住宿"), "01_travel_standards.txt"),
    EvalCase("杭州、南京这类二线城市住宿标准呢？", ("400元", "二线城市住宿"), "01_travel_standards.txt"),
    EvalCase("出差午餐和晚餐每餐能报销多少？", ("100元", "午餐和晚餐"), "01_travel_standards.txt"),
    EvalCase("出差结束后多久内必须提交报销？", ("30个自然日", "30天"), "02_reimbursement_policy.txt"),
    EvalCase("报销需要准备哪些材料？", ("审批单", "增值税发票"), "02_reimbursement_policy.txt"),
    EvalCase("国内机票建议提前多久预订？", ("7-14天",), "03_booking_guide.txt"),
    EvalCase("发票抬头和税号有什么要求？", ("公司全称", "统一社会信用代码"), "02_reimbursement_policy.txt"),
    EvalCase("哪些费用属于个人消费不予报销？", ("个人消费", "白条"), "02_reimbursement_policy.txt"),
    EvalCase("住宿超标准了怎么审批？", ("超标30%", "超标准"), "01_travel_standards.txt"),
    EvalCase("绿色出差有哪些建议？", ("绿色出行", "低碳"), "08_environmental_initiatives.txt"),
    EvalCase("报销审批通过后多久到账？", ("3-5个工作日", "工资卡"), "02_reimbursement_policy.txt"),
    EvalCase("出差申请需要提前几个工作日提交？", ("3个工作日",), "04_faq.txt"),
    EvalCase("业务招待费怎么报销？", ("业务招待", "500元"), "01_travel_standards.txt"),
    EvalCase("签证费用可以报销吗？", ("签证费",), "02_reimbursement_policy.txt"),
    # 04 FAQ
    EvalCase("出差申请被驳回了怎么办？", ("驳回原因",), "04_faq.txt"),
    EvalCase("可以开私家车出差吗？", ("私家车",), "04_faq.txt"),
    EvalCase("什么情况下可以申请商务舱或头等舱？", ("1.85米", "孕妇"), "04_faq.txt"),
    # 05 紧急情况处理
    EvalCase("航班延误超过4小时怎么办？", ("超过4小时",), "05_emergency_procedures.txt"),
    EvalCase("托运行李丢失或延误了怎么处理？", ("行李延误证明", "行李牌"), "05_emergency_procedures.txt"),
    EvalCase("因交通堵塞误机了怎么办？", ("误机", "1.5小时"), "05_emergency_procedures.txt"),
    EvalCase("到酒店发现房间超售没有房怎么办？", ("超售", "免费升级"), "05_emergency_procedures.txt"),
    # 06 平台指南
    EvalCase("阿里商旅平台的企业协议价能优惠多少？", ("10%-30%",), "06_platform_guide.txt"),
    EvalCase("阿里商旅平台的客服电话是多少？", ("400-800-8888", "7×24"), "06_platform_guide.txt"),
    EvalCase("阿里商旅支持哪些登录方式？", ("钉钉",), "06_platform_guide.txt"),
    # 07 城市贴士
    EvalCase("从大兴机场到北京市区怎么走？", ("大兴机场线", "草桥"), "07_city_specific_tips.txt"),
    EvalCase("上海浦东机场怎么快速到市区？", ("磁悬浮", "龙阳路"), "07_city_specific_tips.txt"),
    EvalCase("深圳宝安机场到市区有哪些交通方式？", ("宝安国际机场", "地铁11号线"), "07_city_specific_tips.txt"),
    # 08 环保倡议
    EvalCase("多少公里以内的出差应优先选择高铁？", ("500公里", "90%"), "08_environmental_initiatives.txt"),
    EvalCase("出差时如何选择环保酒店？", ("环保认证",), "08_environmental_initiatives.txt"),
    EvalCase("办公中如何节约用纸？", ("双面打印",), "08_environmental_initiatives.txt"),
)


def evaluate_retrieval(
    search: Callable[[str, int], list[dict[str, Any]]],
    cases: tuple[EvalCase, ...] = EVAL_CASES,
    top_k: int = 5,
    min_score: float | None = None,
) -> dict[str, Any]:
    """运行评测。

    Args:
        search: ``search(query, top_k) -> [{"content": ..., "distance": ...}, ...]``
        cases: 评测用例
        top_k: 每个问题检索的片段数（同时决定 hit@K 的上限）
        min_score: 问答链路的相似度阈值（同一配置值）。评测 / 管理接口是
            纯向量口径，这里过滤的是向量余弦距离；聊天链路过滤的是
            ``max(余弦, 归一化 BM25)`` 的混合 ``distance``——数值同源、
            但不在同一分数空间。给定时低于阈值的结果不参与命中判定
            （否则评测能 100% 通过而真实回答全是「没有找到」）

    Returns:
        {"total", "hit1", "hit3", "hit5", "mrr",
         "rows": [{"query", "rank", "source"}], "warning": 可选}
    """
    ranks: list[int | None] = []
    rows: list[dict[str, Any]] = []

    for case in cases:
        documents = search(case.query, top_k) or []
        if min_score is not None:
            documents = [
                document
                for document in documents
                if float(document.get("distance") or 0.0) >= min_score
            ]
        rank: int | None = None
        for index, document in enumerate(documents[:top_k], start=1):
            text = str(document.get("content", "")).lower()
            if any(keyword.lower() in text for keyword in case.keywords):
                rank = index
                break
        ranks.append(rank)
        rows.append({"query": case.query, "rank": rank, "source": case.source})

    total = len(ranks)

    def hit_at(k: int) -> float:
        if not total:
            return 0.0
        return sum(1 for rank in ranks if rank is not None and rank <= k) / total

    mrr = sum(1.0 / rank for rank in ranks if rank) / total if total else 0.0
    payload: dict[str, Any] = {
        "total": total,
        "hit1": hit_at(1),
        "hit3": hit_at(3),
        "hit5": hit_at(5),
        "mrr": mrr,
        "rows": rows,
    }
    if total and hit_at(5) == 0:
        # 固定题库与当前知识库完全不匹配时给出明确告警，避免被当作质量回退
        payload["warning"] = (
            "没有命中任何评测题：题库可能不适用于当前知识库，请核对知识内容或更换题库"
        )
    return payload
