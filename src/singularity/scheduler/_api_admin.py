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
                tiers=m.get("recommended_for") or m.get("tiers", []),
                speed=m.get("speed", "medium"), cost=m.get("cost", "standard"),
                rating=m.get("rating", "?"), strengths=m.get("strengths", []),
                notes=m.get("notes", ""),
            )
            imported.append(m["id"])
            if auto_assign:
                # 两档后: 直接添加, 不按层级
                try:
                    disp_mod.add_agent(model=m["id"])
                except Exception as _e:
                    logging.getLogger(__name__).warning("agent add during import failed: %s", _e)
        except Exception as e:
            errors.append(f"{m.get('id', '?')}: {e}")
            witness.heartbeat('_api', f'warn:{e}'[:80])
    return {"ok": True, "imported": imported, "errors": errors}, 200


def model_list():
    from . import model_registry, api_store
    from . import dispatcher as disp_mod
    # 前端模型目录只显示扫描导入的模型(models_custom); 内置能力快照(models.toml)只供调度/扫描填能力, 不出现在目录
    custom_ids = set(model_registry._load_custom().keys())
    models = {mid: m for mid, m in model_registry.load_models().items() if mid in custom_ids}
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
        "token_totals": tt, "stalled": stalled, "agents": agents,
        "workdir": str(config.PROJECT_ROOT)}, 200


def cleanup():
    n_hb, n_tasks = witness.force_cleanup_heartbeats()
    n_wt = _cleanup_orphan_worktrees()
    from . import snapshot as snap_mod; n_snap = snap_mod.purge_old_snapshot_meta()
    return {"ok": True, "cleaned": {"heartbeats": n_hb, "worktrees": n_wt, "snapshots": n_snap, "tasks": n_tasks}}, 200


