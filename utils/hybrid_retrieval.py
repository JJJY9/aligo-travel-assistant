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
from collections import Counter
from typing import Any, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]+")


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
) -> list[dict[str, Any]]:
    """为融合结果写入可解释分数，并生成用于阈值过滤的 ``distance``。

    ``distance`` = max(向量相似度, 归一化 BM25)。这样既保留既有
    ``min_score`` 的「0–1、越高越相关」语义，又让只在关键词路命中
    （向量分低）的片段有机会通过阈值。
    """
    max_bm25 = max(bm25_scores.values(), default=0.0)
    annotated: list[dict[str, Any]] = []
    for doc in fused_documents:
        key = str(doc.get("id") or "")
        vector_score = float(vector_distances.get(key, 0.0) or 0.0)
        bm25_score = float(bm25_scores.get(key, 0.0) or 0.0)
        normalized = (bm25_score / max_bm25) if max_bm25 > 0 else 0.0
        annotated.append(
            {
                **doc,
                "vector_distance": vector_score,
                "bm25_score": bm25_score,
                "distance": max(vector_score, normalized),
            }
        )
    return annotated
