"""dispatcher.py — 读 agents.toml 选 executor 并调用。

v2: 集成 api_store + model_registry，API 欠费/限流时自动跳过对应 agent。
project_lineup 支持项目级自定义编组。
"""

from __future__ import annotations
import time
from dataclasses import dataclass

from . import config
from .executors import (
    BaseExecutor, ExecutorResult,
    ClaudeCliExecutor, ZhipuApiExecutor, OpenAIAgentExecutor,
)

_ESCALATION = {"E": "E+", "E+": "D"}
_EXECUTOR_BY_TYPE = {
    "claude-cli": ClaudeCliExecutor,
    "zhipu-api": ZhipuApiExecutor,
    "openai-agent": OpenAIAgentExecutor,
}


@dataclass
class DispatchResult:
    level: str
    agent_cfg: dict
    executor_result: ExecutorResult
    attempts: int


def load_agents() -> dict:
    """加载 agent 配置: agents.toml (基础) + agents_custom.json (覆盖)。

    custom 文件中可包含:
      - 每层追加的 agent 配置 (add)
      - _disabled: [model_name, ...] — 从 toml 中禁用的模型
    """
    import tomllib
    with open(config.AGENTS_TOML, "rb") as f:
        data = tomllib.load(f)
    raw = data.get("agents", {})
    agents = {}
    for k, v in raw.items():
        key = "E+" if k == "E_plus" else k
        agents[key] = list(v)  # shallow copy

    # 合并自定义覆盖
    custom = _load_custom_agents()
    for k, cfgs in custom.items():
        if k.startswith("_"):
            continue
        level = "E+" if k == "E_plus" else k
        if level not in agents:
            agents[level] = []
        for c in cfgs:
            if isinstance(c, dict) and c.get("model"):
                # 避免重复
                existing = [a.get("model") for a in agents[level]]
                if c["model"] not in existing:
                    agents[level].append(c)

    # 应用禁用列表
    for level in agents:
        disabled = custom.get("_disabled", {}).get(level, [])
        if disabled:
            agents[level] = [a for a in agents[level] if a.get("model") not in disabled]

    return agents


def agent_api_available(agent_cfg: dict) -> bool:
    """检查 agent 的 API 是否可用。

    所有类型都经过 model_registry → api_store 检查。
    claude-cli 也检查 api_store 状态。
    """
    model = agent_cfg.get("model", "")
    if model:
        try:
            from . import model_registry as mr
            provider = mr.provider_for_model(model)
            if provider:
                from . import api_store
                if not api_store.is_available(provider):
                    return False
        except Exception as e:
            from . import witness; witness.heartbeat("dispatch", "warn", status="error", detail=f"api_check:{e}")

    # claude-cli: api_store 通过了就算通过
    etype = agent_cfg.get("type", "")
    if etype == "claude-cli":
        return True

    env_key = agent_cfg.get("api_key_env", "")
    if env_key:
        import os
        return bool(os.environ.get(env_key, ""))
    return True


def _build_agent_from_registry(model_name: str) -> dict | None:
    """模型不在 agents.toml 时，从 model_registry + api_store 自动构造配置。"""
    try:
        from . import model_registry as mr
        from . import api_store
        m = mr.get(model_name)
        if not m:
            return None
        apis = api_store.list_all()
        api = apis.get(m.provider, {}) if hasattr(apis, 'get') else {}
        return {
            "model": model_name,
            "type": "openai-agent",
            "entry": getattr(api, "base_url", "") + "/chat/completions" if hasattr(api, "base_url") else "",
            "api_key_env": getattr(api, "api_key_env", ""),
            "max_turns": m.max_turns,
            "default": False,
            "roles": ["daily"],
            "sandbox": "worktree",
            "request_template": {"model": model_name, "max_tokens": 4096},
        }
    except Exception as e:
        from . import witness; witness.heartbeat("dispatch", "warn", status="error", detail=f"build_agent:{e}")
        return None


def _find_agent_by_model(agents: dict, model_name: str) -> dict | None:
    """跨所有层搜索 agent 配置。"""
    for level_cfgs in agents.values():
        for a in level_cfgs:
            if a.get("model") == model_name:
                return a
    return None


def pick_agent(agents: dict, level: str, role: str = None,
               project_lineup: dict[str, list[str]] = None) -> dict:
    """选 agent: project_lineup > role > default。

    API 不可用的 agent 自动跳过。
    project_lineup 里的模型找不到时跨层搜索。
    """
    candidates = agents.get(level, [])
    if not candidates:
        raise RuntimeError(f"无 {level} 层 agent")

    # project_lineup 优先
    lineup = (project_lineup or {}).get(level, [])
    if lineup:
        for model_name in lineup:
            # 先在本层找
            for a in candidates:
                if a.get("model") == model_name and agent_api_available(a):
                    return a
            # 跨层找 (如 D 层 lineup 里配 glm-5.2，它在 E+ 配置里)
            cross = _find_agent_by_model(agents, model_name)
            if cross and agent_api_available(cross):
                return cross
            # 不在 agents.toml 中，从 model_registry 自动构造
            built = _build_agent_from_registry(model_name)
            if built and agent_api_available(built):
                return built

    # role 匹配
    if role:
        for a in candidates:
            if role in (a.get("roles") or []) and agent_api_available(a):
                return a

    # default
    for a in candidates:
        if a.get("default") and agent_api_available(a):
            return a

    # 第一个可用的
    for a in candidates:
        if agent_api_available(a):
            return a

    raise RuntimeError(f"{level} 层所有 agent 的 API 均不可用")


def pick_agent_fallback_chain(agents: dict, level: str, role: str = None,
                               exclude: set = None,
                               project_lineup: dict[str, list[str]] = None) -> list[dict]:
    """返回该层可用 agent 列表。project_lineup > role > default > 其他。

    API 不可用的自动跳过。
    """
    candidates = agents.get(level, [])
    if not candidates:
        return []
    exclude = exclude or set()
    result = []
    seen = set()

    lineup = (project_lineup or {}).get(level, [])
    if lineup:
        for model_name in lineup:
            found = None
            for a in candidates:
                key = a.get("model", "")
                if key == model_name and key not in seen and key not in exclude:
                    if agent_api_available(a):
                        found = a
            # 跨层找
            if not found:
                cross = _find_agent_by_model(agents, model_name)
                if cross and agent_api_available(cross):
                    found = cross
            if found:
                key = found.get("model", "")
                result.append(found)
                seen.add(key)

    if role:
        for a in candidates:
            key = a.get("model", "")
            if role in (a.get("roles") or []) and key not in seen and key not in exclude:
                if agent_api_available(a):
                    result.append(a)
                    seen.add(key)

    for a in candidates:
        key = a.get("model", "")
        if a.get("default") and key not in seen and key not in exclude:
            if agent_api_available(a):
                result.append(a)
                seen.add(key)

    for a in candidates:
        key = a.get("model", "")
        if key not in seen and key not in exclude:
            if agent_api_available(a):
                result.append(a)
                seen.add(key)

    return result


def dispatch(
    task: str,
    level: str,
    task_id: str,
    agents: dict,
    feedback: str = "",
    baseline_ref: str = "",
    cwd: str = "",
    project_lineup: dict[str, list[str]] = None,
) -> DispatchResult:
    """选 executor 跑一次。失败由调用方决定升级或打回。"""
    agent_cfg = pick_agent(agents, level, project_lineup=project_lineup)
    etype = agent_cfg.get("type", "claude-cli")
    executor_cls = _EXECUTOR_BY_TYPE.get(etype)
    if not executor_cls:
        raise RuntimeError(f"未知 executor type: {etype}")

    full_task = task
    if feedback:
        full_task = (
            f"{task}\n\n"
            f"---\n[上一轮校验反馈, 请据此修正]\n{feedback}"
        )

    executor: BaseExecutor = executor_cls(
        agent_cfg, full_task, task_id, baseline_ref=baseline_ref, cwd=cwd,
    )
    result = executor.run()

    return DispatchResult(
        level=level,
        agent_cfg=agent_cfg,
        executor_result=result,
        attempts=1,
    )


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

def add_agent(level: str, model: str, agent_type: str = "openai-agent",
              entry: str = "", api_key_env: str = "", max_turns: int = 5,
              roles: list = None, sandbox: str = "worktree", mode: str = "",
              request_template: dict = None) -> dict:
    custom = _load_custom_agents()
    key = "E_plus" if level == "E+" else level

    # 1. 如果之前在禁用列表里，移除禁用标记即可 (重新启用 toml 内置 agent)
    disabled = custom.get("_disabled", {}).get(level, [])
    if model in disabled:
        disabled.remove(model)
        custom.setdefault("_disabled", {})[level] = disabled
        _save_custom_agents(custom)
        # 从 toml 找到原始配置返回
        agents = load_agents()
        for a in agents.get(level, []):
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
    return cfg

def remove_agent(level: str, model: str) -> bool:
    """禁用 agent: 加入 _disabled 列表。支持 toml 内置和 custom 两种来源。"""
    custom = _load_custom_agents()
    key = "E_plus" if level == "E+" else level

    # 1. 如果是 custom 里的，直接删
    cfgs = custom.get(key, [])
    new_cfgs = [a for a in cfgs if a.get("model") != model]
    if len(new_cfgs) != len(cfgs):
        custom[key] = new_cfgs
        _save_custom_agents(custom)
        return True

    # 2. 如果是 toml 内置的，加入禁用列表
    custom.setdefault("_disabled", {})
    custom["_disabled"].setdefault(level, [])
    if model not in custom["_disabled"][level]:
        custom["_disabled"][level].append(model)
        _save_custom_agents(custom)
        return True
    return False

def update_agent(level: str, model: str, updates: dict) -> dict:
    # 先在自定义 overlay 里找，再在 TOML 内置里找
    custom = _load_custom_agents()
    key = "E_plus" if level == "E+" else level
    cfgs = custom.get(key, [])
    for a in cfgs:
        if a.get("model") == model:
            a.update(updates)
            _save_custom_agents(custom)
            return a
    # 不在自定义里，从内置 TOML 复制一份到 overlay 再更新
    agents = load_agents()
    tier_agents = agents.get(level, [])
    for a in tier_agents:
        if a.get("model") == model:
            new_cfg = dict(a)
            new_cfg.update(updates)
            custom.setdefault(key, []).append(new_cfg)
            _save_custom_agents(custom)
            return new_cfg
    raise RuntimeError(f"Agent {model} 不在 {level} 层")


def escalate(level: str) -> str | None:
    return _ESCALATION.get(level)
