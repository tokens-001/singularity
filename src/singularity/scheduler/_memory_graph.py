from singularity.scheduler._memory_core import *  # noqa: F401,F403
from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events
import json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

__all__ = ['_expand_node', '_rrf_anchors', 'add_inferred_causal_edge', 'find_by_files', 'find_candidate_latent_edges', 'find_causal_chain', 'find_similar', 'query', 'synthesize', 'traverse']
# Stage 2: Multi-Signal Anchor Identification (RRF)
# ═══════════════════════════════════════════════════════════

def _rrf_anchors(
    query_tokens: list[float],
    query_text: str,
    events: dict[str, EventNode],
    edges: dict,
    k: int = 60,
    top_n: int = 5,
) -> list[tuple[str, float]]:
    """RRF 融合三信号找入口节点。

    Signal 1 — Semantic: cosine(query_emb, event.emb)
    Signal 2 — Lexical:  关键词命中 event.content + event.attrs['files']
    Signal 3 — Temporal:  时间衰减 1/(1 + hours_ago)
    """
    now = time.time()
    signals: list[tuple[str, dict[str, float]]] = []

    # Signal 1: Semantic
    sem_scores: dict[str, float] = {}
    for tid, node in events.items():
        s = _cosine_sim(query_tokens, node.emb)
        if s > 0:
            sem_scores[tid] = s
    signals.append(("semantic", sem_scores))

    # Signal 2: Lexical (keyword substring + file path overlap)
    lex_scores: dict[str, float] = {}
    query_lower = query_text.lower()
    # 提取查询中的关键词 (中文双字 + 英文单词)
    q_keywords = set()
    for seg in re.findall(r"[一-鿿]{2,}", query_text):
        q_keywords.add(seg)
    for w in re.findall(r"[a-zA-Z0-9_./]{2,}", query_text):
        q_keywords.add(w.lower())
    for tid, node in events.items():
        score = 0.0
        # 描述命中: 关键词子串匹配
        desc_lower = node.content.lower()
        for kw in q_keywords:
            if kw in desc_lower:
                score += 0.5
        # 文件路径命中
        for fp in node.attrs.get("files", []):
            fp_lower = fp.lower()
            for kw in q_keywords:
                if kw in fp_lower:
                    score += 1.0
        if score > 0:
            lex_scores[tid] = score
    signals.append(("lexical", lex_scores))

    # Signal 3: Temporal recency
    temp_scores: dict[str, float] = {}
    for tid, node in events.items():
        hours_ago = (now - node.timestamp) / 3600
        temp_scores[tid] = 1.0 / (1.0 + hours_ago)
    signals.append(("temporal", temp_scores))

    # 集合并 (Omni-SimpleMem): 三信号 top-k 取并集，优于 RRF 加权融合
    anchors: set[str] = set()
    for _sig_name, scores in signals:
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
        anchors.update(tid for tid, _ in ranked)

    # 重新按语义分排序 (保留最高分作为 tiebreaker)
    anchor_scores: list[tuple[str, float]] = []
    for tid in anchors:
        best = max(sem_scores.get(tid, 0.0), lex_scores.get(tid, 0.0), temp_scores.get(tid, 0.0))
        anchor_scores.append((tid, best))
    return sorted(anchor_scores, key=lambda x: -x[1])[:top_n]


# ═══════════════════════════════════════════════════════════
# Stage 3: Adaptive Traversal (Heuristic Beam Search)
# ═══════════════════════════════════════════════════════════

def _expand_node(
    task_id: str,
    edges: dict,
    events: dict[str, EventNode],
) -> list[tuple[str, str]]:
    """从一个节点扩展所有出边。

    返回 [(neighbor_id, edge_type), ...]。
    """
    neighbors: list[tuple[str, str]] = []

    # 语义邻居 (无向)
    for src, dst, _sim in edges.get("semantic", []):
        if src == task_id and dst in events:
            neighbors.append((dst, EdgeType.SEMANTIC))
        elif dst == task_id and src in events:
            neighbors.append((src, EdgeType.SEMANTIC))

    # 时间邻居 (向后: task 之后的事件)
    for src, dst in edges.get("temporal", []):
        if src == task_id and dst in events:
            neighbors.append((dst, EdgeType.TEMPORAL))

    # 因果邻居 (向前: task causes X)
    for src, dst, _source in edges.get("causal", []):
        if src == task_id and dst in events:
            neighbors.append((dst, EdgeType.CAUSAL))
        elif dst == task_id and src in events:
            # 反向: 谁 caused task (溯源)
            neighbors.append((src, EdgeType.CAUSAL))

    # 实体邻居 (共享文件)
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}
    node = events.get(task_id)
    if node:
        for fp in node.attrs.get("files", []):
            for other_id in entity_idx.get(fp, []):
                if other_id != task_id and other_id in events:
                    neighbors.append((other_id, EdgeType.ENTITY))

    return neighbors


def traverse(
    query: str,
    beam_width: int = 3,
    max_hops: int = 3,
    alpha: float = 0.6,
) -> list[dict]:
    """Stage 3: 意图驱动的 Adaptive Traversal (Beam Search)。

    论文公式:
      S(n_i → n_j, e_k) = α·sim(n_j, q) + (1-α)·Ψ(e_k, I_q)

    参数:
      query      : 查询文本
      beam_width : Beam 宽度 K
      max_hops   : 最大跳数 H
      alpha      : 语义 vs 结构权重 (0.6=偏语义, 论文建议)

    返回:
      [{task_id, description, score, path: [(from_id, edge_type), ...]}, ...]
    """
    events = _load_events()
    edges = _load_edges()
    if not events:
        return []

    # Stage 1: 意图分类 + 边权重
    intent = detect_intent(query)
    w = _INTENT_EDGE_WEIGHTS.get(intent, _INTENT_EDGE_WEIGHTS["semantic"])
    query_tokens = _embed(query)

    # Stage 2: RRF 锚点
    anchors = _rrf_anchors(query_tokens, query, events, edges, top_n=beam_width)
    if not anchors:
        return []

    # Beam: [(task_id, cumulative_score, path)], path 用于溯源
    beam: list[tuple[str, float, list[tuple[str, str]]]] = [
        (tid, score, []) for tid, score in anchors
    ]
    visited: set[str] = {tid for tid, _, _ in beam}

    for _hop in range(max_hops):
        candidates: list[tuple[str, float, list[tuple[str, str]]]] = []

        for task_id, cum_score, path in beam:
            # 保留当前节点自身 (锚点不会被挤出 top-K)
            candidates.append((task_id, cum_score, path))

            neighbors = _expand_node(task_id, edges, events)

            for neighbor_id, edge_type in neighbors:
                if neighbor_id in visited:
                    continue

                neighbor = events.get(neighbor_id)
                if not neighbor:
                    continue

                # 语义部分: sim(n_j, q) — cosine on tokens
                sim = _cosine_sim(query_tokens, neighbor.emb)

                # 结构部分: Ψ(e_k, I_q) — intent-weighted edge bonus
                structural = w.get(edge_type, 0.3)

                # Transition score
                step_score = alpha * sim + (1.0 - alpha) * structural

                if step_score <= 0:
                    continue

                new_score = cum_score * step_score
                new_path = path + [(task_id, edge_type)]

                candidates.append((neighbor_id, new_score, new_path))

        if len(candidates) <= len(beam):
            break  # 无新节点可扩展

        # 保留 top-K (含锚点自身)
        candidates.sort(key=lambda x: -x[1])
        # 去重: 同一节点取最高分
        seen: dict[str, int] = {}
        deduped: list[tuple[str, float, list]] = []
        for tid, score, p in candidates:
            if tid not in seen:
                seen[tid] = len(deduped)
                deduped.append((tid, score, p))
            elif score > deduped[seen[tid]][1]:
                deduped[seen[tid]] = (tid, score, p)
        beam = deduped[:beam_width]
        visited.update(tid for tid, _, _ in beam)

    # 格式化结果
    results: list[dict] = []
    anchor_ids = {a[0] for a in anchors}
    for task_id, score, path in beam:
        node = events.get(task_id)
        if not node:
            continue
        is_anchor = task_id in anchor_ids
        if not path:
            results.append({
                "task_id": task_id,
                "description": node.content[:120],
                "score": round(score, 4),
                "path": [],
                "graph_sources": ["anchor"] if is_anchor else [],
                "timestamp": node.timestamp,
            })
        else:
            edge_types = list(set(et for _, et in path))
            sources = edge_types.copy()
            if is_anchor:
                sources.insert(0, "anchor")
            results.append({
                "task_id": task_id,
                "description": node.content[:120],
                "score": round(score, 4),
                "path": [(src[-8:] if len(src) >= 8 else src, et) for src, et in path],
                "graph_sources": sources,
                "timestamp": node.timestamp,
            })

    # 去重 (同节点不同路径 → 保留最高分)
    seen: dict[str, dict] = {}
    for r in results:
        tid = r["task_id"]
        if tid not in seen or r["score"] > seen[tid]["score"]:
            seen[tid] = r
    return sorted(seen.values(), key=lambda x: -x["score"])


# ═══════════════════════════════════════════════════════════
# Stage 4: Narrative Synthesis (Graph Linearization)
# ═══════════════════════════════════════════════════════════

def synthesize(results: list[dict], query: str) -> dict:
    """Stage 4: 叙事合成 — 拓扑排序 + 溯源 + 显著性预算。

    返回:
      {
        "narrative": [...],       # 按意图拓扑排序的结果
        "intent": str,            # 检测到的查询意图
        "graph_coverage": {...},  # 各图命中统计
        "query": str,
      }
    """
    intent = detect_intent(query)

    # 拓扑排序
    if intent == "causal":
        # 因果排序: causal 边深度优先
        def _causal_depth(r: dict) -> int:
            return sum(1 for _, et in r.get("path", []) if et == EdgeType.CAUSAL)
        sorted_results = sorted(results, key=lambda r: (-_causal_depth(r), -r["score"]))
    elif intent == "temporal":
        sorted_results = sorted(results, key=lambda r: r.get("timestamp", 0))
    else:
        sorted_results = sorted(results, key=lambda r: -r["score"])

    # 图覆盖统计
    coverage: dict[str, int] = defaultdict(int)
    for r in sorted_results:
        for src in r.get("graph_sources", []):
            coverage[src] += 1

    # 显著性预算: 前面结果保留完整, 后面截断
    narrative: list[dict] = []
    total_chars = 0
    budget = 600  # 字符预算

    for r in sorted_results:
        desc = r.get("description", "")
        if total_chars + len(desc) > budget and narrative:
            # 截断: 标记为超出预算
            narrative.append({
                **r,
                "truncated": True,
                "description": desc[:60] + "...",
            })
        else:
            narrative.append(r)
            total_chars += len(desc)

    return {
        "narrative": narrative,
        "intent": intent,
        "graph_coverage": dict(coverage),
        "total_results": len(sorted_results),
        "budget_exceeded": len(sorted_results) > len(narrative),
        "query": query,
    }


# ═══════════════════════════════════════════════════════════
# 高级查询入口: 完整 Stage 1→4 流水线
# ═══════════════════════════════════════════════════════════

def query(
    description: str,
    files: list[str] | None = None,
    beam_width: int = 3,
    max_hops: int = 3,
    max_depth: int = 1,
    mem_type: str = "",
) -> dict:
    """MAGMA 完整查询流水线（金字塔渐进检索 — 受 Omni-SimpleMem 启发）。

    mem_type: 可选过滤类型 (architecture/bug_fix/decision/code_change/docs)，空=不过滤

    Stage 1: 意图分类 → 边权重
    Stage 2: RRF 锚点识别
    Stage 3: Adaptive Beam Search 遍历
    Stage 4: Narrative Synthesis

    max_depth 控制检索深度：
      1 (默认) = 仅语义摘要搜索（快速，毫秒级，适合 90% 场景）
      2 = 摘要 + Beam Search 遍历（中等成本）
      3 = 深度搜索 + 实体图 + 全文（最全但最慢，按需触发）
    """
    stats_data = stats()
    results: list[dict] = []
    entity_matches: dict[str, list[str]] = {}
    semantic_only: list[dict] = []

    # ── Depth 1: 语义摘要层（最快） ──
    semantic_only = find_similar(description, top_k=5)

    if max_depth >= 2:
        # ── Depth 2: Beam Search 遍历层 ──
        results = traverse(description, beam_width=beam_width, max_hops=max_hops)

    if max_depth >= 3:
        # ── Depth 3: 实体图 + 全文匹配层（最全） ──
        if files:
            entity_matches = find_by_files(files)
        # 合成时传入全文上下文
        narrative = synthesize(results, description) if results else {
            "summary": "无深度遍历结果",
            "nodes": [],
            "synthesis_model": "none",
        }
    elif max_depth == 2:
        narrative = synthesize(results, description) if results else {
            "summary": "无遍历结果",
            "nodes": [],
            "synthesis_model": "none",
        }
    else:
        # Depth 1: 纯语义摘要，不做 traversal synthesis
        narrative = {
            "summary": f"语义检索命中 {len(semantic_only)} 条记录",
            "nodes": [
                {"task_id": s["task_id"], "description": s["description"][:120],
                 "similarity": s["similarity"]}
                for s in semantic_only[:3]
            ],
            "synthesis_model": "semantic_only",
        }

    # ── 按记忆类型过滤 ──
    if mem_type:
        events = _load_events()
        semantic_only = [
            s for s in semantic_only
            if events.get(s["task_id"], EventNode("", "", 0)).attrs.get("mem_type", "") == mem_type
        ]

    return {
        "traversal": narrative,
        "entity_matches": entity_matches,
        "semantic_baseline": [
            {"task_id": s["task_id"], "similarity": s["similarity"],
             "description": s["description"][:80]}
            for s in semantic_only
        ],
        "stats": stats_data,
        "depth_used": max_depth,
    }


# ═══════════════════════════════════════════════════════════
# 直接查询 (不走遍历, 兼容旧 API)
# ═══════════════════════════════════════════════════════════

def find_by_files(files: list[str]) -> dict[str, list[str]]:
    """实体图直查: 哪些任务改过这些文件？"""
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}
    result: dict[str, list[str]] = {}
    for fp in files:
        matches = entity_idx.get(fp, [])
        if matches:
            result[fp] = matches
    return result


def find_similar(description: str, top_k: int = 5) -> list[dict]:
    """语义图直查: cosine 相似度 (不走遍历, 快但浅)。"""
    events = _load_events()
    query_tokens = _embed(description)
    if not query_tokens or not events:
        return []

    scored: list[dict] = []
    for tid, node in events.items():
        sim = _cosine_sim(query_tokens, node.emb)
        if sim > 0:
            scored.append({
                "task_id": tid,
                "description": node.content[:120],
                "similarity": round(sim, 4),
                "timestamp": node.timestamp,
            })

    scored.sort(key=lambda x: (-x["similarity"], -x["timestamp"]))
    return scored[:top_k]


def find_causal_chain(task_id: str, direction: str = "up") -> list[dict]:
    """因果图 BFS: 溯源(up) / 追果(down) / 双向(both)。"""
    edges = _load_edges()
    events = _load_events()

    result: list[dict] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(task_id, 0)]

    while queue:
        tid, depth = queue.pop(0)
        if tid in visited or depth > 5:
            continue
        visited.add(tid)

        node = events.get(tid)
        desc = node.content[:80] if node else ""

        if tid != task_id:
            result.append({"task_id": tid, "description": desc, "depth": depth})

        # 找因果邻居
        for src, dst, _source in edges.get("causal", []):
            if direction in ("up", "both") and dst == tid and src not in visited:
                queue.append((src, depth + 1))
            if direction in ("down", "both") and src == tid and dst not in visited:
                queue.append((dst, depth + 1))

    return result


# ═══════════════════════════════════════════════════════════
# 慢通道: 异步结构化整合 (Slow Path — "Structural Consolidation")
# ═══════════════════════════════════════════════════════════

def find_candidate_latent_edges() -> list[dict]:
    """慢通道候选发现: 找出共享文件但没有显式因果边的任务对。

    这些候选需要 LLM 推理确认 → 真正的因果/实体边。
    当前返回候选列表, LLM 推理由外部调度 (见 orchestrator._run_slow_consolidation)。

    返回:
      [{task_a, task_b, shared_files, semantic_sim, time_gap_hours}, ...]
    """
    events = _load_events()
    edges = _load_edges()
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}

    # 现有因果边集合 (无向, 用于判断是否已有边)
    existing_causal: set[tuple[str, str]] = set()
    for src, dst, _source in edges.get("causal", []):
        existing_causal.add((src, dst))
        existing_causal.add((dst, src))

    candidates: list[dict] = []
    # 按文件分组找共现
    file_tasks: dict[str, list[str]] = defaultdict(list)
    for tid, node in events.items():
        for fp in node.attrs.get("files", []):
            file_tasks[fp].append(tid)

    seen_pairs: set[tuple[str, str]] = set()
    for fp, tid_list in file_tasks.items():
        for i in range(len(tid_list)):
            for j in range(i + 1, len(tid_list)):
                pair = tuple(sorted([tid_list[i], tid_list[j]]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if pair in existing_causal:
                    continue

                a, b = pair
                node_a = events.get(a)
                node_b = events.get(b)
                if not node_a or not node_b:
                    continue

                shared = list(set(node_a.attrs.get("files", [])) &
                              set(node_b.attrs.get("files", [])))
                sim = _cosine_sim(node_a.emb, node_b.emb)
                time_gap = abs(node_a.timestamp - node_b.timestamp) / 3600

                candidates.append({
                    "task_a": a,
                    "task_b": b,
                    "desc_a": node_a.content[:60],
                    "desc_b": node_b.content[:60],
                    "shared_files": shared,
                    "semantic_sim": round(sim, 4),
                    "time_gap_hours": round(time_gap, 1),
                })

    # 排序: 高语义相似 + 小时间间隔优先
    candidates.sort(key=lambda c: (-c["semantic_sim"], c["time_gap_hours"]))
    return candidates


def add_inferred_causal_edge(src: str, dst: str, reason: str = "") -> None:
    """慢通道: 加入 LLM 推理确认的因果边。"""
    edges = _load_edges()
    # 去重
    if not any(e[0] == src and e[1] == dst for e in edges["causal"]):
        edges["causal"].append((src, dst, f"inferred: {reason}" if reason else "inferred"))
    _save_edges(edges)


# ═══════════════════════════════════════════════════════════
