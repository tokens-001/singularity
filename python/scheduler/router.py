"""router.py — 二维判定 (复杂度 + 附加标记)

审计修了什么 (审计 Q2 / 2.1 / 2.2):
  - 复杂度判定与附加标记 (gate_required / task_type) 拆成两遍扫描,
    "命中即停"只管级别, 附加标记 |= 合并累加 (修 2.1 重构 core.py 优先级冲突)
  - 开头动词消歧: "查/解释/怎么/什么是" 开头且短 → 强制 E,
    覆盖后面"重构/设计"名词命中 (修 2.2 误判)
  - 优先级 0 的 gate_required 不再"级别不变"留下歧义, 级别交给后续优先级判
  - 否定消歧: 否定词+动作词 → 去动作词, 防 "不写代码"→写代码误判

v1 砍了什么:
  - task_type 不再驱动验证链分支 (审计 3.x: refactor 链依赖不存在的
    dependency_graph, feature 链的 diff_review 只留硬规则层且 v1 暂不启用)
  - task_type 仍然识别并记录, 供 neijinglu 标注, 但不分支验证链
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class RouteResult:
    level: str                       # "E" | "D" | "E+"
    gate_required: bool = False      # 命中引擎核心文件
    task_type: str = "default"       # bugfix | feature | refactor | docs | default
    cost_tier: str = "standard"      # "budget" | "standard" | "premium"
    matched_signals: list = field(default_factory=list)  # 命中证据, 供 trace

    def with_level(self, level: str) -> "RouteResult":
        self.level = level
        return self


# ── 第一维: 复杂度 (命中即停) ──────────────────────────────────────────
# 优先级从高到低; 0 优先级只置 gate_required, 级别留给 1-3 判 (审计 2.1)
_GATE_FILE_RE = re.compile(r"\b(core|tokenizer|graph|search)\.py\b", re.ASCII)
_LEVEL_PATTERNS = [
    (1, "E+", re.compile(r"重写|多文件|新模块|插件|从零开始|大规模重构|跨模块")),
    (2, "D",  re.compile(r"架构设计|系统设计|安全审计|架构方案|技术方案|架构审查|代码审查|系统审查|深度重构|系统架构")),
    (3, "E",  re.compile(r"新建|创建|搭建|建立|写代码|编写|实现|加个|添加|修改|修复|查|找|解释|怎么|报错|坏了|不对|异常|删掉")),
]

# ── 第二维: 任务类型 (独立扫描, v1 仅记录不分支) ──────────────────────
_TYPE_PATTERNS = [
    ("bugfix",   re.compile(r"修|报错|bug|坏了|不对|异常|崩溃")),
    ("feature",  re.compile(r"加|新增|实现|功能|模块")),
    ("refactor", re.compile(r"重构|重写|改架构|拆分|合并")),
    ("docs",     re.compile(r"文档|README|注释|changelog|配置|\.yaml|\.yml")),
]

# 开头动词消歧 (审计 2.2): 短查询里查询类动词在句首 → 查询意图, 强制 E
_LEADING_QUERY_RE = re.compile(
    r"^(查|查找|查一下|解释|怎么|什么是|能不能|行不行|可以吗|是否)"
    r"|(^(这|那)(个|么)?(能不能|行不行|可以吗|是否))"
)

# 真复杂关键词——命中就不降级，必须走 E+
_GENUINELY_COMPLEX_RE = re.compile(r"重写|多文件|新模块|插件|从零开始|大规模|跨模块")

# D-lite: 简单架构任务不走 D 层, 降级到 E (省 Opus $15/M→$0.5/M)
_SIMPLE_D_RE = re.compile(r"加个|加一个|增加|添加|删掉|去掉|修改配置|改个|调一下|小改|微调|单文件")

# 消歧: 否定词+动作词 → 去除动作词, 防误判 (如 "不写代码"→不是写代码意图)
_NEGATION_FILTER = re.compile(r"(?:不|别|不要|不必|不用)(?:新建|创建|搭建|建立|写代码|编写|重写|实现|重构|审查|修改)")


def route(task: str) -> RouteResult:
    result = RouteResult(level="E")  # 兜底 E

    # Step 0: 否定消歧 —— "不写代码"/"别重构" 不是动作意图
    task = _NEGATION_FILTER.sub("", task)

    # Step 0b: gate_required —— 独立置位, 不抢级别 (审计 2.1)
    if _GATE_FILE_RE.search(task):
        result.gate_required = True
        result.matched_signals.append("gate_required: 命中引擎核心文件名")

    # Step 0c: 开头动词消歧 —— 短查询强制 E, 覆盖后续名词 (审计 2.2)
    if len(task) < 20 and _LEADING_QUERY_RE.match(task):
        result.level = "E"
        result.task_type = "default"
        result.cost_tier = "budget"
        result.matched_signals.append("leading-query: 短查询开头动词 → E, type=default")
        return result

    # Step 1-3: 复杂度命中即停
    for prio, level, pat in _LEVEL_PATTERNS:
        if pat.search(task):
            result.level = level
            result.matched_signals.append(f"complexity@{prio}: {pat.pattern} → {level}")
            break

    # Step 1b: E+ 降级 —— 简单任务不配 E+，但真复杂(重写/多文件等)不降
    if result.level == "E+":
        genuinely_complex = _GENUINELY_COMPLEX_RE.search(task)
        single_file = bool(re.search(r"\.\w{1,6}\s*(文件|$)", task))
        very_short = len(task) <= 10
        if not genuinely_complex and (single_file or very_short):
            result.level = "E"
            result.matched_signals.append("降级 E+: 非复杂关键词 → E")

    # Step 1c: D-lite —— 简单架构任务降级到 E (省钱, Opus $15/M → DeepSeek $0.5/M)
    if result.level == "D":
        simple_d = _SIMPLE_D_RE.search(task)
        if simple_d:
            result.level = "E"
            result.cost_tier = "standard"
            result.matched_signals.append(f"D-lite: {simple_d.group()} → E (省$14.5/M)")
            result.matched_signals.append("降级 E+: 单文件/简单任务 → E")

    # 任务类型独立扫描 (v1 仅记录)
    _scan_task_type(task, result)

    if result.level == "E":
        if _LEADING_QUERY_RE.match(task):
            result.cost_tier = "budget"
        elif result.task_type == "bugfix":
            result.cost_tier = "standard"
        elif result.task_type == "feature":
            result.cost_tier = "premium"
    elif result.level in ("E+", "D"):
        result.cost_tier = "premium"

    return result


def _scan_task_type(task: str, result: RouteResult) -> None:
    for ttype, pat in _TYPE_PATTERNS:
        if pat.search(task):
            result.task_type = ttype
            result.matched_signals.append(f"task_type: {pat.pattern} → {ttype}")
            return
    result.task_type = "default"


def rank_models_for_task(task_desc: str, task_type: str = "",
                         exclude: list[str] = None,
                         phase: str = None) -> list[str]:
    """根据画像返回该任务类型的模型排名（最佳→最差），排除熔断模型。

    冷启动（画像不足 5 条记录）时返回空列表，调用方自己兜底。

    phase 参数：当提供项目阶段时，应用相位感知偏好（受 HyperAgents 启发）。
    不提供时行为不变（backward compatible）。
    """
    from .model_profile import ProfileStore
    from . import config
    from .task_templates import guess_template

    ttype = task_type or guess_template(task_desc)
    store = ProfileStore(config.QIDIAN_DIR / "model_profile.json")
    store.load()

    # 尝试模式画像（如果 phase 提供且 pattern 数据充足）
    if phase:
        template_id = guess_template(task_desc)
        pattern_ranked = store.rank_by_pattern(ttype, template_id, exclude_models=exclude)
        if pattern_ranked and any(r["from_pattern"] for r in pattern_ranked):
            return _apply_phase_boost(pattern_ranked, phase, ttype)

    # 回退：标准任务类型画像
    ranked = store.rank(ttype, exclude_models=exclude)
    total_records = sum(s.total_attempts for s in ranked)
    if total_records < 5:
        return []

    result = [s.model for s in ranked]
    # 有 phase 时也应用 boost（即使 pattern 数据不足）
    if phase:
        # 转换为 dict 格式以复用 _apply_phase_boost
        dict_ranked = [
            {"model": s.model, "success_rate": s.success_rate,
             "attempts": s.total_attempts, "avg_tokens": 0.0, "elo": s.elo}
            for s in ranked
        ]
        return _apply_phase_boost(dict_ranked, phase, ttype)

    return result


# ═══════════════════════════════════════════════════
# 相位感知偏好（受 HyperAgents 理论启发）
# ═══════════════════════════════════════════════════

_PHASE_PREFERENCE = {
    "template": "budget",        # 模板阶段：便宜模型快速试
    "researching": "budget",     # 调研阶段：便宜模型探索
    "gate1": "reasoning",        # 研究审查：需要强推理
    "planning": "reasoning",     # 架构规划：需要强推理
    "gate2": "reasoning",        # 架构审查：需要强推理
    "executing": "reliable",     # 执行阶段：可靠中模型，高 turns
    "gate3": "accurate",         # 执行审查：准确模型
    "reviewing": "accurate",     # 评审阶段：准确模型
    "fixing": "accurate",        # 修复阶段：准确模型，中 turns
    "gate4": "accurate",         # 最终审查
    "done": "default",           # 已完成
}


def _apply_phase_boost(ranked: list[dict], phase: str,
                       task_type: str = "default") -> list[str]:
    """根据项目阶段对模型排名应用偏好加成。

    Preference strategies:
    - budget: 低成本模型 ×1.2
    - reasoning: D 层推理模型 ×1.3
    - reliable: 高成功率 + 高 turns 模型 ×1.15
    - accurate: 高 Elo 模型 ×1.1
    - default: 不修改

    加成后按调整分重排，返回模型名列表。
    """
    strategy = _PHASE_PREFERENCE.get(phase, "default")

    # 尝试加载 model_registry 获取模型层级/成本信息
    try:
        from . import model_registry as mr
    except ImportError:
        mr = None

    for r in ranked:
        model = r["model"]
        base = r["success_rate"]

        if strategy == "budget":
            # 低成本模型加分
            cost_tier = "standard"
            if mr:
                try:
                    info = mr.get(model)
                    cost_tier = getattr(info, "cost", "standard")
                except Exception:
                    pass
            if cost_tier == "budget":
                r["_score"] = base * 1.2
            else:
                r["_score"] = base

        elif strategy == "reasoning":
            # D 层推理模型加分
            tier = ""
            if mr:
                try:
                    info = mr.get(model)
                    tier = getattr(info, "tier", "")
                except Exception:
                    pass
            if tier == "D":
                r["_score"] = base * 1.3
            else:
                r["_score"] = base

        elif strategy == "reliable":
            # 高成功率 + 高 Elo 加分
            elo = r.get("elo", 1500.0)
            if elo > 1550.0:
                r["_score"] = base * 1.15
            else:
                r["_score"] = base

        elif strategy == "accurate":
            # 高 Elo 模型加分
            elo = r.get("elo", 1500.0)
            if elo > 1550.0:
                r["_score"] = base * 1.1
            else:
                r["_score"] = base

        else:  # default
            r["_score"] = base

    # 按加成后分数重排
    ranked.sort(key=lambda r: r["_score"], reverse=True)
    return [r["model"] for r in ranked]


# ── 拓扑路由 (AdaptOrch 论文) ──────────────────────────────────

def select_topology() -> dict:
    """基于 DAG 结构指标选择执行拓扑。

    AdaptOrch 论文条件:
      τP (并行): ω>1, γ<0.3 — 宽图，多独立子任务并行
      τH (层级): δ大, k大 — 深链，层级分解
      τX (混合): γ中等 — 部分并行部分顺序
      τS (顺序): 默认 — 单链或小图

    返回: {"topology": "τP"|"τS"|"τH"|"τX", "omega": ω, "delta": δ, "gamma": γ, ...}
    """
    try:
        from . import tracker
        m = tracker.dag_metrics()
    except Exception:
        return {"topology": "τS", "omega": 1, "delta": 1, "gamma": 0.0,
                "node_count": 1, "reason": "metrics_unavailable"}

    omega = m["omega"]
    delta = m["delta"]
    gamma = m["gamma"]
    n = m["node_count"]

    # 单节点 → 顺序
    if n <= 1:
        return {**m, "topology": "τS", "reason": "single_node"}

    # AdaptOrch 决策表
    if omega >= 3 and gamma < 0.3:
        topo = "τP"
        reason = f"ω={omega}≥3 且 γ={gamma:.2f}<0.3 → 并行拓扑"
    elif delta >= 5 and gamma > 0.5:
        topo = "τH"
        reason = f"δ={delta}≥5 且 γ={gamma:.2f}>0.5 → 层级分解"
    elif gamma > 0.5:
        topo = "τX"
        reason = f"γ={gamma:.2f}>0.5 → 混合拓扑"
    else:
        topo = "τS"
        reason = f"ω={omega} δ={delta} γ={gamma:.2f} → 默认顺序"

    return {**m, "topology": topo, "reason": reason}
