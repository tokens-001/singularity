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

from . import config


class Phase(str, Enum):
    TEMPLATE = "template"
    RESEARCHING = "researching"
    GATE1 = "gate1"
    PLANNING = "planning"
    GATE2 = "gate2"
    EXECUTING = "executing"
    GATE3 = "gate3"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    GATE4 = "gate4"
    DONE = "done"


# Gate 拒绝 → 回退到哪里
_REJECT_FALLBACK: dict[Phase, Phase] = {
    Phase.GATE1: Phase.TEMPLATE,
    Phase.GATE2: Phase.RESEARCHING,        # 回调研或重写需求
    Phase.GATE3: Phase.EXECUTING,          # 回执行或重架构
    Phase.GATE4: Phase.REVIEWING,
}

# 架构级返工: 从执行/审查直接回 planning
_ARCHITECTURE_REDO = {Phase.EXECUTING, Phase.GATE3, Phase.REVIEWING}

# Gate 确认→下一个 phase
_GATE_NEXT: dict[Phase, Phase] = {
    Phase.GATE1: Phase.PLANNING,
    Phase.GATE2: Phase.EXECUTING,
    Phase.GATE3: Phase.REVIEWING,
    Phase.GATE4: Phase.DONE,
}


@dataclass
class ProjectState:
    id: str
    name: str
    template: str = "product_dev"   # 选题模板
    phase: Phase = Phase.TEMPLATE

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

    # 成本
    token_budget_total: float = 5.0        # $ (默认 $5)
    token_spent: float = 0.0               # $ 累计

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
            "token_budget_total": self.token_budget_total,
            "token_spent": self.token_spent,
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
        d.setdefault("token_budget_total", 5.0)
        d.setdefault("token_spent", 0.0)
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


def save(project: ProjectState) -> None:
    project.updated_at = time.time()
    p = _path(project.id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)  # 原子写


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
) -> ProjectState:
    now = time.time()
    proj = ProjectState(
        id=_next_id(), name=name, template=template,
        description=description, scope=scope,
        raw_constraints=list(constraints or []),
        token_budget_total=budget,
        phase=Phase.TEMPLATE,
        created_at=now, updated_at=now,
    )
    save(proj)
    return proj


def list_all() -> list[ProjectState]:
    projects = []
    for p in sorted(_projects_dir().glob("*.json"), reverse=True):
        try:
            projects.append(ProjectState.from_dict(
                json.loads(p.read_text(encoding="utf-8"))
            ))
        except (json.JSONDecodeError, KeyError, TypeError):
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
}
