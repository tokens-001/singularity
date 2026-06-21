"""_api_projects.py — 项目 API handlers (从 _api.py 提取)。"""
from __future__ import annotations
import json
import time

from . import config
from . import tracker
from . import witness
from . import project as proj_mod
from . import workflow as wf_mod
from . import dispatcher as disp_mod
from . import snapshot as snap_mod
from . import conductor as _conductor
from .project import Phase
from .task_templates import get as _get_template
from .workflow import _needs_research
from ._api import _list_all_tasks, _read_task_file


def project_list() -> tuple[dict, int]:
    """GET /api/projects"""
    projects = proj_mod.list_all()
    return {"projects": [p.to_dict() if hasattr(p, 'to_dict') else p for p in projects]}, 200


def project_create(name: str, template: str = "product_dev",
                   description: str = "", scope: str = "",
                   constraints: list = None, budget: float = 5.0) -> tuple[dict, int]:
    """POST /api/projects"""
    p = proj_mod.create(name=name, template=template, description=description,
                        scope=scope, constraints=constraints or [], budget=budget)
    return {"ok": True, "project": {"id": p.id, "name": p.name}}, 200


def project_detail(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>"""
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    return proj.to_dict() if hasattr(proj, 'to_dict') else {"ok": True}, 200


def project_gate_confirm(project_id: str, gate: str = "", decision: str = "",
                          feedback: str = "") -> tuple[dict, int]:
    """POST /api/projects/<id>/gate-confirm"""
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
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    if not hasattr(proj, 'phase') or proj.phase is None:
        return {"error": "项目未设定阶段"}, 400
    phase = phase_name or proj.phase.value
    task_desc = task_desc or f"[{project_id}] {phase} 阶段任务"
    agents = disp_mod.load_agents()
    result = wf_mod.run_phase(proj, agents)
    if push_event:
        push_event("system", f"[{project_id[:8]}] {phase} 阶段已启动")
    return {"ok": True, "phase": phase, "result": str(result)}, 200


def project_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/start"""
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
    p = proj_mod.load(project_id)
    if p is None:
        return {"error": "项目不存在"}, 404
    cost_rates = {Phase.RESEARCHING: 0.02, Phase.PLANNING: 2.50, Phase.REVIEWING: 1.00}
    phase_levels = {Phase.RESEARCHING: "E", Phase.PLANNING: "D", Phase.REVIEWING: "D"}
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
    tpl = _get_template(project_id)
    if tpl:
        return {"lineage": tpl}, 200
    all_tasks = _list_all_tasks()
    proj_tasks = [t for t in all_tasks if t.get("project_id") == project_id]
    return {"tasks": proj_tasks, "total": len(proj_tasks)}, 200


def project_snapshot(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/snapshot"""
    snap = snap_mod.take(project_id)
    return {"ok": True, "snapshot_id": snap.id, "ref": snap.ref}, 200


def project_auto(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/auto — 自动运行下一个阶段。"""
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    next_phase = proj_mod.advance_phase(project_id)
    if next_phase is None:
        return {"error": "无下一阶段"}, 400
    return {"ok": True, "phase": next_phase.value if hasattr(next_phase, 'value') else str(next_phase)}, 200


def project_autopilot_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/autopilot — 启动自驾模式。"""
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    result = _conductor.start_autopilot(project_id)
    if push_event:
        push_event("system", f"[{project_id[:8]}] autopilot 已启动")
    return {"ok": True, "result": result}, 200


def project_autopilot_stop(project_id: str, push_event=None) -> tuple[dict, int]:
    """DELETE /api/projects/<id>/autopilot"""
    _conductor.stop_autopilot(project_id)
    if push_event:
        push_event("system", f"[{project_id[:8]}] autopilot 已停止")
    return {"ok": True}, 200


def project_lineup_get(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>/lineup"""
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    lineup = getattr(proj, 'agent_lineup', {}) or {}
    return {"lineup": lineup}, 200


def project_lineup_set(project_id: str, lineup: dict) -> tuple[dict, int]:
    """PUT /api/projects/<id>/lineup"""
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    proj.agent_lineup = lineup
    proj_mod.save(proj)
    return {"ok": True}, 200

