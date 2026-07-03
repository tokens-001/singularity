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
