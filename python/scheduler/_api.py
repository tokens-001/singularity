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
        except Exception as e:
            witness.heartbeat('_api', f'warn:{e}')
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

# ponytail: task handlers → _api_tasks.py, 此处 re-export 保持兼容
from ._api_tasks import (
    task_list, task_detail, task_trace, task_timeline,
    task_hold, task_release, task_override_route,
    task_cancel, task_delete, task_retry,
    task_approval, task_apply, task_rollback, task_supervise,
    task_submit,
)

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

# ponytail: project handlers → _api_projects.py, 此处 re-export 保持兼容
from ._api_projects import (
    project_list, project_create, project_detail,
    project_gate_confirm, project_run_phase, project_start,
    project_cost, project_lineage, project_snapshot,
    project_auto, project_autopilot_start, project_autopilot_stop,
    project_lineup_get, project_lineup_set,
)

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
    except Exception as e:
        witness.heartbeat('_api', f'warn:{e}')
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
    except Exception as e:
        witness.heartbeat('_api', f'warn:{e}')
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
