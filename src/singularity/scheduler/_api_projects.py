"""_api.py — API handler 层 (所有路由处理函数)。

Section 分组:
  - 辅助函数
  - 任务 CRUD
  - 项目 CRUD + workflow
  - Agent / Model / API Store
  - Skill / Permission
  - MCP 服务器
  - 监控 / Auth / Health / 模板
  - Memory / Conflict
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time

from pathlib import Path
from typing import Optional

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler import witness
from singularity.scheduler import orchestrator


# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 项目 CRUD + Workflow  (ex _api_projects.py)
# ═══════════════════════════════════════════════════════════════

def project_list() -> tuple[dict, int]:
    """GET /api/projects"""
    from . import project as proj_mod
    projects = proj_mod.list_all()
    return {"projects": [p.to_dict() if hasattr(p, 'to_dict') else p for p in projects]}, 200


def project_create(name: str, template: str = "product_dev",
                   description: str = "", scope: str = "",
                   constraints: list = None, budget: float = 5.0) -> tuple[dict, int]:
    """POST /api/projects"""
    from . import project as proj_mod
    try:
        p = proj_mod.create(name=name, template=template, description=description,
                            scope=scope, constraints=constraints or [], budget=budget)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"ok": True, "project": {"id": p.id, "name": p.name}}, 200


def project_detail(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    d = proj.to_dict() if hasattr(proj, 'to_dict') else {"ok": True}
    d["repo_dir"] = str(proj_mod.repo_dir(project_id))  # 成品保存路径
    return d, 200


def projects_root_get() -> tuple[dict, int]:
    """GET /api/projects-root"""
    from . import project as proj_mod
    return {"root": str(proj_mod.get_projects_root())}, 200


def projects_root_set(path: str) -> tuple[dict, int]:
    """PUT /api/projects-root"""
    from . import project as proj_mod
    try:
        root = proj_mod.set_projects_root(path)
    except Exception as e:
        return {"error": str(e)}, 400
    return {"ok": True, "root": str(root)}, 200


def fs_list(path: str = "") -> tuple[dict, int]:
    """GET /api/fs/ls —— 列出目录的子目录（目录选择器用）。"""
    from pathlib import Path
    base = Path(path or str(Path.home())).expanduser()
    if not base.is_dir():
        return {"error": f"目录不存在: {base}"}, 404
    try:
        dirs = [e.name for e in sorted(base.iterdir()) if e.is_dir() and not e.name.startswith(".")]
    except PermissionError:
        return {"error": "无权限访问"}, 403
    return {"path": str(base), "parent": str(base.parent), "dirs": dirs}, 200


def fs_mkdir(path: str, name: str) -> tuple[dict, int]:
    """POST /api/fs/mkdir —— 在 path 下新建目录 name。"""
    from pathlib import Path
    base = Path(path).expanduser()
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name:
        return {"error": "非法目录名"}, 400
    target = base / name
    if target.exists():
        return {"error": "目录已存在"}, 400
    target.mkdir(parents=True)
    return {"ok": True, "path": str(target)}, 200


def fs_pick() -> tuple[dict, int]:
    """POST /api/fs/pick —— 用 macOS Finder 原生对话框选择文件夹。"""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return {"error": "仅支持 macOS"}, 400
    script = 'POSIX path of (choose folder with prompt "选择项目根目录")'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "选择超时"}, 500
    if r.returncode != 0:
        return {"error": "已取消"}, 400
    return {"path": r.stdout.strip()}, 200


def project_gate_confirm(project_id: str, gate: str = "", decision: str = "",
                          feedback: str = "") -> tuple[dict, int]:
    """POST /api/projects/<id>/gate-confirm"""
    from . import project as proj_mod
    from .project import Phase
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404

    gate_phase = Phase(gate) if gate else proj.phase
    if decision == "approved":
        next_p = proj.confirm_gate(gate_phase, "approved")
        proj_mod.save(proj)
        return {"ok": True, "gate": gate, "decision": "approved",
                "next_phase": next_p.value if next_p else "done"}, 200
    elif decision == "rejected":
        proj.confirm_gate(gate_phase, "rejected")
        proj_mod.save(proj)
        # GATE3 打回: D出修复方案
        if gate_phase == Phase.GATE3:
            from . import workflow as wf_mod
            from . import dispatcher as disp_mod
            agents = disp_mod.load_agents()
            result = wf_mod.handle_gate3_reject(proj, agents, feedback)
            return {"ok": True, "gate": "gate3", "decision": "rejected",
                    "result": result, "next_phase": proj.phase.value}, 200
        return {"ok": True, "gate": gate, "decision": "rejected",
                "next_phase": proj.phase.value}, 200
    return {"ok": True, "gate": gate, "decision": decision or "pending"}, 200


def project_run_phase(project_id: str, phase_name: str = "",
                      task_desc: str = "", agent_override: str = "",
                      push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/run-phase"""
    from . import project as proj_mod
    from . import workflow as wf_mod
    from . import dispatcher as disp_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    if not hasattr(proj, 'phase') or proj.phase is None:
        return {"error": "项目未设定阶段"}, 400
    phase = phase_name or proj.phase.value
    agents = disp_mod.load_agents()
    result = wf_mod.run_phase(proj, agents)
    if push_event:
        push_event("system", f"[{project_id[:8]}] {phase} 阶段已启动")
    return {"ok": True, "phase": phase, "result": str(result)}, 200


def project_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/start"""
    from . import project as proj_mod
    from . import workflow as wf_mod
    from . import dispatcher as disp_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    agents = disp_mod.load_agents()
    result = wf_mod.start_project_workflow(proj, agents)
    if push_event:
        push_event("system", f"[{project_id[:8]}] workflow 已启动")
    return {"ok": True, "workflow": result}, 200


def project_cost(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/cost"""
    from . import project as proj_mod
    from .project import Phase
    from .workflow import _needs_research
    p = proj_mod.load(project_id)
    if p is None:
        return {"error": "项目不存在"}, 404
    cost_rates = {Phase.RESEARCHING: 0.02, Phase.PLANNING: 2.50, Phase.REVIEWING: 1.00}
    phase_levels = {Phase.RESEARCHING: "any", Phase.PLANNING: "any", Phase.REVIEWING: "any"}
    phase = p.phase
    cost = 0
    level = "-"
    if phase == Phase.TEMPLATE:
        if _needs_research(p):
            cost = cost_rates.get(Phase.RESEARCHING, 0)
            level = phase_levels.get(Phase.RESEARCHING, "-")
    elif phase in cost_rates:
        cost = cost_rates[phase]
        level = phase_levels[phase]
    return {"cost": round(cost, 2), "phase": phase.value, "level": level,
            "token_budget_total": p.token_budget_total or 0,
            "token_spent": p.token_spent}, 200


def project_lineage(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/lineage"""
    from .task_templates import get as _get_template
    tpl = _get_template(project_id)
    if tpl:
        return {"lineage": tpl}, 200
    from ._api_tasks import _list_all_tasks
    all_tasks = _list_all_tasks()
    proj_tasks = [t for t in all_tasks if t.get("project_id") == project_id]
    return {"tasks": proj_tasks, "total": len(proj_tasks)}, 200


def project_snapshot(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/snapshot — 快照项目 repo (修复 #1 遗漏: 原先快照的是奇点仓库)。"""
    from . import snapshot as snap_mod
    from . import project as proj_mod
    snap = snap_mod.take(project_id, repo_root=proj_mod.repo_dir(project_id))
    return {"ok": True, "snapshot_id": snap.id, "ref": snap.ref}, 200


def project_auto(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/auto — 自动运行下一个阶段。"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    next_phase = proj_mod.advance_phase(project_id)
    if next_phase is None:
        return {"error": "无下一阶段"}, 400
    return {"ok": True, "phase": next_phase.value if hasattr(next_phase, 'value') else str(next_phase)}, 200


def project_lineup_get(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/lineup"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    lineup = getattr(proj, 'agent_lineup', {}) or {}
    return {"lineup": lineup}, 200


def project_lineup_set(project_id: str, lineup: dict) -> tuple[dict, int]:
    """PUT /api/projects/<id>/lineup"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    proj.agent_lineup = lineup
    proj_mod.save(proj)
    return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# Agent / Model / API Store  (ex _api_agents.py)
# ═══════════════════════════════════════════════════════════════

# ── token 估算常量 ──
_TOKEN_PER_CHAR = 0.6

