"""_api.py — API handler 层

从 app.py 路由 handler 中提取的业务逻辑。
app.py 的路由只做: 参数校验 → 调 handler → jsonify 返回。

分层规则:
- _api.py 可以 import scheduler.* 的任何模块
- _api.py 不 import Flask（返回 dict/tuple，由 app.py 包装）
"""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Optional

from . import config
from . import tracker
from .tracker import TaskStatus
from . import witness
from . import orchestrator
from . import neijinglu
from . import dispatcher as disp_mod
from . import mcp as mcp_mod


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _list_all_tasks() -> list[dict]:
    """列出所有任务文件的原始数据。"""
    tasks_dir = tracker._tasks_dir()
    if not tasks_dir.exists():
        return []
    result = []
    for f in sorted(tasks_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.stem
            result.append(data)
        except Exception:
            pass
    return result


def _read_task_file(path: Path) -> Optional[dict]:
    """读取单个任务文件。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 任务 API handlers
# ═══════════════════════════════════════════════════════════════

def task_list(status_filter: str = "", level_filter: str = "") -> tuple[dict, int]:
    """GET /api/tasks"""
    all_tasks = _list_all_tasks()
    result = []
    now = time.time()
    for t in all_tasks:
        if status_filter and t.get("status") != status_filter:
            continue
        if level_filter and t.get("route_level") != level_filter:
            continue
        created = t.get("created_at", 0)
        updated = t.get("updated_at", created)
        result.append({
            "id": t.get("id", t["_filename"]),
            "description": (t.get("description", "") or "")[:120],
            "status": t.get("status", "unknown"),
            "route_level": t.get("route_level", ""),
            "route_type": t.get("route_type", ""),
            "priority": t.get("priority", 0),
            "depends_on": t.get("depends_on", []),
            "children": t.get("children", []),
            "error": (t.get("error", "") or "")[:200],
            "retry_count": t.get("retry_count", 0),
            "created_at": created,
            "wait_sec": round(now - created) if created else 0,
            "duration_sec": round(updated - created) if t.get("status") in ("done", "failed") else None,
        })
    return {"tasks": result, "total": len(result)}, 200


def task_detail(task_id: str) -> tuple[dict, int]:
    """GET /api/tasks/<id>"""
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return {"error": "任务不存在"}, 404
    data = _read_task_file(task_path)
    if not data:
        return {"error": "读取失败"}, 500
    now = time.time()
    created = data.get("created_at", 0)
    updated = data.get("updated_at", created)
    data["wait_sec"] = round(now - created) if created else 0
    data["duration_sec"] = round(updated - created) if created else 0
    # DAG 关系
    data["_dag_parents"] = []
    data["_dag_children"] = []
    for dep_id in data.get("depends_on", []):
        dep_path = tracker._tasks_dir() / f"{dep_id}.json"
        if dep_path.exists():
            dep_data = _read_task_file(dep_path)
            if dep_data:
                data["_dag_parents"].append({
                    "id": dep_id,
                    "description": (dep_data.get("description", "") or "")[:80],
                    "status": dep_data.get("status", "unknown"),
                })
    for child_id in data.get("children", []):
        child_path = tracker._tasks_dir() / f"{child_id}.json"
        if child_path.exists():
            child_data = _read_task_file(child_path)
            if child_data:
                data["_dag_children"].append({
                    "id": child_id,
                    "description": (child_data.get("description", "") or "")[:80],
                    "status": child_data.get("status", "unknown"),
                })
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    data["_has_trace"] = trace_path.exists()
    return data, 200


def task_trace(task_id: str, section: str = "", fmt: str = "") -> tuple:
    """GET /api/tasks/<id>/trace"""
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    if not trace_path.exists():
        return {"error": "Trace 文件不存在"}, 404
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"error": "Trace 文件读取失败"}, 500

    if fmt == "md":
        from scheduler.neijinglu import DeliveryReport, format_report
        report = DeliveryReport.from_dict(data)
        return format_report(report), 200, {"Content-Type": "text/plain; charset=utf-8"}

    if section == "route":
        route = data.get("route", {})
        return {
            "level": route.get("level", "?"),
            "gate_required": route.get("gate_required", False),
            "task_type": route.get("task_type", "default"),
            "matched_signals": route.get("matched_signals", []),
        }, 200
    elif section == "pre_search":
        ps = data.get("pre_search", {})
        return {
            "skipped": ps.get("skipped", True),
            "reason": ps.get("reason", ""),
            "top_decisions": ps.get("top_decisions", []),
            "memory": ps.get("memory", {}),
        }, 200
    elif section == "validation":
        val = data.get("validation", {})
        return {
            "verdict": val.get("verdict", "?"),
            "action": val.get("action", "?"),
            "validate_verdict": val.get("validate_verdict", ""),
            "validate_reason": val.get("validate_reason", ""),
            "gate_passed": val.get("gate_passed"),
            "gate_message": val.get("gate_message", ""),
            "turns_used": val.get("turns_used", 0),
            "unverified": val.get("unverified", []),
            "changed_files": data.get("changed_files", []),
            "agent_output": data.get("agent_output", ""),
            "token_count": data.get("token_count", 0),
            "elapsed": data.get("elapsed", 0),
        }, 200
    return data, 200


def task_timeline(task_id: str) -> tuple[dict, int]:
    """GET /api/tasks/<id>/timeline"""
    task_path = tracker._tasks_dir() / f"{task_id}.json"
    if not task_path.exists():
        return {"error": "任务不存在"}, 404
    task_data = _read_task_file(task_path)
    if not task_data:
        return {"error": "读取失败"}, 500
    timeline = []
    status = task_data.get("status", "pending")
    created_at = task_data.get("created_at", 0)
    updated_at = task_data.get("updated_at", created_at)
    timeline.append({"from": None, "to": "pending", "timestamp": created_at, "meta": {}})
    route_level = task_data.get("route_level", "")
    if status not in ("pending",) and route_level:
        timeline.append({
            "from": "pending", "to": "routed",
            "timestamp": task_data.get("routed_at", updated_at),
            "meta": {"route_level": route_level, "route_gate": task_data.get("route_gate", False),
                     "route_type": task_data.get("route_type", "default")},
        })
    if status in ("dispatched", "running", "validating", "done", "failed", "rolled_back", "decomposed", "conflict_held"):
        timeline.append({"from": "routed", "to": "dispatched", "timestamp": updated_at, "meta": {}})
    if task_data.get("snapshot_id"):
        timeline.append({"from": "dispatched", "to": "running", "timestamp": updated_at,
                         "meta": {"snapshot_id": task_data.get("snapshot_id", "")}})
    if status in ("done", "failed", "rolled_back", "decomposed", "conflict_held"):
        prev = "validating" if status in ("done", "failed") else "running"
        meta = {}
        if status == "failed":
            meta["error"] = (task_data.get("error", "") or "")[:200]
        if status == "rolled_back":
            meta["rolled_back"] = True
        timeline.append({"from": prev, "to": status, "timestamp": updated_at, "meta": meta})
    trace_path = config.TRACE_DIR / f"{task_id}.json"
    if trace_path.exists():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            timeline.append({
                "from": None, "to": "_trace", "timestamp": updated_at,
                "meta": {
                    "route": trace.get("route", {}).get("matched_signals", []),
                    "elapsed": trace.get("elapsed"),
                    "token_count": trace.get("token_count"),
                    "changed_files": trace.get("changed_files", []),
                    "validation_verdict": trace.get("validation", {}).get("verdict"),
                    "pre_search_escalated": trace.get("pre_search", {}).get("escalated"),
                },
            })
        except Exception:
            pass
    return {"task_id": task_id, "current_status": status, "timeline": timeline}, 200


def task_hold(task_id: str, reason: str = "") -> tuple[dict, int]:
    """POST /api/tasks/<id>/hold"""
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持扣留"}, 400
    tracker.transition(task_id, task.status, held=True, held_reason=reason)
    return {"ok": True, "held": True, "reason": reason}, 200


def task_release(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/release"""
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if not task.held:
        return {"error": "任务未被扣留"}, 400
    tracker.transition(task_id, task.status, held=False, held_reason="")
    return {"ok": True, "held": False}, 200


def task_override_route(task_id: str, level: str, locked: bool = True) -> tuple[dict, int]:
    """POST /api/tasks/<id>/override-route"""
    if level not in ("E", "D", "E+"):
        return {"error": "level 必须是 E / D / E+"}, 400
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持覆盖路由"}, 400
    tracker.transition(task_id, task.status, route_level=level, route_locked=locked)
    return {"ok": True, "route_level": level, "locked": locked}, 200


def task_cancel(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/cancel"""
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK, TaskStatus.DECOMPOSED):
        return {"error": f"终态任务 {task.status.value} 不可取消"}, 400
    config.ensure_dirs()
    if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
        cancel_path = config.CANCEL_DIR / f"{task_id}.json"
        cancel_path.write_text(json.dumps({"task_id": task_id, "cancelled_at": time.time()}), encoding="utf-8")
        return {"ok": True, "message": "已发送取消信号，将在当前 turn 结束后生效"}, 200
    else:
        tracker.transition(task_id, TaskStatus.FAILED, error="用户手动取消")
        return {"ok": True, "message": "已取消"}, 200


def task_delete(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/delete"""
    config.ensure_dirs()
    deleted = 0
    for d in [config.CANCEL_DIR, config.TRACE_DIR, tracker._tasks_dir()]:
        p = d / f"{task_id}.json"
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        return {"ok": True, "message": f"已删除 {deleted} 个文件"}, 200
    return {"error": "任务文件不存在"}, 404


def task_retry(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/retry"""
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.FAILED, TaskStatus.ROLLED_BACK):
        return {"error": f"当前状态 {task.status.value} 不支持重试"}, 400
    tracker.transition(task_id, TaskStatus.PENDING, error="", retry_count=0)
    return {"ok": True, "new_status": "pending"}, 200


def task_approval(task_id: str, decision: str = "reject", action: str = "", push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/approval"""
    if push_event:
        push_event("system", f"[{task_id[:8]}] 用户{decision}了 {action}")
    return {"ok": True, "decision": decision}, 200


def task_apply(task_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/apply — 应用 E+ 智谱 patch 到工作区。"""
    from .executors.zhipu_api import ZhipuApiExecutor
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    result = ZhipuApiExecutor.apply_patch(task_id)
    success = bool(result.get("applied"))
    msg = result.get("message", "")
    if push_event:
        push_event("system", f"[{task_id[:8]}] apply: {msg}")
    return {"ok": success, "message": msg}, 200


def task_rollback(task_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/rollback"""
    try:
        from . import rollback as rb_mod
        ok, msg = rb_mod.rollback(task_id)
        if push_event:
            push_event("system", f"[{task_id[:8]}] rollback: {msg}")
        return {"ok": ok, "message": msg}, (200 if ok else 400)
    except ImportError:
        return {"error": "rollback 模块不可用"}, 500


def task_supervise(task_id: str, data: dict, push_event=None) -> tuple[dict, int]:
    """POST /api/tasks/<id>/supervise — 监督者介入。"""
    from .supervisor import supervise
    task = tracker._read(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    verdict = supervise(
        task_description=task.description,
        changed_files=data.get("changed_files", []),
        constraints=data.get("constraints", []),
        checklist=data.get("checklist", []),
        agent_output=data.get("agent_output", ""),
        task_id=task_id,
    )
    result = {"verdict": verdict.verdict, "action": verdict.verdict, "issues": verdict.issues}
    if push_event:
        push_event("system", f"[{task_id[:8]}] 监督介入: {verdict.verdict}")
    return result, 200


def task_submit(desc: str, priority: int = 0, depends_on: list = None,
                route_level: str = "", route_locked: bool = True,
                route_type: str = "",
                push_event=None) -> tuple[dict, int]:
    """POST /api/tasks — 创建新任务。"""
    config.ensure_dirs()
    task = tracker.create(desc, priority=priority, depends_on=depends_on or [])
    if route_level or route_type:
        kwargs = {}
        if route_level:
            kwargs["route_level"] = route_level
            kwargs["route_locked"] = route_locked
        if route_type:
            kwargs["route_type"] = route_type
        tracker.transition(task.id, TaskStatus.PENDING, **kwargs)
    if push_event:
        push_event("task_create", f"[{task.id[:8]}] {desc[:60]}")
    return {"ok": True, "task_id": task.id, "description": desc[:120]}, 200


# ═══════════════════════════════════════════════════════════════
# 冲突 API handlers
# ═══════════════════════════════════════════════════════════════

def conflict_list() -> tuple[dict, int]:
    """GET /api/conflicts"""
    all_tasks = _list_all_tasks()
    conflicts = [t for t in all_tasks if t.get("status") == TaskStatus.CONFLICT_HELD.value]
    result = []
    for t in conflicts:
        result.append({
            "id": t.get("id", t["_filename"]),
            "description": (t.get("description", "") or "")[:120],
            "error": (t.get("error", "") or "")[:300],
            "created_at": t.get("created_at", 0),
            "route_level": t.get("route_level", ""),
        })
    return {"conflicts": result, "total": len(result)}, 200


# ═══════════════════════════════════════════════════════════════
# 记忆 API handlers
# ═══════════════════════════════════════════════════════════════

def memory_query(query_text: str = "", files_str: str = "",
                 beam_width: int = 3, max_hops: int = 3, max_depth: int = 1) -> tuple[dict, int]:
    """GET /api/memory — MAGMA 多图记忆查询。"""
    from . import memory as mem_mod
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    results = mem_mod.query(
        query_text, files=files, beam_width=beam_width,
        max_hops=max_hops, max_depth=max_depth,
    )
    return results, 200


def memory_chain(task_id: str) -> tuple[dict, int]:
    """GET /api/memory/chain/<id>"""
    from . import memory as mem_mod
    chain = mem_mod.get_task_chain(task_id)
    if chain is None:
        return {"error": "无记忆链"}, 404
    return chain, 200


def memory_rebuild() -> tuple[dict, int]:
    """POST /api/memory/rebuild"""
    count = orchestrator.consolidate_memory()
    return {"ok": True, "consolidated": count}, 200


# ═══════════════════════════════════════════════════════════════
# 项目 API handlers
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
    p = proj_mod.create(name=name, template=template, description=description,
                        scope=scope, constraints=constraints or [], budget=budget)
    return {"ok": True, "project": {"id": p.id, "name": p.name}}, 200


def project_detail(project_id: str) -> tuple[dict, int]:
    """GET /api/projects/<id>"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    return proj.to_dict() if hasattr(proj, 'to_dict') else {"ok": True}, 200


def project_gate_confirm(project_id: str, gate: str = "", decision: str = "") -> tuple[dict, int]:
    """POST /api/projects/<id>/gate-confirm"""
    from . import project as proj_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    # gate-confirm 暂为 no-op (安全关口需手动确认)
    return {"ok": True, "gate": gate, "decision": decision or "skip"}, 200


def project_run_phase(project_id: str, phase_name: str = "",
                      task_desc: str = "", agent_override: str = "",
                      push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/run-phase"""
    from . import project as proj_mod
    from . import workflow as wf_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    if not hasattr(proj, 'phase') or proj.phase is None:
        return {"error": "项目未设定阶段"}, 400
    phase = phase_name or proj.phase.value
    task_desc = task_desc or f"[{project_id}] {phase} 阶段任务"
    from . import dispatcher as disp_mod
    agents = disp_mod.load_agents()
    result = wf_mod.run_phase(proj, agents)
    if push_event:
        push_event("system", f"[{project_id[:8]}] {phase} 阶段已启动")
    return {"ok": True, "phase": phase, "result": str(result)}, 200


def project_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/start"""
    from . import project as proj_mod
    from . import workflow as wf_mod
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    from . import dispatcher as disp_mod
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
    from .task_templates import get as _get_template
    tpl = _get_template(project_id)
    if tpl:
        return {"lineage": tpl}, 200
    all_tasks = _list_all_tasks()
    proj_tasks = [t for t in all_tasks if t.get("project_id") == project_id]
    return {"tasks": proj_tasks, "total": len(proj_tasks)}, 200


def project_snapshot(project_id: str) -> tuple[dict, int]:
    """POST /api/projects/<id>/snapshot"""
    from . import snapshot as snap_mod
    snap = snap_mod.take(project_id)
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


def project_autopilot_start(project_id: str, push_event=None) -> tuple[dict, int]:
    """POST /api/projects/<id>/autopilot — 启动自驾模式。"""
    from . import project as proj_mod
    from . import conductor as _conductor
    proj = proj_mod.load(project_id)
    if proj is None:
        return {"error": "项目不存在"}, 404
    result = _conductor.start_autopilot(project_id)
    if push_event:
        push_event("system", f"[{project_id[:8]}] autopilot 已启动")
    return {"ok": True, "result": result}, 200


def project_autopilot_stop(project_id: str, push_event=None) -> tuple[dict, int]:
    """DELETE /api/projects/<id>/autopilot"""
    from . import conductor as _conductor
    _conductor.stop_autopilot(project_id)
    if push_event:
        push_event("system", f"[{project_id[:8]}] autopilot 已停止")
    return {"ok": True}, 200


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
# 模板 API
# ═══════════════════════════════════════════════════════════════

def template_list() -> tuple[dict, int]:
    """GET /api/templates"""
    from .task_templates import list_all
    templates = list_all()
    return {"templates": [{"id": tid, "name": t.name, "description": t.description,
                           "success_criteria": t.success_criteria,
                           "suggested_max_turns": t.suggested_max_turns,
                           "recommended_models": t.recommended_models}
                          for tid, t in templates.items()]}, 200


# ═══════════════════════════════════════════════════════════════
# Agent API handlers
# ═══════════════════════════════════════════════════════════════

def agent_list() -> tuple[dict, int]:
    """GET /api/agents"""
    raw = disp_mod.load_agents()
    custom = disp_mod._load_custom_agents()
    order_map = custom.get("_order", {}) or {}
    result = {}
    for level, cfgs in raw.items():
        rank = {m: i for i, m in enumerate(order_map.get(level, []))}
        sorted_cfgs = sorted(cfgs, key=lambda c: rank.get(c.get("model", ""), 999))
        result[level] = []
        for c in sorted_cfgs:
            result[level].append({
                "model": c.get("model", ""),
                "type": c.get("type", ""),
                "roles": c.get("roles", []),
                "max_turns": c.get("max_turns", 0),
                "entry": c.get("entry", ""),
                "api_key_env": c.get("api_key_env", ""),
                "default": c.get("default", False),
                "mode": c.get("mode", ""),
                "sandbox": c.get("sandbox", ""),
            })
    result["_order"] = order_map
    result["_disabled"] = custom.get("_disabled", {}) or {}
    return result, 200


def agent_add(level: str, model: str, agent_type: str = "openai-agent",
              entry_url: str = "", api_key_env: str = "", max_turns: int = 5,
              roles: list = None, sandbox: str = "worktree", mode: str = "",
              request_template: dict = None) -> tuple[dict, int]:
    """POST /api/agents"""
    cfg = disp_mod.add_agent(
        level=level, model=model, agent_type=agent_type,
        entry=entry_url, api_key_env=api_key_env, max_turns=max_turns,
        roles=roles or [], sandbox=sandbox, mode=mode,
        request_template=request_template,
    )
    return {"ok": True, "agent": cfg}, 200


def agent_update(level: str, model: str, data: dict) -> tuple[dict, int]:
    """PUT /api/agents/<level>/<model>"""
    cfg = disp_mod.update_agent(level, model, data)
    return {"ok": True, "agent": cfg}, 200


def agent_remove(level: str, model: str) -> tuple[dict, int]:
    """DELETE /api/agents/<level>/<model>"""
    ok = disp_mod.remove_agent(level, model)
    return {"ok": ok}, 200


# ═══════════════════════════════════════════════════════════════
# API Store handlers
# ═══════════════════════════════════════════════════════════════

def api_store_list() -> tuple[dict, int]:
    """GET /api/api-store"""
    from . import api_store
    entries = api_store.list_all()
    return {k: {
        "id": v.id, "provider": v.provider, "base_url": v.base_url,
        "api_key_env": v.api_key_env, "status": v.status,
        "notes": v.notes, "available": api_store.is_available(v.id),
        "updated_at": v.updated_at,
    } for k, v in entries.items()}, 200


def api_store_add(api_id: str, provider: str = "", base_url: str = "",
                  api_key_env: str = "", notes: str = "") -> tuple[dict, int]:
    """POST /api/api-store"""
    from . import api_store
    entry = api_store.add(
        api_id=api_id, provider=provider or api_id,
        base_url=base_url, api_key_env=api_key_env, notes=notes,
    )
    scanned = []
    try:
        models = api_store.scan_models(api_id)
        for m in models:
            api_store.save_custom_model(m["id"], m["provider"], m.get("display", m["id"]))
            scanned.append(m["id"])
    except Exception:
        pass
    return {"ok": True, "entry": entry.to_dict(), "scanned_models": scanned}, 200


def api_store_remove(api_id: str) -> tuple[dict, int]:
    """DELETE /api/api-store/<id>"""
    from . import api_store
    ok = api_store.remove(api_id)
    return {"ok": ok}, 200


def api_store_set_status(api_id: str, status: str, notes: str = "") -> tuple[dict, int]:
    """PUT /api/api-store/<id>/status"""
    from . import api_store
    entry = api_store.set_status(api_id, status, notes)
    if not entry:
        return {"error": f"API {api_id} 不存在"}, 404
    return {"ok": True, "entry": entry.to_dict()}, 200


# ═══════════════════════════════════════════════════════════════
# Auth handlers
# ═══════════════════════════════════════════════════════════════

def auth_status() -> tuple[dict, int]:
    """GET /api/auth/status"""
    from ._auth import get_auth
    users = get_auth().list_users()
    return {"enabled": os.environ.get("QIDIAN_AUTH") == "1", "users": users}, 200


def auth_bootstrap() -> tuple[dict, int]:
    """POST /api/auth/bootstrap — 仅首次无用户时可用。明文 token 仅 console 打印。"""
    from ._auth import get_auth
    auth = get_auth()
    # 已有用户时拒绝重复 bootstrap
    if auth._users:
        return {"ok": False, "error": "已有用户，bootstrap 不可重复调用"}, 403
    admin = auth.bootstrap()
    return {"ok": True, "user": admin.to_dict(), "message": f"Admin 创建成功，token: {admin.token[:8]}...（完整 token 已在服务端 console 打印）"}, 200


def auth_add_user(uid: str, name: str = "", role: str = "viewer") -> tuple[dict, int]:
    """POST /api/auth/users"""
    from ._auth import get_auth
    u = get_auth().add_user(uid, name, role)
    return {"ok": True, "user": u.to_dict()}, 200


def auth_remove_user(user_id: str) -> tuple[dict, int]:
    """DELETE /api/auth/users/<id>"""
    from ._auth import get_auth
    if get_auth().remove_user(user_id):
        return {"ok": True}, 200
    return {"error": "用户不存在"}, 404


# ═══════════════════════════════════════════════════════════════
# Model handlers
# ═══════════════════════════════════════════════════════════════

def model_list() -> tuple[dict, int]:
    """GET /api/models"""
    from . import model_registry
    from . import api_store
    models = model_registry.load_models()
    return {mid: {
        "id": m.id, "provider": m.provider, "display": m.display,
        "tiers": m.tiers, "speed": m.speed, "cost": m.cost,
        "reasoning": m.reasoning, "max_turns": m.max_turns,
        "strengths": m.strengths, "notes": m.notes,
        "api_available": api_store.is_available(m.provider),
    } for mid, m in models.items()}, 200


def model_list_for_tier(tier: str) -> tuple[dict, int]:
    """GET /api/models/tier/<tier>"""
    from . import model_registry
    from . import api_store
    models = model_registry.for_tier(tier, available_only=False)
    return [{
        "id": m.id, "provider": m.provider, "display": m.display,
        "cost": m.cost, "speed": m.speed,
        "api_available": api_store.is_available(m.provider),
    } for m in models], 200


def model_add(model_id: str, provider: str = "", display: str = "",
              tiers: list = None, speed: str = "medium", cost: str = "standard",
              reasoning: bool = False, max_turns: int = 5,
              strengths: str = "", notes: str = "") -> tuple[dict, int]:
    """POST /api/models"""
    from . import model_registry
    from . import api_store
    model_registry.add_model(
        model_id, provider, display, tiers or ["E"],
        speed, cost, reasoning, max_turns,
    )
    return {"ok": True, "model_id": model_id}, 200


def model_remove(model_id: str) -> tuple[dict, int]:
    """DELETE /api/models/<id>"""
    from . import model_registry
    ok = model_registry.remove_model(model_id)
    return {"ok": ok}, 200


def model_update(model_id: str, data: dict) -> tuple[dict, int]:
    """PUT /api/models/<id>"""
    from . import model_registry
    from . import api_store
    # 读取现有模型
    models = model_registry.load_models()
    if model_id not in models:
        return {"error": "模型不存在"}, 404
    m = models[model_id]
    model_registry.add_model(
        model_id,
        data.get("provider", m.provider),
        data.get("display", m.display),
        data.get("tiers", m.tiers),
        data.get("speed", m.speed),
        data.get("cost", m.cost),
        data.get("reasoning", m.reasoning),
        data.get("max_turns", m.max_turns),
    )
    return {"ok": True, "model_id": model_id}, 200


# ═══════════════════════════════════════════════════════════════
# Skill handlers
# ═══════════════════════════════════════════════════════════════

def skill_list() -> tuple[dict, int]:
    """GET /api/skills"""
    try:
        from skills.skill_loader import load_skills, get_agent_skills
        all_skills = load_skills()
        skills_data = []
        for name, skill in all_skills.items():
            skills_data.append({
                "name": skill.name, "description": skill.description,
                "type": skill.type, "args": skill.arguments,
                "source": skill.source,
                "body": skill.body[:200],
            })
        return {"skills": skills_data}, 200
    except Exception as e:
        return {"error": str(e)}, 500


def skill_add(name: str, description: str = "", skill_type: str = "prompt",
              args: list = None, body: str = "") -> tuple[dict, int]:
    """POST /api/skills"""
    from skills.skill_loader import create_user_skill
    create_user_skill(name, description, skill_type, args or [], body)
    disp_mod.invalidate_skill_cache()  # skill 定义变了, 清全部
    return {"ok": True, "name": name}, 200


def skill_delete(name: str) -> tuple[dict, int]:
    """DELETE /api/skills/<name>"""
    from skills.skill_loader import delete_user_skill
    ok = delete_user_skill(name)
    disp_mod.invalidate_skill_cache()  # skill 定义变了, 清全部
    return {"ok": ok}, 200


def agent_skill_list(level: str, model: str) -> tuple[dict, int]:
    """GET /api/agents/<level>/<model>/skills"""
    from skills.skill_loader import get_agent_skills, load_skills
    skill_names = get_agent_skills(level, model)
    all_skills = load_skills()
    return {"skill_names": skill_names, "available": list(all_skills.keys())}, 200


def agent_skill_update(level: str, model: str, skill_names: list) -> tuple[dict, int]:
    """PUT /api/agents/<level>/<model>/skills"""
    from skills.skill_loader import set_agent_skills
    set_agent_skills(level, model, skill_names)
    disp_mod.invalidate_skill_cache(level, model)  # 只清该 agent 的缓存
    return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# Permission handlers
# ═══════════════════════════════════════════════════════════════

def perm_profiles() -> tuple[dict, int]:
    """GET /api/permissions/profiles"""
    from .permission import get_store
    return {"profiles": get_store().list_profiles()}, 200


def perm_profiles_add(name: str, profile: dict) -> tuple[dict, int]:
    """POST /api/permissions/profiles"""
    from .permission import get_store
    get_store().add_profile(name, profile)
    return {"ok": True}, 200


def perm_profiles_delete(name: str) -> tuple[dict, int]:
    """DELETE /api/permissions/profiles/<name>"""
    from .permission import get_store
    get_store().remove_profile(name)
    return {"ok": True}, 200


def perm_bind(level: str, model: str, profile: str) -> tuple[dict, int]:
    """PUT /api/permissions/bindings"""
    from .permission import get_store
    get_store().bind_agent(level, model, profile)
    return {"ok": True}, 200


def perm_unbind(level: str, model: str) -> tuple[dict, int]:
    """DELETE /api/permissions/bindings/<level>/<model>"""
    from .permission import get_store
    get_store().unbind_agent(level, model)
    return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# 监控 & 循环 API handlers
# ═══════════════════════════════════════════════════════════════

def loop_status() -> dict:
    """GET /api/loop/status — 返回循环状态 (由 app.py 包装)。"""
    return {}  # 实际实现依赖 app.py 的 _loop_running 等全局变量


def status_overview() -> tuple[dict, int]:
    """GET /api/status"""
    from . import witness
    from . import dispatcher as disp_mod
    config.ensure_dirs()
    counts = witness._count_by_status()
    loads = witness._heartbeat_task_levels()
    pending_waits, done_durations = witness._timing_stats()
    token_totals = witness._token_stats()
    stalled = witness.check_stalled(timeout_seconds=600)
    agents = {}
    try:
        raw_agents = disp_mod.load_agents()
        for level, cfgs in raw_agents.items():
            agents[level] = [{"model": c.get("model", ""), "roles": c.get("roles", [])} for c in cfgs]
    except Exception:
        pass
    return {
        "counts": counts, "heartbeat_levels": loads,
        "running_total": sum(loads.values()),
        "avg_wait": format(sum(pending_waits) / len(pending_waits), ".1f") + "s" if pending_waits else "--",
        "avg_done": format(sum(done_durations) / len(done_durations), ".1f") + "s" if done_durations else "--",
        "token_totals": token_totals, "stalled": stalled, "agents": agents,
    }, 200


def cleanup() -> tuple[dict, int]:
    """POST /api/cleanup — 清理残留心跳和缓存。"""
    try:
        witness.cleanup()
    except AttributeError:
        pass  # witness 模块暂无 cleanup 函数，后续补充
    return {"ok": True, "cleaned": {"heartbeats": 0, "tasks": 0}}, 200


def token_usage() -> tuple[dict, int]:
    """GET /api/token-usage"""
    from ._token_budget import get_usage_stats
    return get_usage_stats(), 200


def token_budget_set(budget: float) -> tuple[dict, int]:
    """PUT /api/token-budget"""
    from ._token_budget import set_budget
    set_budget(budget)
    return {"ok": True, "budget": budget}, 200


def perf_stats() -> tuple[dict, int]:
    """GET /api/perf"""
    from ._profiler import get_perf_stats
    return get_perf_stats(), 200


def dag_metrics() -> tuple[dict, int]:
    """GET /api/dag-metrics"""
    metrics = tracker.dag_metrics()
    return metrics, 200


def judge_monitor_status() -> tuple[dict, int]:
    """GET /api/judge-monitor"""
    from .judge_monitor import JudgeMonitorStore
    from . import config
    jm = JudgeMonitorStore(config.QIDIAN_DIR / "judge_monitor.json")
    jm.load()
    return jm.get_stats(), 200


def model_profile_status() -> tuple[dict, int]:
    """GET /api/model-profile"""
    from .model_profile import ProfileStore
    from . import config
    ps = ProfileStore(config.QIDIAN_DIR / "model_profile.json")
    ps.load()
    return {"profiles": ps.summary()}, 200


def model_profile_pattern() -> tuple[dict, int]:
    """GET /api/model-profile/pattern"""
    from .model_profile import ProfileStore
    from . import config
    ps = ProfileStore(config.QIDIAN_DIR / "model_profile.json")
    ps.load()
    return {"patterns": ps.pattern_summary()}, 200


def reports_critical() -> tuple[dict, int]:
    """GET /api/reports/critical"""
    from . import chancellor as chan_mod
    return chan_mod.recent_critical(), 200


def conflict_resolve(task_id: str, resolution: dict, push_event=None) -> tuple[dict, int]:
    """POST /api/conflicts/<id>/resolve"""
    from .merge import resolve_conflict
    result = resolve_conflict(task_id, resolution)
    if push_event:
        push_event("system", f"[{task_id[:8]}] conflict resolved")
    return {"ok": True, "result": result}, 200


# rollback_all 已移除 — rollback 模块不存在, 此函数无调用方且缺 ImportError 守卫。
# task_rollback (L334) 保留, 有 ImportError 守卫降级返回 500。


# ═══════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════

def health_check(loop_running: bool, sse_clients: int) -> tuple[dict, int]:
    """GET /health"""
    import shutil
    disk = shutil.disk_usage(str(config.QIDIAN_DIR))
    return {
        "status": "ok",
        "disk_free_mb": disk.free // (1024 * 1024),
        "loop_running": loop_running,
        "sse_clients": sse_clients,
        "projects": len({}),  # filled by caller
    }, 200


# ═══════════════════════════════════════════════════════════════
# MCP CRUD (下沉自 app.py:1124-1197)
# ═══════════════════════════════════════════════════════════════

def mcp_server_list() -> tuple[dict, int]:
    """GET /api/mcp/servers"""
    configs = mcp_mod.load_mcp_configs()
    registry = mcp_mod.get_registry()
    servers = []
    for c in configs:
        connected = c.name in registry._clients
        tool_count = len(registry._clients[c.name]._tools) if connected else 0
        servers.append({"name": c.name, "transport": c.transport, "command": c.command,
                        "url": c.url, "enabled": c.enabled, "timeout": c.timeout,
                        "connected": connected, "tool_count": tool_count})
    return {"servers": servers}, 200


def mcp_server_add(data: dict) -> tuple[dict, int]:
    """POST /api/mcp/servers"""
    if not data or not data.get("name"):
        return {"error": "缺少 name"}, 400
    configs = mcp_mod.load_mcp_configs()
    found = False
    for c in configs:
        if c.name == data["name"]:
            c.transport = data.get("transport", c.transport)
            c.command = data.get("command", c.command)
            c.url = data.get("url", c.url)
            c.enabled = data.get("enabled", c.enabled)
            c.timeout = data.get("timeout", c.timeout)
            c.env = data.get("env", c.env)
            found = True
            break
    if not found:
        configs.append(mcp_mod.MCPServerConfig(
            name=data["name"], transport=data.get("transport", "stdio"),
            command=data.get("command", ""), url=data.get("url", ""),
            enabled=data.get("enabled", True), timeout=data.get("timeout", 30.0),
            env=data.get("env", {})))
    mcp_mod.save_mcp_configs(configs)
    return {"ok": True}, 200


def mcp_server_delete(name: str) -> tuple[dict, int]:
    """DELETE /api/mcp/servers/<name>"""
    configs = mcp_mod.load_mcp_configs()
    configs = [c for c in configs if c.name != name]
    mcp_mod.save_mcp_configs(configs)
    return {"ok": True}, 200


def mcp_server_reconnect(name: str) -> tuple[dict, int]:
    """POST /api/mcp/servers/<name>/reconnect"""
    configs = mcp_mod.load_mcp_configs()
    registry = mcp_mod.get_registry()
    for c in configs:
        if c.name == name:
            if name in registry._clients:
                registry._clients[name].disconnect()
                del registry._clients[name]
                registry._tools = [t for t in registry._tools if t.server_name != name]
                registry._tool_index = {k: v for k, v in registry._tool_index.items() if v.cfg.name != name}
            registry.load_configs([c])
            return {"ok": True, "tool_count": len(registry._tools)}, 200
    return {"error": f"服务器 {name} 不存在"}, 404


def mcp_tool_list() -> tuple[dict, int]:
    """GET /api/mcp/tools"""
    registry = mcp_mod.get_registry()
    tools = [{"name": f"mcp__{t.server_name}__{t.name}", "server": t.server_name,
              "tool": t.name, "description": t.description, "inputSchema": t.inputSchema}
             for t in registry.get_all_tools()]
    return {"tools": tools}, 200


def mcp_refresh() -> tuple[dict, int]:
    """POST /api/mcp/refresh"""
    configs = mcp_mod.load_mcp_configs()
    mcp_mod.get_registry().load_configs(configs)
    return {"ok": True, "servers": mcp_mod.get_registry().server_count,
            "tools": mcp_mod.get_registry().tool_count}, 200
