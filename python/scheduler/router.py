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
    (1, "E+", re.compile(r"重写|多文件|新模块|从零开始|大规模重构|跨模块")),
    (2, "D",  re.compile(r"审|设计|架构|方案|重构|review|审查")),
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

    # Step 1b: E+ 降级 —— 单文件或简单任务不配 E+ (省钱)
    if result.level == "E+":
        single_file = bool(re.search(r"\.\w{1,6}\s*(文件|$)", task))
        very_short = len(task) <= 15
        if single_file or very_short:
            result.level = "E"
            result.matched_signals.append("降级 E+: 单文件/简单任务 → E")

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
