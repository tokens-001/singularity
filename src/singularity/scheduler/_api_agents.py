"""_api_agents.py — Agent / Model / API Store handlers."""
from __future__ import annotations
from singularity.scheduler import dispatcher as disp_mod

# ── token 估算常量 ──
_TOKEN_PER_CHAR = 0.6          # 估算: 1 英文字符 ≈ 0.25 token, 中文 ≈ 1.5, 平均 0.6
_COST_PER_M_TOKEN = {           # $/M tokens (input), 按模型 tier
    "E": 0.15,                  # deepseek-chat 级别
    "E+": 0.50,                 # glm/sonnet 级别
    "D": 1.50,                  # opus 级别 (多模型 committee)
}
_TASK_OVERHEAD_TOKENS = {       # 系统提示+工具+模板 固定开销
    "E": 2000,
    "E+": 3000,
    "D": 8000,                  # D 层 committee 开销大
}

def agent_list() -> tuple[dict, int]:
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
    cfg = disp_mod.add_agent(level=level, model=model, agent_type=agent_type, entry=entry_url,
        api_key_env=api_key_env, max_turns=max_turns, roles=roles or [], sandbox=sandbox, mode=mode, request_template=request_template)
    return {"ok": True, "agent": cfg}, 200

def agent_update(level, model, data):
    return {"ok": True, "agent": disp_mod.update_agent(level, model, data)}, 200

def agent_remove(level, model):
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
    """批量导入模型到库。models: [{id, provider, display, tiers, speed, cost}]

    如果 auto_assign=True，同时为每个模型在对应 tier 创建 agent 条目。
    """
    from . import api_store, witness
    imported = []
    errors = []
    for m in models:
        try:
            api_store.save_custom_model(
                model_id=m["id"], provider=m.get("provider", ""),
                display=m.get("display", m["id"]),
                tiers=m.get("tiers", ["E"]),
                speed=m.get("speed", "medium"), cost=m.get("cost", "standard"),
                rating=m.get("rating", "?"), strengths=m.get("strengths", []),
                notes=m.get("notes", ""),
            )
            imported.append(m["id"])
            # 自动分配到架构层
            if auto_assign:
                for tier in m.get("tiers", []):
                    try:
                        disp_mod.add_agent(level=tier, model=m["id"])
                    except Exception:
                        pass  # 可能已存在
        except Exception as e:
            errors.append(f"{m.get('id', '?')}: {e}")
            witness.heartbeat('_api', f'warn:{e}'[:80])
    return {"ok": True, "imported": imported, "errors": errors}, 200

def model_list():
    from . import model_registry, api_store
    models = model_registry.load_models()
    custom = disp_mod._load_custom_agents()
    disabled_by_tier = custom.get("_disabled", {})
    return {mid: {"id": m.id, "provider": m.provider, "display": m.display,
        "tiers": m.tiers, "speed": m.speed, "cost": m.cost, "rating": m.rating,
        "reasoning": m.reasoning, "max_turns": m.max_turns, "strengths": m.strengths,
        "notes": m.notes, "api_available": api_store.is_available(m.provider),
        "disabled_in": [t for t in m.tiers if mid in disabled_by_tier.get(t, [])]} for mid, m in models.items()}, 200

def model_list_for_tier(tier):
    from . import model_registry, api_store
    models = model_registry.for_tier(tier, available_only=False)
    return [{"id": m.id, "provider": m.provider, "display": m.display,
        "cost": m.cost, "speed": m.speed, "api_available": api_store.is_available(m.provider)} for m in models], 200

def model_add(model_id, provider="", display="", tiers=None, speed="medium", cost="standard", reasoning=False, max_turns=5, strengths="", notes=""):
    from . import model_registry
    model_registry.add_model(model_id, provider, display, tiers or ["E"], speed, cost, reasoning, max_turns)
    return {"ok": True, "model_id": model_id}, 200

def model_remove(model_id):
    from . import model_registry, dispatcher
    ok = model_registry.remove_model(model_id)
    # 同步: 从所有层禁掉对应 agent
    agents = dispatcher.load_agents()
    disabled_levels = []
    for lvl in ("E", "E+", "D"):
        for a in agents.get(lvl, []):
            if a.get("model") == model_id:
                dispatcher.remove_agent(lvl, model_id)
                disabled_levels.append(lvl)
                break
    return {"ok": ok, "synced_agents": disabled_levels}, 200

def model_update(model_id, data):
    from . import model_registry
    models = model_registry.load_models()
    if model_id not in models: return {"error": "模型不存在"}, 404
    m = models[model_id]
    model_registry.add_model(model_id, data.get("provider", m.provider), data.get("display", m.display),
        data.get("tiers", m.tiers), data.get("speed", m.speed), data.get("cost", m.cost),
        data.get("reasoning", m.reasoning), data.get("max_turns", m.max_turns))
    return {"ok": True, "model_id": model_id}, 200
