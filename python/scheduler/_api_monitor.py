"""_api_monitor.py — Monitor / Auth / Health / Templates handlers."""
from __future__ import annotations
import os
from . import config, tracker, witness

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
    try: witness.cleanup()
    except AttributeError: pass
    return {"ok": True, "cleaned": {"heartbeats": 0, "tasks": 0}}, 200

def token_usage():
    from ._token_budget import get_usage_stats; return get_usage_stats(), 200

def token_budget_set(budget):
    from ._token_budget import set_budget; set_budget(budget); return {"ok": True, "budget": budget}, 200

def perf_stats():
    from ._profiler import get_perf_stats; return get_perf_stats(), 200

def dag_metrics():
    return tracker.dag_metrics(), 200

def judge_monitor_status():
    from .judge_monitor import JudgeMonitorStore
    jm = JudgeMonitorStore(config.QIDIAN_DIR / "judge_monitor.json"); jm.load(); return jm.get_stats(), 200

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
    import shutil
    disk = shutil.disk_usage(str(config.QIDIAN_DIR))
    return {"status": "ok", "disk_free_mb": disk.free // (1024*1024),
        "loop_running": loop_running, "sse_clients": sse_clients, "projects": len({})}, 200
