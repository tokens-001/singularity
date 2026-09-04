from singularity.scheduler.dispatcher import load_agents, _ESCALATION

__all__ = ['_custom_agents_path', '_load_custom_agents', '_notify_agent_change', '_save_custom_agents', 'add_agent', 'escalate', 'remove_agent', 'update_agent']
# ── Agent CRUD (写入自定义 JSON overlay) ──

import json as _json

def _custom_agents_path():
    from . import config
    return config.QIDIAN_DIR / "agents_custom.json"

def _load_custom_agents() -> dict:
    p = _custom_agents_path()
    if p.exists():
        try:
            return _json.loads(p.read_text())
        except (_json.JSONDecodeError, OSError):
            pass
    return {}

def _save_custom_agents(data: dict) -> None:
    from . import config
    config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    _custom_agents_path().write_text(_json.dumps(data, ensure_ascii=False, indent=2))

def add_agent(level: str = "", model: str = "", agent_type: str = "openai-agent",
              entry: str = "", api_key_env: str = "", max_turns: int = 5,
              roles: list = None, sandbox: str = "worktree", mode: str = "",
              request_template: dict = None) -> dict:
    # 两档后 level 可选, 空=全池
    key = level or "any"
    custom = _load_custom_agents()

    # 1. 如果之前在禁用列表里，移除禁用标记即可 (重新启用 toml 内置 agent)
    disabled = custom.get("_disabled", {}).get(key, [])
    if model in disabled:
        disabled.remove(model)
        custom.setdefault("_disabled", {})[key] = disabled
        _save_custom_agents(custom)
        _notify_agent_change()
        # 从 toml 找到原始配置返回
        agents = load_agents()
        for a in agents.get(key, []):
            if a.get("model") == model:
                return a

    # 2. 新增自定义 agent
    if key not in custom:
        custom[key] = []
    # 避免重复
    if any(a.get("model") == model for a in custom[key]):
        return next(a for a in custom[key] if a.get("model") == model)
    cfg = {
        "model": model, "type": agent_type,
        "entry": entry, "api_key_env": api_key_env,
        "max_turns": max_turns, "default": False,
        "roles": roles or ["daily"], "sandbox": sandbox,
    }
    if mode:
        cfg["mode"] = mode
    if request_template:
        cfg["request_template"] = request_template
    custom[key].append(cfg)
    _save_custom_agents(custom)
    _notify_agent_change()
    return cfg

def remove_agent(level: str = "", model: str = "") -> bool:
    """禁用 agent: 从 custom 删 + 加入 _disabled。两档后 level 可选。"""
    key = level or "any"
    disabled_key = level or "any"
    custom = _load_custom_agents()

    # 1. 从 custom 列表删除
    cfgs = custom.get(key, [])
    new_cfgs = [a for a in cfgs if a.get("model") != model]
    custom[key] = new_cfgs

    # 2. 加入禁用列表 (幂等, 无论来源是 toml 还是 custom)
    custom.setdefault("_disabled", {})
    custom["_disabled"].setdefault(disabled_key, [])
    if model not in custom["_disabled"][disabled_key]:
        custom["_disabled"][disabled_key].append(model)

    _save_custom_agents(custom)
    _notify_agent_change()
    return True


def _notify_agent_change():
    """推送 agent 变更事件到待刷新队列。loop 运行时会广播; loop 未运行时由 API handler 直接推。"""
    try:
        from singularity.scheduler._types import _pending_sse_events
        import time
        _pending_sse_events.append({"kind": "agent_change", "msg": "agent config updated", "ts": time.time()})
    except Exception:
        pass


def update_agent(level: str, model: str, updates: dict) -> dict:
    custom = _load_custom_agents()
    key = level  # 两档后不再映射 E+ → E_plus

    # 处理 disabled: 加到 _disabled 列表或从中移除
    if "disabled" in updates:
        disabled = updates.pop("disabled")
        dis_map = custom.setdefault("_disabled", {})
        dis_list = dis_map.setdefault(level, [])
        if disabled and model not in dis_list:
            dis_list.append(model)
        elif not disabled and model in dis_list:
            dis_list.remove(model)

    # 处理 default: 清除同层其他 agent 的 default
    if updates.get("default"):
        cfgs = custom.get(key, [])
        for a in cfgs:
            if a.get("default") and a.get("model") != model:
                a["default"] = False
        # 也清除 TOML 内置的 default（需要在 custom 里覆盖）
        agents = load_agents()
        for a in agents.get(level, []):
            if a.get("default") and a.get("model") != model and a.get("model") not in [c.get("model") for c in cfgs]:
                new_cfg = dict(a)
                new_cfg["default"] = False
                custom.setdefault(key, []).append(new_cfg)

    # 更新 agent 配置
    cfgs = custom.get(key, [])
    for a in cfgs:
        if a.get("model") == model:
            a.update(updates)
            _save_custom_agents(custom)
            return a
    # 不在自定义里，从内置 TOML 复制一份
    agents_all = load_agents()
    for a in agents_all.get(level, []):
        if a.get("model") == model:
            new_cfg = dict(a)
            new_cfg.update(updates)
            custom.setdefault(key, []).append(new_cfg)
            _save_custom_agents(custom)
            return new_cfg
    raise RuntimeError(f"Agent {model} 不在 {level} 层")


def escalate(level: str) -> str | None:
    # 两档后: 空 level 不升级 (已从全池选)
    if not level:
        return None
    return _ESCALATION.get(level)

