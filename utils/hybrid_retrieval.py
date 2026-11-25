"""混合检索工具：中文友好的 BM25、RRF 融合与相关性标注。

设计约束：
- 纯 Python、零新增依赖：中文按字符二元组切词（无需分词器），英文 /
  数字按词切；BM25 与 RRF 都能在 Milvus Lite / CI 环境直接运行。
- 只负责「候选融合与打分」，不直接访问 Milvus：向量候选由调用方提供，
  BM25 候选由 ``BM25Index`` 对同一批知识片段计算。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from typing import Any, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]+")

# 进程级缓存失效版本号：技能脚本可能以不同模块名被加载多次（懒加载注册器、
# 测试重复导入），每个副本各有一份模块级缓存；知识写入方 bump 版本号后，
# 所有副本在下一次读取时都会判定缓存过期，不必互相持有对方的缓存对象
_BM25_EPOCH_LOCK = threading.Lock()
_BM25_EPOCH = 0


def bm25_cache_epoch() -> int:
    """当前 BM25 缓存版本（跨模块副本共享）。"""
    with _BM25_EPOCH_LOCK:
        return _BM25_EPOCH


def invalidate_bm25_cache() -> None:
    """使进程内所有 BM25 索引缓存失效（知识写入 / 删除后调用）。"""
    global _BM25_EPOCH
    with _BM25_EPOCH_LOCK:
        _BM25_EPOCH += 1


def tokenize(text: object) -> list[str]:
    """中文二元组 + 英文/数字词；单字中文保留为 unigram。"""
    tokens: list[str] = []
    for segment in _TOKEN_RE.findall(str(text or "").lower()):
        if segment.isascii():
            tokens.append(segment)
        elif len(segment) == 1:
            tokens.append(segment)
        else:
            tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
    return tokens


def _doc_sort_key(doc: dict[str, Any]) -> tuple[int, Any]:
    """同分文档的确定性次级排序键（数值 id 优先，其余按字符串）。"""
    raw = doc.get("id")
    if raw is not None:
        try:
            return (0, int(raw))
        except (TypeError, ValueError):
            pass
    return (1, str(raw or "") or str(doc.get("content") or "")[:80])


class BM25Index:
    """内存 Okapi BM25 索引（中文按二元组近似分词）。"""

    def __init__(
        self,
        documents: Sequence[dict[str, Any]],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.documents = list(documents)
        self.k1 = k1
        self.b = b
        # 逐篇切词后只保留长度与 Counter：完整 token 列表在初始化完成后
        # 无人读取，保存在实例上会让缓存索引（TTL 内常驻）白白放大内存
        self._doc_len: list[int] = []
        self._tf: list[Counter[str]] = []
        for doc in self.documents:
            tokens = tokenize(doc.get("content"))
            self._doc_len.append(len(tokens))
            self._tf.append(Counter(tokens))
        self._avgdl = (
            sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0
        )
        document_freq: Counter[str] = Counter()
        for tf in self._tf:
            document_freq.update(tf.keys())
        total = len(self.documents)
        self._idf = {
            term: math.log(1.0 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in document_freq.items()
        }

    def __len__(self) -> int:
        return len(self.documents)

    def search(self, query: object, top_k: int) -> list[tuple[dict[str, Any], float]]:
        """返回 ``[(document, bm25_score), ...]``，按分数降序，零分不返回。"""
        query_tokens = tokenize(query)
        if not query_tokens or not self.documents or top_k <= 0:
            return []
        # 二元查询词去重：重复词不能把 BM25 分翻倍（归一化基准按唯一查询
        # 词计，重复累计会让弱命中被截断成 1.0 绕过 min_score）
        unique_tokens = list(dict.fromkeys(query_tokens))
        scores: list[float] = []
        for index, tf in enumerate(self._tf):
            doc_len = self._doc_len[index]
            score = 0.0
            for term in unique_tokens:
                freq = tf.get(term)
                if not freq:
                    continue
                denominator = freq + self.k1 * (
                    1.0 - self.b + self.b * doc_len / self._avgdl
                )
                score += (
                    self._idf.get(term, 0.0)
                    * freq
                    * (self.k1 + 1.0)
                    / denominator
                )
            scores.append(score)
        ranked = sorted(
            range(len(self.documents)),
            # 同分时按 id 确定性排序：只按分数 + 文档插入序会让 top_k 边界
            # 的归属随扫描行序 / 索引重建漂移
            key=lambda index: (-scores[index], _doc_sort_key(self.documents[index])),
        )
        results: list[tuple[dict[str, Any], float]] = []
        for index in ranked[:top_k]:
            if scores[index] <= 0:
                break
            results.append((self.documents[index], scores[index]))
        return results

    def _unseen_idf(self) -> float:
        """查询里未出现在索引的词按 df=0 计 IDF（即最少见词的得分）。

        只在分子里命中「在库词」、基准却只累加在库词，会让内容词全部
        OOV 的查询基准坍缩为唯一在库的疑问词（如「怎么办」）：命中该词
        的片段归一后接近 1.0，绕过 ``min_score`` 把无关问题当有证据。
        """
        total = len(self.documents)
        if total <= 0:
            return 0.0
        return math.log(1.0 + (total + 0.5) / 0.5)

    def _query_idf_terms(self, query: object) -> list[tuple[str, float]]:
        """唯一查询词及其 IDF（OOV 按 df=0 的 IDF 计入）。"""
        unseen = self._unseen_idf()
        return [
            (term, self._idf.get(term, unseen))
            for term in set(tokenize(query))
        ]

    def query_reference(self, query: object) -> float:
        """归一化基准：平均长度下覆盖全部查询词的理想 BM25 得分。

        未出现在索引的查询词按 df=0 的 IDF 计入（不贡献分子、只抬高
        基准）：内容词全部 OOV 时，命中唯一在库疑问词的片段归一后仍低于
        ``min_score``。旧调用方沿用它即可；聊天链路使用按文档长度计的更
        精确口径（:meth:`normalized_score`）。
        """
        return sum(idf for _, idf in self._query_idf_terms(query))

    def normalization_reference(self, query: object, doc_len: int) -> float:
        """指定文档长度下覆盖全部查询词（各一次）的 BM25 满分。

        长度因子按文档自身长度计：长片段覆盖全部查询词也能到 1.0，
        短片段只覆盖部分查询词不会因长度被抬到阈值以上。
        """
        if not self.documents or self._avgdl <= 0:
            return 0.0
        norm = 1.0 - self.b + self.b * doc_len / self._avgdl
        saturation = (self.k1 + 1.0) / (1.0 + self.k1 * norm)
        return sum(idf for _, idf in self._query_idf_terms(query)) * saturation

    def normalized_score(self, query: object, content: object) -> float:
        """单篇内容的归一化 BM25（0–1，IDF 加权覆盖度 × 绝对证据量）。

        评分 = 覆盖度 × 绝对证据量：

        - 覆盖度 = 命中查询词的 IDF 之和 / 全部查询词（含 OOV）的 IDF 之和；
        - 绝对证据量 = ``1 - exp(-命中词 IDF 之和)``。

        不再使用 TF 饱和比值：旧口径里重复一个查询词就能把单篇分值抬到
        「覆盖全部查询词」的满分（``distance=max(余弦, 归一化BM25)`` 以
        1.0 通过 min_score 并排到首位），单 token 查询更是任何包含该词
        的片段恒为 1.0。IDF 加权覆盖度让「只命中一部分查询词」的片段
        按缺失词的质量缩水；绝对证据量让只命中一个高频词（idf≈1）的
        片段停在 0.5 上下，必须命中更多 / 更罕见的查询词才能接近 1.0。
        长度中性（不做文档长度归一）；TF 仍影响 BM25 排序本身。
        """
        terms = self._query_idf_terms(query)
        if not terms or not self.documents:
            return 0.0
        tf = Counter(tokenize(content))
        matched = [(term, idf) for term, idf in terms if tf.get(term)]
        if not matched:
            return 0.0
        # 跨词二元组（「磁悬浮怎么坐」里的 浮怎 / 么坐）是连续中文串按相邻
        # 字切词的伪影、不在任何文档里：按 unseen IDF 计入覆盖度分母会把
        # 真实命中的证据分稀释到 min_score 以下（内置库里 30 条官方问句有
        # 20 条最高归一化 BM25 不足 0.5）。当本文档命中一个足够稀有的在库
        # 查询词时，与该在库查询词共享字符的 OOV 二元组视为切词伪影、
        # 不计入分母；真正的内容 OOV 词（与任何在库查询词都不共享字符，
        # 如「结婚」「请假」）仍按 unseen 计入，保留「内容词全 OOV 时
        # 基准不坍缩」的防护。
        unseen = self._unseen_idf()
        rare_hit = unseen > 0 and any(idf >= unseen / 2 for _, idf in matched)
        in_library_chars: set[str] = set()
        if rare_hit:
            for term, _ in terms:
                if term in self._idf:
                    in_library_chars.update(term)
        effective_terms = [
            (term, idf)
            for term, idf in terms
            if not (
                rare_hit
                and term not in self._idf
                and not term.isascii()
                and set(term) & in_library_chars
            )
        ]
        total_idf = sum(idf for _, idf in effective_terms)
        matched_idf = sum(idf for _, idf in matched)
        if total_idf <= 0 or matched_idf <= 0:
            return 0.0
        coverage = matched_idf / total_idf
        evidence = 1.0 - math.exp(-matched_idf)
        return coverage * evidence


def rrf_fuse(
    ranked_lists: Sequence[Sequence[dict[str, Any]]],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：``score = Σ 1 / (k + rank + 1)``。

    同一 id 在多路出现时分数累加；返回按 RRF 分数降序的新列表，
    不改写输入文档。
    """
    denominator = max(1, int(k))
    fused: dict[str, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked):
            key = str(doc.get("id") or "") or str(doc.get("content") or "")[:80]
            entry = fused.get(key)
            if entry is None:
                entry = {"document": dict(doc), "rrf_score": 0.0}
                fused[key] = entry
            entry["rrf_score"] += 1.0 / (denominator + rank + 1)
    return [
        {**fused[key]["document"], "rrf_score": fused[key]["rrf_score"]}
        for key in sorted(
            fused,
            key=lambda item: (-fused[item]["rrf_score"], item),
        )
    ]


def annotate_hybrid_scores(
    fused_documents: Sequence[dict[str, Any]],
    vector_distances: dict[str, float],
    bm25_scores: dict[str, float],
    *,
    bm25_reference: float | None = None,
    bm25_normalized: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """为融合结果写入可解释分数，并生成用于阈值过滤的 ``distance``。

    ``distance`` = max(向量相似度, 归一化 BM25)。这样既保留既有
    ``min_score`` 的「0–1、越高越相关」语义，又让只在关键词路命中
    （向量分低）的片段有机会通过阈值。

    归一化口径优先用 ``bm25_normalized``（``BM25Index.normalized_score``：
    IDF 加权覆盖度 × 绝对证据量，单一高频词 / 部分覆盖不会到满分）。兼容
    传入 ``bm25_reference`` 的旧口径（平均长度理想得分，长片段会因长度
    惩罚被压低）；两者都缺省时退回「本次最高 BM25 分」——top-1 永远
    是 1.0，仅为兼容旧调用方保留。
    """
    if bm25_normalized is None:
        if bm25_reference is not None and bm25_reference > 0:
            reference = float(bm25_reference)
        else:
            reference = max(bm25_scores.values(), default=0.0)
    else:
        reference = 0.0
    annotated: list[dict[str, Any]] = []
    for doc in fused_documents:
        key = str(doc.get("id") or "")
        vector_score = float(vector_distances.get(key, 0.0) or 0.0)
        bm25_score = float(bm25_scores.get(key, 0.0) or 0.0)
        if bm25_normalized is not None:
            normalized = min(
                1.0, max(0.0, float(bm25_normalized.get(key, 0.0) or 0.0))
            )
        elif reference > 0:
            normalized = min(1.0, bm25_score / reference)
        else:
            normalized = 0.0
        annotated.append(
            {
                **doc,
                "vector_distance": vector_score,
                "bm25_score": bm25_score,
                "distance": max(vector_score, normalized),
            }
        )
    return annotated
