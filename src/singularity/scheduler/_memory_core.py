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

__all__ = ['EdgeType', 'EventNode', '_EDGES_PATH', '_EMBED_MODEL', '_ENTITY_IDX_PATH', '_EVENTS_PATH', '_INTENT_EDGE_WEIGHTS', '_INTENT_PATTERNS', '_MEMORY_DIR', '_cosine_sim', '_embed', '_ensure_dir', '_get_embed_model', '_read_json', '_write_json', 'detect_intent']
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
