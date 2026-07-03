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
        except Exception as _e:
            logging.getLogger(__name__).warning("task file read failed: %s", _e)
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


