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
