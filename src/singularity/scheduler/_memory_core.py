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
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events

__all__ = ['EdgeType', 'EventNode', '_EDGES_PATH', '_EMBED_MODEL', '_ENTITY_IDX_PATH', '_EVENTS_PATH', '_INTENT_EDGE_WEIGHTS', '_INTENT_PATTERNS', '_MAX_EVENTS', '_MEMORY_DIR', '_calculate_importance', '_cosine_sim', '_embed', '_ensure_dir', '_evict_if_needed', '_get_embed_model', '_hf_log', '_infer_mem_type', '_load_edges', '_load_events', '_read_json', '_save_edges', '_save_events', '_write_json', 'detect_intent', 'index_task', 'update_attrs']
# ═══════════════════════════════════════════════════════════
# 存储路径 + I/O 原语 (ex _memory_io.py)
# ═══════════════════════════════════════════════════════════

_MEMORY_DIR = sched_config.QIDIAN_DIR / "memory"
_EVENTS_PATH = _MEMORY_DIR / "events.json"
_EDGES_PATH = _MEMORY_DIR / "edges.json"
_ENTITY_IDX_PATH = _MEMORY_DIR / "entity_index.json"


def _ensure_dir() -> None:
    _MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict | list:
    if not path.exists():
        return {} if ".json" in str(path) else []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {} if ".json" in str(path) else []


def _write_json(path: Path, data: dict | list) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


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
    """懒加载: 首次查询才下载/加载模型(420MB)。

    默认启用。下载超时或 CI 环境 (QIDIAN_SKIP_EMBED=1) 时降级跳过。
    """
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        import os, time
        if os.environ.get("QIDIAN_SKIP_EMBED", "") == "1":
            _EMBED_MODEL = False
            return None
        import sys, io, logging as _log
        _log.getLogger("sentence_transformers").setLevel(_log.ERROR)
        _log.getLogger("transformers").setLevel(_log.ERROR)
        _stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            _EMBED_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        except Exception:
            # 下载失败/网络问题 → 降级跳过, 不阻塞
            _EMBED_MODEL = False
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


def _infer_mem_type(description: str) -> str:
    """从任务描述推断记忆类型。ponytail: 关键词匹配，够用。"""
    desc = description.lower()
    if any(w in desc for w in ("架构", "设计", "系统", "方案", "重构")):
        return "architecture"
    if any(w in desc for w in ("修", "bug", "fix", "报错", "异常", "崩溃")):
        return "bug_fix"
    if any(w in desc for w in ("决定", "选择", "方案", "决策")):
        return "decision"
    if any(w in desc for w in ("加", "新增", "实现", "功能", "模块", "feature")):
        return "code_change"
    if any(w in desc for w in ("文档", "readme", "注释", "doc")):
        return "docs"
    return "code_change"


def index_task(
    task_id: str,
    description: str,
    changed_files: list[str] | None = None,
    depends_on: list[str] | None = None,
    created_at: float | None = None,
    mem_type: str = "",
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

    # ── 记忆类型: 显式传入或自动推断 ──
    if not mem_type:
        mem_type = _infer_mem_type(description)

    # ── EventNode ──
    tokens = _embed(description)
    node = EventNode(
        task_id=task_id,
        content=description,
        timestamp=created_at,
        emb=tokens,
        attrs={"files": changed_files, "depends_on": depends_on, "mem_type": mem_type},
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

    # 语义边: 增量更新 — 只算新节点 vs 最近 N 个 (防 O(n) 退化)
    recent_for_sem = sorted(events.items(), key=lambda x: -x[1].timestamp)[:200]
    for existing_id, existing_node in recent_for_sem:
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

    # ── T12: LRU 驱逐检查 ──
    evicted = _evict_if_needed(events, edges)
    if evicted > 0:
        _save_events(events)
        _save_edges(edges)


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
# T12: LRU 驱逐 + 重要性评分
# ═══════════════════════════════════════════════════════════

_MAX_EVENTS = 500  # ponytail: 内存上限，超此数触发 LRU 驱逐


def _calculate_importance(
    task_id: str,
    node: EventNode,
    events: dict[str, EventNode],
    edges: dict,
    now: float | None = None,
) -> float:
    """加权评分：引用数 + 成功奖励 + 新鲜度衰减。

    score = α * ref_count + β * success_bonus + γ * recency
    范围 [0, 1]，越高越值得保留。
    """
    if now is None:
        now = time.time()

    # ── 引用数 (被其他节点依赖/关联) ──
    ref_count = 0
    for edge_type in ("causal", "temporal", "semantic"):
        for e in edges.get(edge_type, []):
            if len(e) >= 2 and e[1] == task_id:
                ref_count += 1
    ref_score = min(ref_count / 10.0, 1.0)  # 10 引用满分

    # ── 成功奖励 ──
    attrs = node.attrs or {}
    success = 1.0 if attrs.get("status") in ("done", "passed") else 0.0

    # ── 新鲜度 (天级衰减) ──
    days_ago = (now - node.timestamp) / 86400.0
    recency = 1.0 / (1.0 + days_ago)

    # 权重: α=0.4, β=0.35, γ=0.25
    return 0.4 * ref_score + 0.35 * success + 0.25 * recency


def _evict_if_needed(
    events: dict[str, EventNode],
    edges: dict,
    max_events: int = _MAX_EVENTS,
) -> int:
    """超出上限时驱逐低分节点。返回驱逐数量。"""
    if len(events) <= max_events:
        return 0

    now = time.time()
    # 计算所有节点的重要性
    scored = [
        (tid, _calculate_importance(tid, node, events, edges, now))
        for tid, node in events.items()
    ]
    # 按分数升序，低分在前
    scored.sort(key=lambda x: x[1])
    to_evict = len(events) - max_events
    evicted_ids = {tid for tid, _ in scored[:to_evict]}

    # 驱逐节点
    for tid in evicted_ids:
        del events[tid]

    # 清理涉及的边
    for etype in list(edges.keys()):
        edges[etype] = [
            e for e in edges[etype]
            if (len(e) >= 2 and e[0] not in evicted_ids and e[1] not in evicted_ids)
            or (len(e) == 1 and e[0] not in evicted_ids)
        ]

    return len(evicted_ids)


# ═══════════════════════════════════════════════════════════
