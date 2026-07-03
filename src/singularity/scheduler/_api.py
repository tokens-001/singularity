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
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _list_all_tasks() -> list[dict]:
    tasks_dir = tracker.tasks_dir()
    if not tasks_dir.exists(): return []
    result = []
    for f in sorted(tasks_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.stem; result.append(data)
        except Exception as e: witness.heartbeat('_api', f'warn:{e}')
    return result


def _read_task_file(path: Path) -> Optional[dict]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try: witness.heartbeat('_api', 'warn:read_task_file')
        except Exception: pass
        return None


# ═══════════════════════════════════════════════════════════════
# 任务 CRUD  (ex _api_tasks.py)
# ═══════════════════════════════════════════════════════════════

def task_list(status_filter: str = "", level_filter: str = "") -> tuple[dict, int]:
    """GET /api/tasks"""
    all_tasks = _list_all_tasks()
    result = []
    now = time.time()
    for t in all_tasks:
        if status_filter and t.get("status") != status_filter:
            continue
        if level_filter and t.get("route_type") != level_filter:
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
    task_path = tracker.tasks_dir() / f"{task_id}.json"
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
        dep_path = tracker.tasks_dir() / f"{dep_id}.json"
        if dep_path.exists():
            dep_data = _read_task_file(dep_path)
            if dep_data:
                data["_dag_parents"].append({
                    "id": dep_id,
                    "description": (dep_data.get("description", "") or "")[:80],
                    "status": dep_data.get("status", "unknown"),
                })
    for child_id in data.get("children", []):
        child_path = tracker.tasks_dir() / f"{child_id}.json"
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
        from singularity.scheduler.neijinglu import DeliveryReport, format_report
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
    task_path = tracker.tasks_dir() / f"{task_id}.json"
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
        except Exception as e:
            witness.heartbeat('_api', f'warn:{e}')
    return {"task_id": task_id, "current_status": status, "timeline": timeline}, 200


def task_hold(task_id: str, reason: str = "") -> tuple[dict, int]:
    """POST /api/tasks/<id>/hold"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持扣留"}, 400
    tracker.transition(task_id, task.status, held=True, held_reason=reason)
    return {"ok": True, "held": True, "reason": reason}, 200


def task_release(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/release"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if not task.held:
        return {"error": "任务未被扣留"}, 400
    tracker.transition(task_id, task.status, held=False, held_reason="")
    return {"ok": True, "held": False}, 200


def task_override_route(task_id: str, level: str, locked: bool = True) -> tuple[dict, int]:
    """POST /api/tasks/<id>/override-route"""
    # 两档后 level 可选, 空=不限
    if False:
        return {"error": "level 必须是 E / D / E+ 或留空"}, 400
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.PENDING, TaskStatus.ROUTED):
        return {"error": f"当前状态 {task.status.value} 不支持覆盖路由"}, 400
    tracker.transition(task_id, task.status, route_level=level, route_locked=locked)
    return {"ok": True, "route_level": level, "locked": locked}, 200


def task_cancel(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/cancel"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK, TaskStatus.DECOMPOSED):
        return {"error": f"终态任务 {task.status.value} 不可取消"}, 400
    config.ensure_dirs()
    if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED, TaskStatus.PAUSED):
        # PAUSED 状态下也接受取消: 删 pause 文件 + 写 cancel 文件
        pause_path = config.PAUSE_DIR / f"{task_id}.json"
        if pause_path.exists():
            pause_path.unlink()
        cancel_path = config.CANCEL_DIR / f"{task_id}.json"
        cancel_path.write_text(json.dumps({"task_id": task_id, "cancelled_at": time.time()}), encoding="utf-8")
        return {"ok": True, "message": "已发送取消信号"}, 200
    else:
        tracker.transition(task_id, TaskStatus.FAILED, error="用户手动取消")
        return {"ok": True, "message": "已取消"}, 200


def task_pause(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/pause — GATE 人审时暂停任务。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status not in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
        return {"error": f"只有运行中的任务可暂停, 当前状态: {task.status.value}"}, 400
    config.ensure_dirs()
    pause_path = config.PAUSE_DIR / f"{task_id}.json"
    pause_path.write_text(json.dumps({"task_id": task_id, "paused_at": time.time()}), encoding="utf-8")
    return {"ok": True, "message": "暂停信号已发送, 当前 turn 结束后生效"}, 200


def task_resume(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/resume — GATE 人审通过后恢复任务。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    if task.status != TaskStatus.PAUSED:
        return {"error": f"只有暂停中的任务可恢复, 当前状态: {task.status.value}"}, 400
    pause_path = config.PAUSE_DIR / f"{task_id}.json"
    if pause_path.exists():
        pause_path.unlink()
    return {"ok": True, "message": "已发送恢复信号"}, 200


def task_set_mode(task_id: str, mode: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/mode — 切换执行模式 (auto_edit | confirm_changes)。"""
    if mode not in ("auto_edit", "confirm_changes"):
        return {"error": f"无效模式: {mode}，可选 auto_edit / confirm_changes"}, 400
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    task.execution_mode = mode
    tracker._write(task)
    return {"ok": True, "task_id": task_id, "execution_mode": mode}, 200


def task_delete(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/delete"""
    config.ensure_dirs()
    deleted = 0
    for d in [config.CANCEL_DIR, config.TRACE_DIR, tracker.tasks_dir()]:
        p = d / f"{task_id}.json"
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception as e:
            witness.heartbeat('_api', f'warn:{e}')
    if deleted:
        return {"ok": True, "message": f"已删除 {deleted} 个文件"}, 200
    return {"error": "任务文件不存在"}, 404


def task_retry(task_id: str) -> tuple[dict, int]:
    """POST /api/tasks/<id>/retry"""
    task = tracker.read_task(task_id)
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
    task = tracker.read_task(task_id)
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
    task = tracker.read_task(task_id)
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
        push_event("task", json.dumps({"task_id": task.id, "status": "pending", "desc": desc[:120]}))
    return {"ok": True, "task_id": task.id, "description": desc[:120]}, 200


def task_update(task_id: str, data: dict) -> tuple[dict, int]:
    """PUT /api/tasks/<id> — 更新任务描述等字段。"""
    task = tracker.read_task(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    kwargs = {}
    if "description" in data:
        kwargs["description"] = str(data["description"])[:8000]
    if not kwargs:
        return {"error": "无可更新字段"}, 400
    tracker.transition(task_id, task.status, **kwargs)
    return {"ok": True, "task_id": task_id}, 200


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


def agent_list() -> tuple[dict, int]:
    from . import dispatcher as disp_mod
    raw = disp_mod.load_agents()
    custom = disp_mod._load_custom_agents()
    order_map = custom.get("_order", {}) or {}
    result = {}
    for level, cfgs in raw.items():
        rank = {m: i for i, m in enumerate(order_map.get(level, []))}
        sorted_cfgs = sorted(cfgs, key=lambda c: rank.get(c.get("model", ""), 999))
        result[level] = []
        for c in sorted_cfgs:
            result[level].append({"model": c.get("model", ""), "type": c.get("type", ""),
                "roles": c.get("roles", []), "max_turns": c.get("max_turns", 0),
                "entry": c.get("entry", ""), "api_key_env": c.get("api_key_env", ""),
                "default": c.get("default", False), "mode": c.get("mode", ""), "sandbox": c.get("sandbox", "")})
    result["_order"] = order_map
    result["_disabled"] = custom.get("_disabled", {}) or {}
    return result, 200


def agent_add(level, model, agent_type="openai-agent", entry_url="", api_key_env="", max_turns=5, roles=None, sandbox="worktree", mode="", request_template=None):
    from . import dispatcher as disp_mod
    cfg = disp_mod.add_agent(level=level, model=model, agent_type=agent_type, entry=entry_url,
        api_key_env=api_key_env, max_turns=max_turns, roles=roles or [], sandbox=sandbox, mode=mode, request_template=request_template)
    return {"ok": True, "agent": cfg}, 200


def agent_update(level, model, data):
    from . import dispatcher as disp_mod
    return {"ok": True, "agent": disp_mod.update_agent(level, model, data)}, 200


def agent_remove(level, model):
    from . import dispatcher as disp_mod
    return {"ok": disp_mod.remove_agent(level, model)}, 200


def api_store_list():
    from . import api_store
    entries = api_store.list_all()
    return {k: {"id": v.id, "provider": v.provider, "base_url": v.base_url,
        "api_key_env": v.api_key_env, "status": v.status, "notes": v.notes,
        "available": api_store.is_available(v.id), "updated_at": v.updated_at} for k, v in entries.items()}, 200


def api_store_add(api_id, provider="", base_url="", api_key_env="", notes=""):
    from . import api_store
    entry = api_store.add(api_id=api_id, provider=provider or api_id, base_url=base_url, api_key_env=api_key_env, notes=notes)
    return {"ok": True, "entry": entry.to_dict(),
            "hint": f"调用 POST /api/api-store/{api_id}/scan 发现模型"}, 200


def api_store_remove(api_id):
    from . import api_store; return {"ok": api_store.remove(api_id)}, 200


def api_store_set_status(api_id, status, notes=""):
    from . import api_store
    entry = api_store.set_status(api_id, status, notes)
    if not entry: return {"error": f"API {api_id} 不存在"}, 404
    return {"ok": True, "entry": entry.to_dict()}, 200


def api_store_scan(api_id):
    """扫描 API 厂商的模型列表（预览，不导入）。"""
    from . import api_store
    if not api_store.get(api_id):
        return {"error": f"API {api_id} 不存在"}, 404
    try:
        models = api_store.scan_models(api_id)
        return {"ok": True, "provider": api_id, "models": models, "total": len(models)}, 200
    except Exception as e:
        return {"error": f"扫描失败: {e}"}, 500


def models_import(models: list[dict], auto_assign: bool = False):
    """批量导入模型到库。"""
    from . import api_store, witness
    from . import dispatcher as disp_mod
    imported = []
    errors = []
    for m in models:
        try:
            api_store.save_custom_model(
                model_id=m["id"], provider=m.get("provider", ""),
                display=m.get("display", m["id"]),
                recommended_for=m.get("recommended_for") or m.get("tiers", []),
                speed=m.get("speed", "medium"), cost=m.get("cost", "standard"),
                rating=m.get("rating", "?"), strengths=m.get("strengths", []),
                notes=m.get("notes", ""),
            )
            imported.append(m["id"])
            if auto_assign:
                # 两档后: 直接添加, 不按层级
                try:
                    disp_mod.add_agent(model=m["id"])
                except Exception:
                    pass
        except Exception as e:
            errors.append(f"{m.get('id', '?')}: {e}")
            witness.heartbeat('_api', f'warn:{e}'[:80])
    return {"ok": True, "imported": imported, "errors": errors}, 200


def model_list():
    from . import model_registry, api_store
    from . import dispatcher as disp_mod
    models = model_registry.load_models()
    custom = disp_mod._load_custom_agents()
    disabled_by_tier = custom.get("_disabled", {})
    return {mid: {"id": m.id, "provider": m.provider, "display": m.display,
        "recommended_for": m.recommended_for, "speed": m.speed, "cost": m.cost, "rating": m.rating,
        "reasoning": m.reasoning, "max_turns": m.max_turns, "strengths": m.strengths,
        "notes": m.notes, "api_available": api_store.is_available(m.provider),
        "disabled_in": [t for t in m.recommended_for if mid in disabled_by_tier.get(t, [])]} for mid, m in models.items()}, 200


def model_list_for_tier(tier):
    from . import model_registry, api_store
    models = model_registry.for_tier(tier, available_only=False)
    return [{"id": m.id, "provider": m.provider, "display": m.display,
        "cost": m.cost, "speed": m.speed, "api_available": api_store.is_available(m.provider)} for m in models], 200


def model_add(model_id, provider="", display="", recommended_for=None, speed="medium", cost="standard", reasoning=False, max_turns=5, strengths="", notes=""):
    from . import model_registry
    # backward compat: accept old "tiers" param too
    rf = recommended_for or []
    model_registry.add_model(model_id, provider, display, rf, speed, cost, "", reasoning, max_turns, notes)
    return {"ok": True, "model_id": model_id}, 200


def model_remove(model_id):
    from . import model_registry, dispatcher
    ok = model_registry.remove_model(model_id)
    # 两档后: 从全池移除, 不按层级
    dispatcher.remove_agent("", model_id)
    return {"ok": ok, "synced": True}, 200


def model_update(model_id, data):
    from . import model_registry, dispatcher as disp_mod
    models = model_registry.load_models()
    if model_id not in models: return {"error": "模型不存在"}, 404
    m = models[model_id]
    # backward compat: read old "tiers" or new "recommended_for"
    old_rf = set(m.recommended_for or [])
    new_rf = set((data.get("recommended_for") or data.get("tiers") or m.recommended_for) or [])

    model_registry.add_model(
        model_id,
        data.get("provider", m.provider),
        data.get("display", m.display),
        list(new_rf),
        data.get("speed", m.speed),
        data.get("cost", m.cost),
        data.get("rating", getattr(m, "rating", "")),
        data.get("reasoning", m.reasoning),
        data.get("max_turns", m.max_turns),
        data.get("notes", getattr(m, "notes", "")),
    )
    return {"ok": True, "model_id": model_id,
            "updated": {"recommended_for": sorted(new_rf)}}, 200


# ═══════════════════════════════════════════════════════════════
# Skill / Permission  (ex _api_skills.py)
# ═══════════════════════════════════════════════════════════════

def skill_list():
    try:
        from singularity.skills.skill_loader import load_skills
        all_skills = load_skills()
        return {"skills": [{"name": s.name, "description": s.description, "type": s.type,
            "args": s.arguments, "source": s.source, "body": s.body[:200]} for s in all_skills.values()]}, 200
    except Exception as e: return {"error": str(e)}, 500


def skill_add(name, description="", skill_type="prompt", args=None, body=""):
    from singularity.skills.skill_loader import create_user_skill
    from . import dispatcher as disp_mod
    create_user_skill(name, description, skill_type, args or [], body)
    disp_mod.invalidate_skill_cache(); return {"ok": True, "name": name}, 200


def skill_delete(name):
    from singularity.skills.skill_loader import delete_user_skill
    from . import dispatcher as disp_mod
    ok = delete_user_skill(name); disp_mod.invalidate_skill_cache(); return {"ok": ok}, 200


def agent_skill_list(level, model):
    from singularity.skills.skill_loader import get_agent_skills, load_skills
    return {"skill_names": get_agent_skills(level, model), "available": list(load_skills().keys())}, 200


def agent_skill_update(level, model, skill_names):
    from singularity.skills.skill_loader import set_agent_skills
    from . import dispatcher as disp_mod
    set_agent_skills(level, model, skill_names); disp_mod.invalidate_skill_cache(level, model); return {"ok": True}, 200


def perm_profiles():
    from .permission import get_store; return {"profiles": get_store().list_profiles()}, 200


def perm_profiles_add(name, profile):
    from .permission import get_store; get_store().add_profile(name, profile); return {"ok": True}, 200


def perm_profiles_delete(name):
    from .permission import get_store; get_store().remove_profile(name); return {"ok": True}, 200


def perm_bind(level, model, profile):
    from .permission import get_store; get_store().bind_agent(level, model, profile); return {"ok": True}, 200


def perm_unbind(level, model):
    from .permission import get_store; get_store().unbind_agent(level, model); return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# MCP 服务器  (ex _api_mcp.py)
# ═══════════════════════════════════════════════════════════════

def mcp_server_list():
    from . import mcp as m; configs = m.load_mcp_configs(); reg = m.get_registry()
    servers = []
    for c in configs:
        connected = c.name in reg._clients
        tc = len(reg._clients[c.name]._tools) if connected else 0
        servers.append({"name": c.name, "transport": c.transport, "command": c.command,
            "url": c.url, "enabled": c.enabled, "timeout": c.timeout, "connected": connected, "tool_count": tc})
    return {"servers": servers}, 200


def mcp_server_add(data):
    from . import mcp as m
    if not data or not data.get("name"): return {"error": "缺少 name"}, 400
    configs = m.load_mcp_configs(); found = False
    for c in configs:
        if c.name == data["name"]:
            c.transport = data.get("transport", c.transport); c.command = data.get("command", c.command)
            c.url = data.get("url", c.url); c.enabled = data.get("enabled", c.enabled)
            c.timeout = data.get("timeout", c.timeout); c.env = data.get("env", c.env); found = True; break
    if not found:
        configs.append(m.MCPServerConfig(name=data["name"], transport=data.get("transport","stdio"),
            command=data.get("command",""), url=data.get("url",""), enabled=data.get("enabled",True),
            timeout=data.get("timeout",30.0), env=data.get("env",{})))
    m.save_mcp_configs(configs); return {"ok": True}, 200


def mcp_server_delete(name):
    from . import mcp as m; configs = m.load_mcp_configs()
    m.save_mcp_configs([c for c in configs if c.name != name]); return {"ok": True}, 200


def mcp_server_reconnect(name):
    from . import mcp as m; configs = m.load_mcp_configs(); reg = m.get_registry()
    for c in configs:
        if c.name == name:
            if name in reg._clients:
                reg._clients[name].disconnect(); del reg._clients[name]
                reg._tools = [t for t in reg._tools if t.server_name != name]
                reg._tool_index = {k:v for k,v in reg._tool_index.items() if v.cfg.name != name}
            reg.load_configs([c]); return {"ok": True, "tool_count": len(reg._tools)}, 200
    return {"error": f"服务器 {name} 不存在"}, 404


def mcp_tool_list():
    from . import mcp as m; reg = m.get_registry()
    return {"tools": [{"name": f"mcp__{t.server_name}__{t.name}", "server": t.server_name,
        "tool": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in reg.get_all_tools()]}, 200


def mcp_refresh():
    from . import mcp as m; configs = m.load_mcp_configs(); m.get_registry().load_configs(configs)
    return {"ok": True, "servers": m.get_registry().server_count, "tools": m.get_registry().tool_count}, 200


# ═══════════════════════════════════════════════════════════════
# 监控 / Auth / Health / 模板  (ex _api_monitor.py)
# ═══════════════════════════════════════════════════════════════

def auth_status():
    from ._auth import get_auth
    return {"enabled": os.environ.get("QIDIAN_AUTH") == "1", "users": get_auth().list_users()}, 200


def auth_bootstrap():
    from ._auth import get_auth; a = get_auth()
    if a._users: return {"ok": False, "error": "已有用户"}, 403
    admin = a.bootstrap(); return {"ok": True, "user": admin.to_dict(), "message": f"Admin token: {admin.token[:8]}..."}, 200


def auth_add_user(uid, name="", role="viewer"):
    from ._auth import get_auth; return {"ok": True, "user": get_auth().add_user(uid, name, role).to_dict()}, 200


def auth_remove_user(uid):
    from ._auth import get_auth
    return {"ok": True} if get_auth().remove_user(uid) else {"error": "用户不存在"}, 404


def status_overview():
    from . import dispatcher as disp_mod
    config.ensure_dirs()
    counts = witness._count_by_status(); loads = witness._heartbeat_task_levels()
    pw, dd = witness._timing_stats(); tt = witness._token_stats()
    stalled = witness.check_stalled(600)
    agents = {}
    try:
        for level, cfgs in disp_mod.load_agents().items():
            agents[level] = [{"model": c.get("model",""), "roles": c.get("roles",[])} for c in cfgs]
    except Exception as e: witness.heartbeat('_api', f'warn:{e}')
    return {"counts": counts, "heartbeat_levels": loads, "running_total": sum(loads.values()),
        "avg_wait": f"{sum(pw)/len(pw):.1f}s" if pw else "--",
        "avg_done": f"{sum(dd)/len(dd):.1f}s" if dd else "--",
        "token_totals": tt, "stalled": stalled, "agents": agents}, 200


def cleanup():
    n_hb, n_tasks = witness.force_cleanup_heartbeats()
    n_wt = _cleanup_orphan_worktrees()
    from . import snapshot as snap_mod; n_snap = snap_mod.purge_old_snapshot_meta()
    return {"ok": True, "cleaned": {"heartbeats": n_hb, "worktrees": n_wt, "snapshots": n_snap, "tasks": n_tasks}}, 200


def _cleanup_orphan_worktrees() -> int:
    """删除任务已不存在但 worktree 目录残留的孤儿。"""
    wt_dir = config.QIDIAN_DIR / "worktrees"
    if not wt_dir.exists():
        return 0
    n = 0
    for d in sorted(wt_dir.iterdir()):
        if not d.is_dir():
            continue
        tid = d.name.split("_")[0] if "_" in d.name else d.name
        if not (tracker.tasks_dir() / f"{tid}.json").exists():
            try:
                subprocess.run(["git", "worktree", "remove", "--force", str(d)],
                             cwd=str(config.PROJECT_ROOT), capture_output=True, timeout=10)
            except Exception:
                shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def token_usage():
    from ._token_budget import get_usage_stats; return get_usage_stats(), 200


def token_budget_set(budget):
    from ._token_budget import set_budget; set_budget(budget); return {"ok": True, "budget": budget}, 200


def perf_stats():
    from ._profiler import get_perf_stats; return get_perf_stats(), 200


def dag_metrics():
    return tracker.dag_metrics(), 200


def model_profile_status():
    from .model_profile import ProfileStore
    ps = ProfileStore(config.QIDIAN_DIR / "model_profile.json"); ps.load(); return {"profiles": ps.summary()}, 200


def model_profile_pattern():
    from .model_profile import ProfileStore
    ps = ProfileStore(config.QIDIAN_DIR / "model_profile.json"); ps.load(); return {"patterns": ps.pattern_summary()}, 200


def reports_critical():
    from . import chancellor as chan_mod; return chan_mod.recent_critical(), 200


def reports_list():
    from . import chancellor as chan_mod; return {"reports": chan_mod.list_reports(limit=30)}, 200


def template_list():
    from .task_templates import list_all
    templates = list_all()
    return {"templates": [{"id": tid, "name": t.name, "description": t.description,
        "success_criteria": t.success_criteria, "suggested_max_turns": t.suggested_max_turns,
        "recommended_models": t.recommended_models} for tid, t in templates.items()]}, 200


def health_check(loop_running, sse_clients):
    disk = shutil.disk_usage(str(config.QIDIAN_DIR))
    return {"status": "ok", "disk_free_mb": disk.free // (1024*1024),
        "loop_running": loop_running, "sse_clients": sse_clients, "projects": -1}, 200  # ponytail: caller (app.py) overwrites with real count


# ═══════════════════════════════════════════════════════════════
# Memory + Conflict (小模块，不拆)
# ═══════════════════════════════════════════════════════════════

def memory_query(query_text="", files_str="", beam_width=3, max_hops=3, max_depth=1):
    from . import memory as mem_mod
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    return mem_mod.query(query_text, files=files, beam_width=beam_width, max_hops=max_hops, max_depth=max_depth), 200


def memory_chain(task_id):
    from . import memory as mem_mod
    chain = mem_mod.get_task_chain(task_id)
    return ({"error": "无记忆链"}, 404) if chain is None else (chain, 200)


def memory_rebuild():
    from singularity.scheduler.memory import consolidate_memory
    return {"ok": True, "consolidated": consolidate_memory()}, 200


def conflict_list():
    all_tasks = _list_all_tasks()
    conflicts = [t for t in all_tasks if t.get("status") == TaskStatus.CONFLICT_HELD.value]
    return {"conflicts": [{"id": t.get("id", t["_filename"]), "description": (t.get("description","") or "")[:120],
        "error": (t.get("error","") or "")[:300], "created_at": t.get("created_at",0),
        "route_level": t.get("route_level","")} for t in conflicts], "total": len(conflicts)}, 200


def conflict_resolve(task_id, resolution, push_event=None):
    from .merge import resolve_conflict
    result = resolve_conflict(task_id, resolution)
    if push_event: push_event("system", f"[{task_id[:8]}] conflict resolved")
    return {"ok": True, "result": result}, 200
