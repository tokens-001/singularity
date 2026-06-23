"""_api.py — API handler 层 (薄 facade, 业务逻辑已下沉到子模块)。

子模块分工:
  _api_tasks.py    — 任务 CRUD
  _api_projects.py — 项目 CRUD + workflow
  _api_agents.py   — Agent / Model / API Store
  _api_skills.py   — Skill / Permission
  _api_mcp.py      — MCP 服务器
  _api_monitor.py  — 监控 / Auth / Health / 模板
"""
from __future__ import annotations
import json
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
    tasks_dir = tracker._tasks_dir()
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
# 子模块 re-export
# ═══════════════════════════════════════════════════════════════

from singularity.scheduler._api_tasks import (task_list, task_detail, task_trace, task_timeline, task_hold, task_release,
    task_override_route, task_cancel, task_delete, task_retry, task_approval, task_apply,
    task_rollback, task_supervise, task_submit)

from singularity.scheduler._api_projects import (project_list, project_create, project_detail, project_gate_confirm,
    project_run_phase, project_start, project_cost, project_lineage, project_snapshot,
    project_auto, project_autopilot_start, project_autopilot_stop, project_lineup_get, project_lineup_set)

from singularity.scheduler._api_agents import (agent_list, agent_add, agent_update, agent_remove,
    api_store_list, api_store_add, api_store_remove, api_store_set_status, api_store_scan,
    model_list, model_list_for_tier, model_add, model_remove, model_update, models_import)

from singularity.scheduler._api_skills import (skill_list, skill_add, skill_delete, agent_skill_list, agent_skill_update,
    perm_profiles, perm_profiles_add, perm_profiles_delete, perm_bind, perm_unbind)

from singularity.scheduler._api_mcp import (mcp_server_list, mcp_server_add, mcp_server_delete, mcp_server_reconnect,
    mcp_tool_list, mcp_refresh)

from singularity.scheduler._api_monitor import (auth_status, auth_bootstrap, auth_add_user, auth_remove_user,
    status_overview, cleanup, token_usage, token_budget_set, perf_stats, dag_metrics,
    judge_monitor_status, model_profile_status, model_profile_pattern, reports_critical,
    reports_list, template_list, health_check)

# ═══════════════════════════════════════════════════════════════
# Memory (小, 不值得拆)
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
    return {"ok": True, "consolidated": orchestrator.consolidate_memory()}, 200

# ═══════════════════════════════════════════════════════════════
# Conflict (小, 不值得拆)
# ═══════════════════════════════════════════════════════════════

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
