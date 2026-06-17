#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hybrid Search: BM25 + Semantic RRF Fusion
==========================================
Reciprocal Rank Fusion — 关键词精度 + 语义召回
"""


def rrf_fusion(
    bm25_results: list[dict],
    semantic_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion 融合两个排序列表。

    bm25_results / semantic_results: 各自按 score 降序排列的 [{id, score, ...}, ...]
    k: RRF 常数（默认 60，业界标准）

    返回按 RRF 分数降序的合并列表，每个结果加 rrf_score 字段。
    RRF(doc, rank) = 1/(k + rank + 1)，多来源求和。
    """
    rrf_scores: dict[str, float] = {}
    doc_cache: dict[str, dict] = {}

    for rank, r in enumerate(bm25_results):
        did = r["id"]
        rrf_scores[did] = rrf_scores.get(did, 0) + 1.0 / (k + rank + 1)
        # 优先存 BM25 结果（元数据更完整）
        doc_cache.setdefault(did, r)

    for rank, r in enumerate(semantic_results):
        did = r["id"]
        rrf_scores[did] = rrf_scores.get(did, 0) + 1.0 / (k + rank + 1)
        # 语义结果覆盖（可能带 source 字段）
        if did in doc_cache:
            doc_cache[did]["semantic_score"] = r["score"]
            doc_cache[did]["semantic_rank"] = rank + 1
        else:
            doc_cache[did] = r

    ranked = sorted(rrf_scores.items(), key=lambda x: -x[1])
    return [
        {
            **doc_cache[did],
            "rrf_score": round(score, 4),
            "source": "hybrid",
        }
        for did, score in ranked
    ]
