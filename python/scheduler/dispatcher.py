"""dispatcher.py — 读 agents.yaml 选 executor 并调用

审计修了什么:
  - entry 语义按 type 分叉 (审计 6.1): claude-cli→ClaudeCliExecutor,
    zhipu-api→ZhipuApiExecutor。yaml 不再承载请求逻辑, 请求在 executor 代码里。
  - 升级链终止于 architect (审计 6.3): E/E+ 失败可升 D, D 是顶, 不再升级。
  - 打回时附 validate 全文给同 agent (审计 Q3): 单向反馈通道, 不建 agent 间通信。
  - max_turns 从 yaml 读, 不写死全局 (审计 Q2)。

v1 边界:
  - 第一版单向反馈 (scheduler→agent), 无 agent 间通信
  - 升级到 D 后再失败 → 不再升级, neijinglu 报告 + 退出
"""

from __future__ import annotations
import time
from dataclasses import dataclass

from . import config
from .executors import (
    BaseExecutor, ExecutorResult,
    ClaudeCliExecutor, ZhipuApiExecutor,
)

# 升级链: E 搞不定→E+, E+ 搞不定→D, D 是顶
_ESCALATION = {"E": "E+", "E+": "D"}
_EXECUTOR_BY_TYPE = {
    "claude-cli": ClaudeCliExecutor,
    "zhipu-api": ZhipuApiExecutor,
}


@dataclass
class DispatchResult:
    level: str                      # 实际用的 level (可能升级过)
    agent_cfg: dict
    executor_result: ExecutorResult
    attempts: int                   # 本 level 尝试次数 (含升级)


def load_agents() -> dict:
    """读 agents.toml → {level: [agent_cfg, ...]}。

    用 stdlib tomllib (审计: 去 pyyaml 依赖)。
    toml 不允许 key 含 '+', E+ 在 toml 里写成 E_plus, 这里归一化回 'E+'。
    """
    import tomllib
    with open(config.AGENTS_TOML, "rb") as f:
        data = tomllib.load(f)
    raw = data.get("agents", {})
    # E_plus → E+ 归一化
    agents = {}
    for k, v in raw.items():
        key = "E+" if k == "E_plus" else k
        agents[key] = v
    return agents


def pick_agent(agents: dict, level: str, role: str = None) -> dict:
    """选 agent: role 精准匹配优先, 否则 default。"""
    candidates = agents.get(level, [])
    if not candidates:
        raise RuntimeError(f"yaml 无 {level} 层 agent")
    if role:
        for a in candidates:
            if role in (a.get("roles") or []):
                return a
    for a in candidates:
        if a.get("default"):
            return a
    return candidates[0]


def pick_agent_fallback_chain(agents: dict, level: str, role: str = None, exclude: set = None) -> list[dict]:
    """返回该层所有可用 agent，第一个是首选，后续是容灾备选。"""
    candidates = agents.get(level, [])
    if not candidates:
        return []
    exclude = exclude or set()
    result = []
    seen = set()
    for a in candidates:
        if role and role in (a.get("roles") or []) and a.get("model", "") not in seen and a.get("model", "") not in exclude:
            result.append(a); seen.add(a.get("model", ""))
    for a in candidates:
        if a.get("default") and a.get("model", "") not in seen and a.get("model", "") not in exclude:
            result.append(a); seen.add(a.get("model", ""))
    for a in candidates:
        if a.get("model", "") not in seen and a.get("model", "") not in exclude:
            result.append(a); seen.add(a.get("model", ""))
    return result


def dispatch(
    task: str,
    level: str,
    task_id: str,
    agents: dict,
    feedback: str = "",      # 打回时附的 validate 全文 (审计 Q3)
    baseline_ref: str = "",  # snapshot ref, 用于算 changed_files 基线
    cwd: str = "",           # worktree 沙箱路径; 空则 executor 用 PROJECT_ROOT
) -> DispatchResult:
    """选 executor 跑一次。失败由调用方决定升级或打回。"""
    agent_cfg = pick_agent(agents, level)
    etype = agent_cfg.get("type", "claude-cli")
    executor_cls = _EXECUTOR_BY_TYPE.get(etype)
    if not executor_cls:
        raise RuntimeError(f"未知 executor type: {etype}")

    # 打回反馈拼进 prompt (审计 Q3: 单向反馈通道)
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
    """升级 (审计 6.3)。D 是顶, 返回 None 表示不再升级。"""
    return _ESCALATION.get(level)


# 注: 打回循环的实现在 __main__._dispatch_with_validation (dispatcher + validator
# 必须交替, validate 在 __main__ 调用)。不在此处重复一份, 避免死代码分歧。
