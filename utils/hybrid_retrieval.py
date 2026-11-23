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
        self._tokens = [
            tokenize(doc.get("content")) for doc in self.documents
        ]
        self._doc_len = [len(tokens) for tokens in self._tokens]
        self._avgdl = (
            sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0
        )
        self._tf = [Counter(tokens) for tokens in self._tokens]
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
        scores: list[float] = []
        for index, tf in enumerate(self._tf):
            doc_len = self._doc_len[index]
            score = 0.0
            for term in query_tokens:
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
            range(len(self.documents)), key=lambda index: scores[index], reverse=True
        )
        results: list[tuple[dict[str, Any], float]] = []
        for index in ranked[:top_k]:
            if scores[index] <= 0:
                break
            results.append((self.documents[index], scores[index]))
        return results

    def query_reference(self, query: object) -> float:
        """归一化基准：覆盖全部查询词、长度取平均的理想片段得分。

        取查询词 IDF 之和（文档平均长度下每个词一次的 BM25 贡献恰为
        ``idf``）。用「本次结果的最高分」归一化会让任何查询的 top-1 都
        变成 1.0，弱关键词命中也绕过 ``min_score``；按查询覆盖面归一化
        后，只命中少量常见词的片段分数自然低于阈值。
        """
        return sum(self._idf.get(term, 0.0) for term in set(tokenize(query)))


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
            fused, key=lambda item: fused[item]["rrf_score"], reverse=True
        )
    ]


def annotate_hybrid_scores(
    fused_documents: Sequence[dict[str, Any]],
    vector_distances: dict[str, float],
    bm25_scores: dict[str, float],
    *,
    bm25_reference: float | None = None,
) -> list[dict[str, Any]]:
    """为融合结果写入可解释分数，并生成用于阈值过滤的 ``distance``。

    ``distance`` = max(向量相似度, 归一化 BM25)。这样既保留既有
    ``min_score`` 的「0–1、越高越相关」语义，又让只在关键词路命中
    （向量分低）的片段有机会通过阈值。

    ``bm25_reference`` 应传 ``BM25Index.query_reference(query)``（查询词
    IDF 之和）。缺省退回「本次最高 BM25 分」的旧口径：top-1 永远是
    1.0，弱命中也会绕过阈值，仅为兼容旧调用方保留。
    """
    if bm25_reference is not None and bm25_reference > 0:
        reference = float(bm25_reference)
    else:
        reference = max(bm25_scores.values(), default=0.0)
    annotated: list[dict[str, Any]] = []
    for doc in fused_documents:
        key = str(doc.get("id") or "")
        vector_score = float(vector_distances.get(key, 0.0) or 0.0)
        bm25_score = float(bm25_scores.get(key, 0.0) or 0.0)
        normalized = (
            min(1.0, bm25_score / reference) if reference > 0 else 0.0
        )
        annotated.append(
            {
                **doc,
                "vector_distance": vector_score,
                "bm25_score": bm25_score,
                "distance": max(vector_score, normalized),
            }
        )
    return annotated
