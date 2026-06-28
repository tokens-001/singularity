"""router.py — 任务类型识别 (仅标注, 不分配层级)

两档后弃用 E/E+/D 分级。router 只做 task_type 检测供 trace 标注。
模型选择由用户指定或 dispatcher 从全池推荐。
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from singularity.scheduler.log import timed


@dataclass
class RouteResult:
    """路由结果: 仅 task_type + gate_required, 不分配层级。"""
    task_type: str = "default"       # bugfix | feature | refactor | docs | default
    gate_required: bool = False      # 命中引擎核心文件
    matched_signals: list = field(default_factory=list)


# ── GATE 触发文件 ──
_GATE_FILE_RE = re.compile(r"\b(core|tokenizer|graph|search)\.py\b", re.ASCII)

# ── 任务类型检测 ──
_TYPE_PATTERNS = [
    ("bugfix",   re.compile(r"修|报错|bug|坏了|不对|异常|崩溃")),
    ("feature",  re.compile(r"加|新增|实现|功能|模块")),
    ("refactor", re.compile(r"重构|重写|改架构|拆分|合并")),
    ("docs",     re.compile(r"文档|README|注释|changelog|配置|\.yaml|\.yml")),
    ("fusion",   re.compile(r"架构设计|系统设计|安全审计|架构方案|技术方案|多模块|跨模块|从零开始")),
]

# 消歧: 否定词+动作词
_NEGATION_FILTER = re.compile(
    r"(?:不|别|不要|不必|不用)(?:新建|创建|搭建|建立|写代码|编写|重写|实现|重构|审查|修改)"
)


@timed(name="router")
def route(task: str) -> RouteResult:
    """检测 task_type + gate_required。不分配层级。"""
    task = _NEGATION_FILTER.sub("", task)
    result = RouteResult()

    if _GATE_FILE_RE.search(task):
        result.gate_required = True
        result.matched_signals.append("gate_required: 引擎核心文件")

    for ttype, pat in _TYPE_PATTERNS:
        if pat.search(task):
            result.task_type = ttype
            result.matched_signals.append(f"task_type: {ttype}")
            return result

    result.task_type = "default"
    return result


# ═══════════════════════════════════════════════════
# 拓扑路由 (AdaptOrch 论文) — 保留, 不涉及层级
# ═══════════════════════════════════════════════════

def select_topology() -> dict:
    """基于 DAG 结构指标选择执行拓扑。"""
    try:
        from . import tracker
        m = tracker.dag_metrics()
    except Exception:
        return {"topology": "τS", "omega": 1, "delta": 1, "gamma": 0.0,
                "node_count": 1, "reason": "metrics_unavailable"}

    omega = m["omega"]; delta = m["delta"]; gamma = m["gamma"]; n = m["node_count"]

    if n <= 1:
        return {**m, "topology": "τS", "reason": "single_node"}
    if omega >= 3 and gamma < 0.3:
        return {**m, "topology": "τP", "reason": f"ω={omega}≥3 且 γ={gamma:.2f}<0.3 → 并行"}
    if delta >= 5 and gamma > 0.5:
        return {**m, "topology": "τH", "reason": f"δ={delta}≥5 且 γ={gamma:.2f}>0.5 → 层级"}
    if gamma > 0.5:
        return {**m, "topology": "τX", "reason": f"γ={gamma:.2f}>0.5 → 混合"}
    return {**m, "topology": "τS", "reason": f"ω={omega} δ={delta} γ={gamma:.2f} → 默认顺序"}
