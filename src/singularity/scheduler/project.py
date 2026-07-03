"""project.py — 项目状态机 + 黑板持久化。

ProjectState 是整个工作流的单一真相源。存盘到 .qidian/projects/{id}.json。
重启恢复: load → 读 phase → 从中断点继续。
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from singularity.scheduler import config


class Phase(str, Enum):
    TEMPLATE = "template"
    RESEARCHING = "researching"
    GATE1 = "gate1"          # 用户审调研报告
    PLANNING = "planning"
    GATE2 = "gate2"          # 用户审架构+任务分配
    EXECUTING = "executing"
    INTEGRATING = "integrating"  # D2: 多路worktree合并+集成测试(非用户门)
    REVIEWING = "reviewing"      # 内部: 强力层审查(非用户门)
    FIXING = "fixing"            # 内部: 修复任务(非用户门)
    GATE3 = "gate3"              # 用户最终交付审核
    DELIVERING = "delivering"    # S1: 打包归档 (GATE3通过后)
    DONE = "done"


# Gate 拒绝 → 回退到哪里
_REJECT_FALLBACK: dict[Phase, Phase] = {
    Phase.GATE1: Phase.TEMPLATE,
    Phase.GATE2: Phase.RESEARCHING,        # 回调研或重写需求
    # GATE3 不在此表: 由 workflow.handle_gate3_reject 按 fix_route 分级路由
    # (impl→EXECUTING / design→PLANNING / note→不回退), 不再一刀切回 PLANNING
}

# 架构级返工: 可从这些阶段直接回 planning
_ARCHITECTURE_REDO = {Phase.EXECUTING, Phase.INTEGRATING, Phase.GATE3, Phase.REVIEWING, Phase.FIXING}

# Gate 确认→下一个 phase
_GATE_NEXT: dict[Phase, Phase] = {
    Phase.GATE1: Phase.PLANNING,
    Phase.GATE2: Phase.EXECUTING,
    Phase.GATE3: Phase.DELIVERING,           # S1: 最终批准→交付打包
}

# D2: 集成合并失败上限 (自动修N轮后升GATE2)
_INTEGRATE_MAX_RETRIES = 2


@dataclass
class ProjectState:
    id: str
    name: str
    template: str = "product_dev"   # 选题模板
    phase: Phase = Phase.TEMPLATE
    auto_mode: bool = False     # 自动流转: 跳过所有 Owner Gate

    # Owner 填写的需求
    description: str = ""
    scope: str = ""
    raw_constraints: list[str] = field(default_factory=list)

    # Gate 确认状态: {gate1: "approved"|"rejected"|"pending", ...}
    owner_confirm: dict = field(default_factory=dict)

    # Artifact 区 (各阶段的产出)
    research_report: dict | None = None          # Researcher 产出
    architecture: dict | None = None             # Architect 产出 {plan, tasks, constraints}
    constraints_checklist: list[str] = field(default_factory=list)  # Gate2 确认后的可检查约束
    task_ids: list[str] = field(default_factory=list)               # 关联 tracker tasks
    issues: list[dict] = field(default_factory=list)                # Reviewer 问题清单
    supervision_log: list[dict] = field(default_factory=list)       # Supervisor 校验记录
    lineage: list[dict] = field(default_factory=list)               # 血缘日志
    handoffs: list[dict] = field(default_factory=list)              # Agent 交接记录
    token_budget_total: float = 5.0        # $ (默认 $5)
    token_spent: float = 0.0               # $ 累计
    fix_round: int = 0                      # 内循环修复轮次(上限3)
    review_failures: int = 0                # D1: 审查自动修失败计数 (上限 _REVIEW_MAX_AUTO_FIX)
    integrate_failures: int = 0             # D2: 集成合并失败计数 (上限 _INTEGRATE_MAX_RETRIES)

    # Agent 编组: {"any": ["model_a","model_b"]} — 两档后统一全池, 不设则用全局配置
    agent_lineup: dict[str, list[str]] = field(default_factory=dict)

    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "name": self.name, "template": self.template,
            "phase": self.phase.value, "description": self.description,
            "scope": self.scope, "raw_constraints": self.raw_constraints,
            "owner_confirm": self.owner_confirm,
            "research_report": self.research_report,
            "architecture": self.architecture,
            "constraints_checklist": self.constraints_checklist,
            "task_ids": self.task_ids, "issues": self.issues,
            "supervision_log": self.supervision_log, "lineage": self.lineage,
            "handoffs": self.handoffs,
            "auto_mode": self.auto_mode,
            "token_budget_total": self.token_budget_total,
            "token_spent": self.token_spent,
            "fix_round": self.fix_round,
            "review_failures": self.review_failures,
            "integrate_failures": self.integrate_failures,
            "agent_lineup": self.agent_lineup,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectState":
        d = dict(d)
        d["phase"] = Phase(d.get("phase", "template"))
        d.setdefault("description", "")
        d.setdefault("scope", "")
        d.setdefault("raw_constraints", [])
        d.setdefault("owner_confirm", {})
        d.setdefault("research_report", None)
        d.setdefault("architecture", None)
        d.setdefault("constraints_checklist", [])
        d.setdefault("task_ids", [])
        d.setdefault("issues", [])
        d.setdefault("supervision_log", [])
        d.setdefault("lineage", [])
        d.setdefault("handoffs", [])
        d.setdefault("auto_mode", False)
        d.setdefault("token_budget_total", 5.0)
        d.setdefault("token_spent", 0.0)
        d.setdefault("fix_round", 0)
        d.setdefault("review_failures", 0)
        d.setdefault("integrate_failures", 0)
        d.setdefault("agent_lineup", {})
        d.setdefault("created_at", 0.0)
        d.setdefault("updated_at", 0.0)
        return cls(**d)

    # ── Phase 流转 ──

    def advance_to(self, next_phase: Phase) -> bool:
        """推进到下一个 phase (Gate 确认后)。"""
        self.phase = next_phase
        self.updated_at = time.time()
        return True

    def confirm_gate(self, gate: Phase, decision: str) -> Optional[Phase]:
        """Owner 批 Gate。自动推进 phase。返回下一个 phase 或 None。"""
        self.owner_confirm[gate.value] = decision
        self.updated_at = time.time()
        if decision == "approved":
            next_p = _GATE_NEXT.get(gate)
            if next_p:
                self.phase = next_p
            return next_p
        elif decision == "rejected":
            fallback = _REJECT_FALLBACK.get(gate)
            if fallback:
                self.phase = fallback
            return fallback
        return None

    def architecture_redo(self) -> bool:
        """架构级返工: 从 executing/reviewing 回 planning。"""
        if self.phase in _ARCHITECTURE_REDO:
            self.phase = Phase.PLANNING
            self.architecture = None
            self.constraints_checklist = []
            self.updated_at = time.time()
            return True
        return False

    def is_at_gate(self) -> bool:
        return self.phase.value.startswith("gate")

    def add_lineage(self, entry: dict):
        """追加血缘条目。"""
        entry["ts"] = time.time()
        self.lineage.append(entry)
        # 硬上限 1000 条
        if len(self.lineage) > 1000:
            self.lineage = self.lineage[-1000:]

    def spend_tokens(self, model: str, raw_tokens: int) -> float:
        """累计成本 (按 $ 算)。粗略: Opus=$15/M, DeepSeek=$0.5/M, GLM=$1/M."""
        rates = {"claude-opus": 15.0, "deepseek": 0.5, "glm": 1.0}
        rate = 1.0
        for k, v in rates.items():
            if k in model.lower():
                rate = v
                break
        cost = (raw_tokens / 1_000_000) * rate
        self.token_spent += cost
        return cost

    def over_budget(self) -> bool:
        return self.token_spent >= self.token_budget_total


# ═══════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════

def _projects_dir() -> Path:
    d = config.QIDIAN_DIR / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_project_dir(project_id: str) -> Path:
    """获取项目工作目录 (存定义文档等)。"""
    d = _projects_dir() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(project_id: str) -> Path:
    return _projects_dir() / f"{project_id}.json"


def _next_id() -> str:
    base = int(time.time() * 1000)
    max_existing = base
    for p in _projects_dir().glob("*.json"):
        try:
            max_existing = max(max_existing, int(p.stem))
        except ValueError:
            continue
    return str(max(max_existing, base) + 1)


def delete(project_id: str) -> bool:
    """删除项目及所有关联文件。"""
    p = _path(project_id)
    deleted = False
    if p.exists():
        p.unlink()
        deleted = True
    # 删除关联产出文件
    for f in _projects_dir().glob(f"{project_id}.*"):
        try: f.unlink(); deleted = True
        except Exception: pass
    return deleted


def save(project: ProjectState) -> None:
    project.updated_at = time.time()
    p = _path(project.id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)  # 原子写
    # SSE 推送项目进度
    _push_project_event(project)


def _push_project_event(proj: "ProjectState") -> None:
    """推送项目状态变更到 SSE (两个通道: pending queue + 直接广播)。"""
    try:
        import json as _json
        phase = proj.phase.value if hasattr(proj.phase, 'value') else str(proj.phase)
        task_count = len(proj.task_ids) if hasattr(proj, 'task_ids') else 0
        payload = _json.dumps({
            "project_id": proj.id, "name": proj.name,
            "phase": phase, "task_count": task_count,
        })
        # Channel 1: pending queue (loop flush)
        from singularity.scheduler._types import _pending_sse_events
        _pending_sse_events.append({"kind": "project", "msg": payload, "ts": time.time()})
        # Channel 2: direct broadcast
        from singularity.web.app import _sse_broadcast
        _sse_broadcast("project", payload)
    except Exception:
        pass


def load(project_id: str) -> Optional[ProjectState]:
    p = _path(project_id)
    if not p.exists():
        return None
    try:
        return ProjectState.from_dict(
            json.loads(p.read_text(encoding="utf-8"))
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def create(
    name: str, template: str = "product_dev",
    description: str = "", scope: str = "",
    constraints: list[str] = None,
    budget: float = 5.0,
    auto_mode: bool = False,
) -> ProjectState:
    now = time.time()
    proj = ProjectState(
        id=_next_id(), name=name, template=template,
        description=description, scope=scope,
        raw_constraints=list(constraints or []),
        token_budget_total=budget,
        auto_mode=auto_mode,
        phase=Phase.TEMPLATE,
        created_at=now, updated_at=now,
    )
    save(proj)
    return proj


def list_all() -> list[ProjectState]:
    projects = []
    # ponytail: 跳过阶段产出文件 (traceability.json 等)
    _OUTPUT_SUFFIXES = {".traceability.json", ".research.md", ".architecture.md", ".test-plan.md"}
    for p in sorted(_projects_dir().glob("*.json"), reverse=True):
        if any(str(p).endswith(s) for s in _OUTPUT_SUFFIXES):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "phase" not in data:
                continue  # 非项目文件
            projects.append(ProjectState.from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return projects


def recover_all() -> list[ProjectState]:
    """启动时恢复所有非终态项目。"""
    return [p for p in list_all() if p.phase != Phase.DONE]


# ═══════════════════════════════════════════════════════════
# 选题模板
# ═══════════════════════════════════════════════════════════

TEMPLATES = {
    "product_dev": {
        "name": "产品开发",
        "fields": ["项目名称", "项目目标", "功能范围", "涉及模块", "技术约束", "验收标准"],
        "research_domains": ["架构参考", "替代方案", "类似项目"],
    },
    "bug_fix": {
        "name": "Bug修复",
        "fields": ["问题描述", "复现步骤", "影响范围", "期望行为"],
        "research_domains": ["同类问题解法", "根因分析"],
    },
    "refactor": {
        "name": "重构优化",
        "fields": ["重构目标", "现有问题", "不改的接口", "预期收益"],
        "research_domains": ["设计模式参考", "业界实践"],
    },
    "agent_dev": {
        "name": "Agent开发",
        "fields": ["Agent名称", "能力需求", "目标模型", "工具需求", "性能要求", "验收标准"],
        "research_domains": ["Agent框架参考", "工具调用优化", "同类Agent实现"],
    },
}
