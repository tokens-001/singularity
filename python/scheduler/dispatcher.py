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
    import tomllib
    with open(config.AGENTS_TOML, "rb") as f:
        data = tomllib.load(f)
    raw = data.get("agents", {})
    agents = {}
    for k, v in raw.items():
        key = "E+" if k == "E_plus" else k
        agents[key] = v
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
        except Exception:
            pass

    # claude-cli: api_store 通过了就算通过
    etype = agent_cfg.get("type", "")
    if etype == "claude-cli":
        return True

    env_key = agent_cfg.get("api_key_env", "")
    if env_key:
        import os
        return bool(os.environ.get(env_key, ""))
    return True


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


def escalate(level: str) -> str | None:
    return _ESCALATION.get(level)
