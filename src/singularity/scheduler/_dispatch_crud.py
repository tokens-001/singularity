from singularity.scheduler.dispatcher import load_agents, _ESCALATION

__all__ = ['_custom_agents_path', '_load_custom_agents', '_notify_agent_change', '_save_custom_agents', 'add_agent', 'escalate', 'remove_agent', 'purge_disabled', 'update_agent']
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
              sandbox: str = "worktree", mode: str = "",
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
    # 不再写 "roles" 字段：它是"按角色挑模型"那条链路的输入，而那条链路已删
    # （全仓无人读它，见 pick_agent_fallback_chain 的注释）。老数据里留着这个键无害。
    cfg = {
        "model": model, "type": agent_type,
        "entry": entry, "api_key_env": api_key_env,
        "max_turns": max_turns, "default": False,
        "sandbox": sandbox,
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
    """禁用 agent: 从 custom 删（+ 需要时加入 _disabled）。两档后 level 可选。

    `_disabled` 是给**模型库里真有的**模型留的标记 —— 它记录"这个模型被有意停用了"，
    前端靠它把模型显示在「已禁用」区（点一下能回来）。

    库里没有的名字**不该记**：从阵容删掉就已经彻底调不动了（`agents.toml` 是空占位，
    名单只剩 custom 这一份来源），留个标记既挡不住任何东西，又会在前端冒出一个
    查无此物的名字 —— 就是之前修过一轮的那种幽灵。触发路径很具体：用户启用了
    一个模型库里没有的"空壳 agent"（后端会回 warning 提醒），再点移除。
    """
    key = level or "any"
    disabled_key = level or "any"
    custom = _load_custom_agents()

    # 1. 从 custom 列表删除
    cfgs = custom.get(key, [])
    new_cfgs = [a for a in cfgs if a.get("model") != model]
    custom[key] = new_cfgs

    # 2. 加入禁用列表 (幂等) —— 但只记模型库里真有的
    if _in_model_library(model):
        custom.setdefault("_disabled", {})
        custom["_disabled"].setdefault(disabled_key, [])
        if model not in custom["_disabled"][disabled_key]:
            custom["_disabled"][disabled_key].append(model)

    _save_custom_agents(custom)
    _notify_agent_change()
    return True


def _in_model_library(model: str) -> bool:
    """模型库里有没有这个模型。查不了时**当作有** —— 宁可多留一个标记，
    也不要因为注册表读挂了就把用户"我停用过它"这件事丢掉。"""
    try:
        from . import model_registry
        return model_registry.get(model) is not None
    except Exception:
        return True


def purge_disabled(model: str) -> bool:
    """把 model 从**所有**层的 _disabled 列表里摘掉，返回是否真的摘到了。

    专给「模型已从模型库删除」这条路径用：`remove_agent` 的语义是**停用**，
    它会主动把 model 写进 _disabled。但模型都不在库了，这个标记没有任何意义 ——
    留着就成了前端「已禁用」区里一个模型库里根本不存在的名字，点它还会
    得到一个"空壳 agent"的警告（后端 add_agent 的 warning，见 _api_admin.py）。
    """
    custom = _load_custom_agents()
    disabled = custom.get("_disabled")
    if not isinstance(disabled, dict):
        return False
    hit = False
    for lst in disabled.values():
        if isinstance(lst, list) and model in lst:
            lst.remove(model)
            hit = True
    if hit:
        _save_custom_agents(custom)
        _notify_agent_change()
    return hit


def _notify_agent_change():
    """推送 agent 变更事件到待刷新队列。loop 运行时会广播; loop 未运行时由 API handler 直接推。"""
    try:
        from singularity.scheduler._types import _pending_sse_events
        import time
        _pending_sse_events.append({"kind": "agent_change", "msg": "agent config updated", "ts": time.time()})
    except Exception:
        pass


def _merge_tmpl(base: dict, updates: dict) -> dict:
    """request_template 走局部更新而非整份替换。

    整份替换会逼用户重抄 model/max_tokens，而漏写 model 更会连带出事：运行时
    dispatcher 是 `setdefault("request_template", ...)`，key 已存在就不补默认值
    → body 里没有 model，请求直接废掉。所以这里 merge 进已有 tmpl。

    底座优先取已有 tmpl；agent 本来没配过（出厂状态就是这样）则补运行时默认，
    否则只写 thinking 会得到一个连 model 都没有的 template。
    """
    tmpl = updates.get("request_template")
    if isinstance(tmpl, dict):
        from . import config
        floor = base.get("request_template") or {
            "model": base.get("model", ""), "max_tokens": config.MODEL_MAX_TOKENS}
        # None = 删掉这个键。merge 语义下没有删除能力，前端「恢复默认」就回不去了。
        merged = {**floor, **tmpl}
        return {**updates, "request_template": {k: v for k, v in merged.items() if v is not None}}
    return updates


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
            a.update(_merge_tmpl(a, updates))
            _save_custom_agents(custom)
            return a
    # 不在自定义里，从内置 TOML 复制一份
    agents_all = load_agents()
    for a in agents_all.get(level, []):
        if a.get("model") == model:
            new_cfg = dict(a)
            new_cfg.update(_merge_tmpl(a, updates))
            custom.setdefault(key, []).append(new_cfg)
            _save_custom_agents(custom)
            return new_cfg
    raise RuntimeError(f"Agent {model} 不在 {level} 层")


def escalate(level: str) -> str | None:
    """升到下一档。**当前恒返回 None** —— 这是设计事实，不是坏了。

    两档制已合并成单档（"any"，全池选人），所以**根本没有"下一档"**。
    `_ESCALATION` 也是空表、全仓无人填。调用方 `_exec.py` 依赖这个 None 收尾
    （终态 `no_escalation_path`，已改成如实报而不是假装"升级用尽"）。

    留着这个口子是为了将来重加档位时不用动调用方 —— 往 `_ESCALATION` 填表即可。
    **别指望它现在会返回非 None。**
    """
    if not level:
        return None
    return _ESCALATION.get(level)

