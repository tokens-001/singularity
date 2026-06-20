"""memory.py — MAGMA 多图记忆 (arXiv:2601.03236 完整实现)

四图架构:
  G_sem   — 语义图 (无向): 概念相似性边
  G_temp  — 时间图 (有向, 严格序): 事件时间线
  G_causal— 因果图 (有向): 逻辑蕴含边, 支持 "Why" 查询
  G_entity— 实体图 (事件→实体节点): 跨时间线的对象持久性

核心算法:
  Stage 1 — Query Analysis: 意图分类 + 时间解析 + 表示提取
  Stage 2 — Multi-Signal Anchors: RRF 融合语义/词法/时间三信号定位入口
  Stage 3 — Adaptive Traversal: 意图驱动的 Beam Search 跨图多跳遍历
  Stage 4 — Narrative Synthesis: 拓扑排序 + 溯源元数据 + 显著性预算

Dual-Stream:
  Fast Path  — 同步摄入 (embedding+时间骨架), 零 LLM
  Slow Path  — 异步后台 LLM 推理隐含因果/实体边
"""

from __future__ import annotations
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

from . import config as sched_config

# ═══════════════════════════════════════════════════════════
# 存储路径
# ═══════════════════════════════════════════════════════════

_MEMORY_DIR = sched_config.QIDIAN_DIR / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events.json"          # EventNode 持久化
_EDGES_PATH = _MEMORY_DIR / "edges.json"            # 所有图边
_ENTITY_IDX_PATH = _MEMORY_DIR / "entity_index.json"  # file→task_ids 倒排


def _ensure_dir() -> None:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict | list:
    if not path.exists():
        return {} if path.suffix == ".json" else []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {} if ".json" in str(path) else []


def _write_json(path: Path, data: dict | list) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ═══════════════════════════════════════════════════════════
# EventNode — 论文定义: n_i = (content, t_i, v_i, A_i)
# ═══════════════════════════════════════════════════════════

@dataclass
class EventNode:
    """MAGMA 事件节点。

    content  : 任务描述文本
    timestamp: 创建时间戳 (t_i)
    emb      : sentence-transformers embedding (384-dim)
    attrs    : 结构化属性 (A_i): files, status, route_level, route_type, snapshot_id
    """
    task_id: str
    content: str
    timestamp: float
    emb: list[float] = field(default_factory=list)  # embedding (384-dim)
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content[:200],
            "timestamp": self.timestamp,
            "emb": self.emb,
            "attrs": self.attrs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EventNode":
        return cls(
            task_id=d["task_id"],
            content=d.get("content", ""),
            timestamp=d.get("timestamp", 0),
            emb=list(d.get("emb", d.get("tokens", []))),  # backward compat
            attrs=d.get("attrs", {}),
        )


# ═══════════════════════════════════════════════════════════
# 图边类型
# ═══════════════════════════════════════════════════════════

class EdgeType:
    SEMANTIC = "semantic"    # 概念相似
    TEMPORAL = "temporal"    # 时间先后 (→ 方向)
    CAUSAL = "causal"        # 因果 (→ 方向)
    ENTITY = "entity"        # 共享实体


# ═══════════════════════════════════════════════════════════
# 中文分词
# ═══════════════════════════════════════════════════════════

_EMBED_MODEL = None
# 模块加载时抑制HF/transformers日志
import logging as _hf_log
_hf_log.getLogger("sentence_transformers").setLevel(_hf_log.ERROR)
_hf_log.getLogger("transformers").setLevel(_hf_log.ERROR)
def _get_embed_model():
    """懒加载: 首次查询才下载/加载模型(420MB)。"""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        import os, time
        # 检查是否在 CI/快速模式 — 跳过模型加载
        if os.environ.get("QIDIAN_SKIP_EMBED"):
            _EMBED_MODEL = False
            return None
        import sys, io, logging as _log
        _log.getLogger("sentence_transformers").setLevel(_log.ERROR)
        _log.getLogger("transformers").setLevel(_log.ERROR)
        from sentence_transformers import SentenceTransformer
        _stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            _EMBED_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        finally:
            sys.stderr = _stderr
    return _EMBED_MODEL if _EMBED_MODEL is not False else None

def _embed(text: str) -> list[float]:
    """384维归一化向量。空文本或无模型时返回空列表。"""
    if not text or not text.strip():
        return []
    model = _get_embed_model()
    if model is None:
        return []
    return model.encode(text.strip(), normalize_embeddings=True).tolist()


# ═══════════════════════════════════════════════════════════
# cosine 相似度
# ═══════════════════════════════════════════════════════════

def _cosine_sim(a, b) -> float:
    """余弦相似度。a,b为embedding列表。"""
    if not a or not b:
        return 0.0
    import math
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(y*y for y in b))
    if na==0 or nb==0:
        return 0.0
    return dot/(na*nb)


# ═══════════════════════════════════════════════════════════
# Stage 1: 意图分类
# ═══════════════════════════════════════════════════════════

_INTENT_PATTERNS = {
    "causal":   ["为什么", "原因", "理由", "动机", "导致", "引起", "触发", "根源", "为啥", "因为", "所以"],
    "temporal": ["什么时候", "何时", "先后", "顺序", "流程", "步骤", "之前", "之后", "最早", "最近", "历史"],
    "entity":   ["谁", "哪个", "哪些", "什么文件", "什么模块", "在哪里", "文件", "模块", "函数", "类"],
}


def detect_intent(query: str) -> str:
    """意图分类 → causal | temporal | entity | semantic (默认)。"""
    query_lower = query.lower()
    scores = {intent: sum(1 for kw in kws if kw in query_lower)
              for intent, kws in _INTENT_PATTERNS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "semantic"


# 意图 → 边类型权重 (论文 Ψ 函数: 自适应结构对齐)
# 权重高 = 该意图下优先走这类边。
#
# 设计原则:
#   - 主导边 = 1.0 (意图命中的主边)
#   - 辅助边 = 0.2~0.5 (次要探索方向)
#   - 跨意图对称: causal 意图下 entity=0.5 (因果常伴实体),
#                   entity 意图下 semantic=0.5 (找人/物后可语义扩散)
#
# 校准状态: 初始启发值, 未做 A/B 测试。
# 校准方法: 对金标查询集跑 grid search, 最大化 recall@5。
#           或用在线 bandit (每查询记录点击, 定期调权)。
_INTENT_EDGE_WEIGHTS = {
    "causal":   {EdgeType.CAUSAL: 1.0, EdgeType.SEMANTIC: 0.4, EdgeType.TEMPORAL: 0.3, EdgeType.ENTITY: 0.5},
    "temporal": {EdgeType.TEMPORAL: 1.0, EdgeType.CAUSAL: 0.3, EdgeType.SEMANTIC: 0.3, EdgeType.ENTITY: 0.2},
    "entity":   {EdgeType.ENTITY: 1.0, EdgeType.SEMANTIC: 0.5, EdgeType.CAUSAL: 0.2, EdgeType.TEMPORAL: 0.2},
    "semantic": {EdgeType.SEMANTIC: 1.0, EdgeType.CAUSAL: 0.5, EdgeType.TEMPORAL: 0.5, EdgeType.ENTITY: 0.5},
}


# ═══════════════════════════════════════════════════════════
# 快通道: 同步摄入 (Fast Path — "Synaptic Ingestion")
# ═══════════════════════════════════════════════════════════

def _load_events() -> dict[str, EventNode]:
    """加载全部事件节点。"""
    raw: dict = _read_json(_EVENTS_PATH) or {}
    return {tid: EventNode.from_dict(d) for tid, d in raw.items()}


def _save_events(events: dict[str, EventNode]) -> None:
    _write_json(_EVENTS_PATH, {tid: n.to_dict() for tid, n in events.items()})


def _load_edges() -> dict:
    """加载边存储。

    edges = {
      "semantic":  [(src, dst, sim), ...],
      "temporal":  [(src, dst), ...],      # 方向: 早→晚
      "causal":    [(src, dst, source), ...],  # source: "explicit"|"inferred"
      "entity":    [(task_id, file_path), ...],  # 任务→实体
    }
    """
    default = {"semantic": [], "temporal": [], "causal": [], "entity": []}
    raw: dict = _read_json(_EDGES_PATH) or {}
    for k in default:
        raw.setdefault(k, [])
    return raw


def _save_edges(edges: dict) -> None:
    _write_json(_EDGES_PATH, edges)


def index_task(
    task_id: str,
    description: str,
    changed_files: list[str] | None = None,
    depends_on: list[str] | None = None,
    created_at: float | None = None,
) -> None:
    """快通道摄入: 创建 EventNode + 更新四图边。

    - embedding 向量 (384-dim)
    - 追加时间链
    - 添显式因果边 (depends_on)
    - 连实体边 (changed_files)
    - 重算语义边 (增量更新)
    """
    changed_files = changed_files or []
    depends_on = depends_on or []
    if created_at is None:
        created_at = time.time()

    _ensure_dir()

    # ── 选择性摄入 (Omni-SimpleMem): Jaccard 对比最近摘要 ──
    events = _load_events()
    # 只看最近 20 条事件 (O(1), 原文用 "recent summaries")
    recent = sorted(events.items(), key=lambda x: -x[1].timestamp)[:20]
    desc_words = set(description.lower().split())
    for existing_id, existing_node in recent:
        existing_words = set(existing_node.content.lower().split())
        if desc_words and existing_words:
            jaccard = len(desc_words & existing_words) / len(desc_words | existing_words)
            if jaccard > 0.75:
                return  # 高度重复，跳过 index

    # ── EventNode ──
    tokens = _embed(description)
    node = EventNode(
        task_id=task_id,
        content=description,
        timestamp=created_at,
        emb=tokens,
        attrs={"files": changed_files, "depends_on": depends_on},
    )
    events[task_id] = node
    _save_events(events)

    # ── 边 ──
    edges = _load_edges()

    # 实体边: task → file
    for fp in changed_files:
        edges["entity"].append((task_id, fp))

    # 因果边: dep_id → task_id (dep causes task)
    for dep_id in depends_on:
        if dep_id in events:
            edges["causal"].append((dep_id, task_id, "explicit"))

    # 时间边: 找前一个事件
    sorted_events = sorted(events.items(), key=lambda x: x[1].timestamp)
    idx = next((i for i, (tid, _) in enumerate(sorted_events) if tid == task_id), None)
    if idx is not None and idx > 0:
        prev_id = sorted_events[idx - 1][0]
        # 去重
        if not any(e[0] == prev_id and e[1] == task_id for e in edges["temporal"]):
            edges["temporal"].append((prev_id, task_id))
    if idx is not None and idx < len(sorted_events) - 1:
        next_id = sorted_events[idx + 1][0]
        if not any(e[0] == task_id and e[1] == next_id for e in edges["temporal"]):
            edges["temporal"].append((task_id, next_id))

    # 语义边: 增量更新 — 只算新节点 vs 现有节点
    for existing_id, existing_node in events.items():
        if existing_id == task_id:
            continue
        sim = _cosine_sim(tokens, existing_node.emb)
        if sim >= 0.6:
            # 无向边, 去重
            pair = sorted([task_id, existing_id])
            if not any((e[0] == pair[0] and e[1] == pair[1]) for e in edges["semantic"]):
                edges["semantic"].append((pair[0], pair[1], round(sim, 4)))

    _save_edges(edges)

    # ── 实体倒排索引 ──
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}
    for fp in changed_files:
        entity_idx.setdefault(fp, [])
        if task_id not in entity_idx[fp]:
            entity_idx[fp].append(task_id)
    _write_json(_ENTITY_IDX_PATH, entity_idx)


# ═══════════════════════════════════════════════════════════
# 快通道辅助: 补充事件属性 (task 完成后更新 status 等)
# ═══════════════════════════════════════════════════════════

def update_attrs(task_id: str, **kwargs) -> None:
    """更新事件节点的 attrs 字段。"""
    events = _load_events()
    if task_id in events:
        events[task_id].attrs.update(kwargs)
        _save_events(events)


# ═══════════════════════════════════════════════════════════
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
) -> dict:
    """MAGMA 完整查询流水线（金字塔渐进检索 — 受 Omni-SimpleMem 启发）。

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
# 维护
# ═══════════════════════════════════════════════════════════

def stats() -> dict:
    """各图统计。"""
    events = _load_events()
    edges = _load_edges()
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}

    explicit_causal = sum(1 for _, _, s in edges.get("causal", []) if s == "explicit")
    inferred_causal = sum(1 for _, _, s in edges.get("causal", []) if s != "explicit")

    return {
        "events": len(events),
        "edges_semantic": len(edges.get("semantic", [])),
        "edges_temporal": len(edges.get("temporal", [])),
        "edges_causal_explicit": explicit_causal,
        "edges_causal_inferred": inferred_causal,
        "edges_entity": len(edges.get("entity", [])),
        "entity_files": len(entity_idx),
        "latent_candidates": len(find_candidate_latent_edges()),
    }


def rebuild_from_traces() -> int:
    """从 traces/ 重建全部索引 (清空重跑快通道)。"""
    from . import tracker as tracker_mod

    _ensure_dir()
    for path in [_EVENTS_PATH, _EDGES_PATH, _ENTITY_IDX_PATH]:
        path.write_text("{}", encoding="utf-8")

    count = 0
    trace_dir = sched_config.TRACE_DIR
    if not trace_dir.exists():
        return 0

    for trace_path in sorted(trace_dir.glob("*.json")):
        try:
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        task_id = trace_path.stem
        description = trace_data.get("task", "")
        changed_files = trace_data.get("changed_files", [])

        task_data = tracker_mod._read(task_id)
        depends_on = task_data.depends_on if task_data else []
        created_at = task_data.created_at if task_data else None

        index_task(
            task_id=task_id, description=description,
            changed_files=changed_files, depends_on=depends_on,
            created_at=created_at,
        )
        count += 1

    return count


# ── 记忆生命周期 (hot → warm → cold) ──────────────────────

# ── 生命周期管理 (委托给 _lifecycle.py) ──
from ._lifecycle import (
    _HOT_WINDOW, _WARM_WINDOW, _COLD_MAX, _get_age_tier,
    lifecycle_stats as _lc_stats,
    prune_expired as _lc_prune,
    auto_maintain as _lc_maintain,
)


def lifecycle_stats() -> dict:
    """记忆生命周期统计。"""
    return _lc_stats(_load_events, _EVENTS_PATH)


def prune_expired() -> int:
    """清理过期事件。"""
    return _lc_prune(_load_events, _load_edges, _save_events, _save_edges,
                     _read_json, _write_json, _ENTITY_IDX_PATH)


def auto_maintain() -> dict:
    """自动维护: 清理 + 统计。"""
    return _lc_maintain(_load_events, _load_edges, _save_events, _save_edges,
                        _read_json, _write_json, _ENTITY_IDX_PATH, _EVENTS_PATH)


# ═══════════════════════════════════════════════════════════
# DCPM System 2 — 夜间异步模式提取 (DCPM 2026 论文)
# ═══════════════════════════════════════════════════════════
# System 1 (现有): embedding 粗筛 + 单对 LLM 精判
# System 2 (新增): 空闲时异步批处理，提取跨任务模式
# ponytail: 分组统计 + 简单启发式，不做 LLM 批处理

_INSIGHTS_PATH = _MEMORY_DIR / "insights.json"


def system2_extract() -> dict:
    """DCPM System 2: 空闲时异步提取跨任务模式。

    分组维度: 任务类型 × 状态 × 层级
    提取:
      1. 成功模式 — 哪些模型/策略在特定任务类型上成功率高
      2. 失败模式 — 哪些失败模式反复出现
      3. 有效策略 — 从成功任务中提取共性

    返回: {"insights": [...], "added": N}
    ponytail: 纯统计分析，不调 LLM。需要时加 LLM 模式提取。
    """
    events = _load_events()
    if len(events) < 10:
        return {"insights": [], "added": 0, "reason": "insufficient_data"}

    # 分组
    successes: dict[str, list] = defaultdict(list)  # (type×level) → events
    failures: dict[str, list] = defaultdict(list)

    for tid, ev in events.items():
        if not isinstance(ev, dict):
            continue
        status = ev.get("attrs", {}).get("status", "") if isinstance(ev.get("attrs"), dict) else ""
        task_type = ev.get("attrs", {}).get("route_type", "default") if isinstance(ev.get("attrs"), dict) else "default"
        level = ev.get("attrs", {}).get("route_level", "E") if isinstance(ev.get("attrs"), dict) else "E"
        key = f"{task_type}×{level}"
        if status in ("done", "pass", "merged"):
            successes[key].append(tid)
        elif status in ("failed", "blocked"):
            failures[key].append(tid)

    insights = []
    # 1. 成功率统计（按分组）
    for key in set(list(successes.keys()) + list(failures.keys())):
        s_count = len(successes.get(key, []))
        f_count = len(failures.get(key, []))
        total = s_count + f_count
        if total >= 3:
            rate = s_count / total
            if rate >= 0.8:
                insights.append({
                    "type": "high_success_pattern",
                    "group": key,
                    "success_rate": round(rate, 2),
                    "total": total,
                    "summary": f"{key} 任务成功率 {rate:.0%} ({s_count}/{total})",
                })
            elif rate <= 0.3 and total >= 3:
                insights.append({
                    "type": "failure_hotspot",
                    "group": key,
                    "success_rate": round(rate, 2),
                    "total": total,
                    "summary": f"⚠️ {key} 任务失败率 {1-rate:.0%} ({f_count}/{total})，建议升级路由",
                })

    # 2. 去重：只保留与已有 insights 不同的
    existing = _load_insights()
    existing_summaries = {i.get("summary", "") for i in existing}
    new_insights = [i for i in insights if i["summary"] not in existing_summaries]

    if new_insights:
        _save_insights(existing + new_insights)

    return {"insights": new_insights, "added": len(new_insights),
            "groups_analyzed": len(successes) + len(failures)}


def _load_insights() -> list[dict]:
    return list(_read_json(_INSIGHTS_PATH) or [])


def _save_insights(data: list[dict]) -> None:
    _write_json(_INSIGHTS_PATH, data)


def get_insights(limit: int = 10) -> list[dict]:
    """获取最近的 System 2 洞察。"""
    all_insights = _load_insights()
    return all_insights[-limit:]
