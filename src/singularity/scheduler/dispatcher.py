"""dispatcher.py — 读 agents.toml 选 executor 并调用。

v2: 集成 api_store + model_registry，API 欠费/限流时自动跳过对应 agent。
project_lineup 支持项目级自定义编组。
"""

from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass

from singularity.scheduler import config, witness
from singularity.scheduler.log import timed
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler.executors import (
    BaseExecutor, ExecutorResult,
    ClaudeCliExecutor, ZhipuApiExecutor, OpenAIAgentExecutor, AnthropicApiExecutor,
)

# 档位 → 下一档 的映射表。**故意为空**：两档制已合并成单档("any")，
# 没有"下一档"可升。`escalate()` 因此恒返回 None（详见它的 docstring）。
# 将来重加档位时在这里填表即可，调用方不用动。
_ESCALATION: dict[str, str] = {}


def _all_agents_list(agents: dict) -> list[dict]:
    """两档后: 从所有层级收集 agent (去重)。"""
    seen = set()
    result = []
    for level_agents in agents.values():
        for a in (level_agents if isinstance(level_agents, list) else []):
            m = a.get("model", "")
            if m and m not in seen:
                seen.add(m)
                result.append(a)
    return result


_EXECUTOR_BY_TYPE = {
    "claude-cli": ClaudeCliExecutor,
    "zhipu-api": ZhipuApiExecutor,
    "anthropic-api": AnthropicApiExecutor,
    "openai-agent": OpenAIAgentExecutor,
}

# ── Skill/MCP 缓存 (P2-4) ──────────────────────────────────
# dispatch() 在 worker 线程读, invalidate_* 从 Flask 请求线程调。
# 用一把 Lock 串行化 读/写/失效, 防并发 lost-update (同 tracker._LOCK 模式)。
_CACHE_LOCK = threading.Lock()
_SKILL_CACHE: dict[tuple[str, str], tuple[list, str, dict]] = {}  # (level,model) → (tools,prompt,skills)
_MCP_CACHE: tuple[list, object] | None = None  # (tools, executor_callable), None=未加载/已失效


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
    from ._io import load_toml
    data = load_toml(config.AGENTS_TOML)
    raw = data.get("agents", {})
    agents = {}
    for k, v in raw.items():
        key = k
        agents[key] = list(v)  # shallow copy

    # 合并自定义覆盖
    custom = _load_custom_agents()
    for k, cfgs in custom.items():
        if k.startswith("_"):
            continue
        level = k
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


def _ensure_agent_type(agent_cfg: dict) -> dict:
    """自动补全 agent 配置：缺 type/provider/api_key_env 时从 model_registry + api_store 填充。

    ponytail: agents_custom.json 可以只写 model，其余字段自动推断。
    """
    # ponytail: 先补全缺失字段
    model = agent_cfg.get("model", "")
    need_fill = not agent_cfg.get("type") or not agent_cfg.get("api_key_env")
    if need_fill and model:
        try:
            from . import model_registry as mr
            from . import api_store
            m = mr.get(model)
            if m:
                apis = api_store.list_all()
                api = apis.get(m.provider)
                if api:
                    if not agent_cfg.get("type"):
                        agent_cfg["type"] = "openai-agent"
                    if not agent_cfg.get("provider"):
                        agent_cfg["provider"] = m.provider
                    if not agent_cfg.get("api_key_env"):
                        agent_cfg["api_key_env"] = api.api_key_env
                    if not agent_cfg.get("entry"):
                        agent_cfg["entry"] = api.base_url + "/chat/completions"
        except Exception as _e:
            logging.getLogger(__name__).warning("agent config scan failed: %s", _e)

    # ponytail: 尊重 models.toml 每模型 max_turns(thinking 模型如 deepseek-v4-pro=3 轮),
    # 不再硬抬 ≥15——慢模型 15 轮×~150s 直接撞 orchestrator 900s deadline
    if model:
        try:
            from . import model_registry as mr
            m = mr.get(model)
            registry_max = getattr(m, 'max_turns', 10) or 10 if m else 10
        except Exception:
            registry_max = 10
        current_max = agent_cfg.get("max_turns", 0)
        agent_cfg["max_turns"] = current_max or registry_max or 8
    agent_cfg.setdefault("max_tool_turns", 3)
    agent_cfg.setdefault("request_template", {"model": model, "max_tokens": config.MODEL_MAX_TOKENS})
    return agent_cfg


def agent_api_available(agent_cfg: dict) -> bool:
    """检查 agent 的 API 是否可用。

    所有类型都经过 model_registry → api_store 检查。
    claude-cli 也检查 api_store 状态。

    硬限制：OpenAI 模型必须在 _order 里显式列出才会被选中，
    防止误烧 GPT 额度。
    """
    # 自动补全缺失的 type/provider
    agent_cfg = _ensure_agent_type(agent_cfg)
    model = agent_cfg.get("model", "")
    agent_type = agent_cfg.get("type", "")

    # ponytail: API 类 agent 必须有 entry 或 api_key_env, 否则无法调 API
    if agent_type in ("openai-agent", "zhipu-api"):
        entry = agent_cfg.get("entry", "")
        key_env = agent_cfg.get("api_key_env", "")
        if not entry and not key_env:
            return False  # 空壳 agent (前端添加但未配置)

    provider = ""
    if model:
        try:
            from . import model_registry as mr
            provider = mr.provider_for_model(model)
            if provider:
                from . import api_store
                # 模型级判据，不是 provider 级：provider 被标欠费时，同厂商下
                # 还能调的模型不该被连坐踢出（智谱付费模型欠费 → 免费/低价模型一起消失）。
                if not api_store.is_model_available(model):
                    return False
        except Exception as e:
            from . import witness; witness.warn("dispatch", f"api_check:{e}")

    # 硬限制：OpenAI 模型除非在 _order 显式列出或有显式配置，否则不可用
    if provider == "openai":
        custom = _load_custom_agents()
        all_ordered = []
        for tier_order in (custom.get("_order", {}) or {}).values():
            all_ordered.extend(tier_order)
        # ponytail: built-from-registry agents may have agent_cfg with provider set;
        # only reject if _order is defined AND model is not in it
        if all_ordered and model not in all_ordered:
            return False

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
            "roles": ["generic"],
            "sandbox": "worktree",
            "request_template": {"model": model_name, "max_tokens": config.MODEL_MAX_TOKENS},
        }
    except Exception as e:
        from . import witness; witness.warn("dispatch", f"build_agent:{e}")
        return None


def _find_agent_by_model(agents: dict, model_name: str) -> dict | None:
    """跨所有层搜索 agent 配置。"""
    for level_cfgs in agents.values():
        for a in level_cfgs:
            if a.get("model") == model_name:
                return a
    return None




def pick_agent_fallback_chain(agents: dict, level: str, role: str = None,
                               exclude: set = None,
                               project_lineup: dict[str, list[str]] = None,
                               fallback_levels: list[str] = None) -> list[dict]:
    """返回该层可用 agent 列表。project_lineup > role > default > 其他。

    API 不可用的自动跳过。
    若目标层无可用 agent，尝试 fallback_levels 列表。
    """
    def _collect(tier: str):
        cands = agents.get(tier, [])
        if not cands:
            return []
        excl = exclude or set()
        res = []; s = set()
        lineup = (project_lineup or {}).get(tier, [])
        if lineup:
            for mn in lineup:
                found = None
                for a in cands:
                    k = a.get("model","")
                    if k == mn and k not in s and k not in excl and agent_api_available(a):
                        found = a; break
                if not found:
                    cross = _find_agent_by_model(agents, mn)
                    if cross and agent_api_available(cross): found = cross
                if found:
                    res.append(found); s.add(found.get("model",""))
        if role:
            for a in cands:
                k = a.get("model","")
                if role in (a.get("roles") or []) and k not in s and k not in excl and agent_api_available(a):
                    res.append(a); s.add(k)
        # 同层 agent 平等, 不区分 default 优先级
        for a in cands:
            k = a.get("model","")
            if k not in s and k not in excl and agent_api_available(a):
                res.append(a); s.add(k)
        return res

    # 两档后: level 为空时从全池收集
    if level:
        result = _collect(level)
    else:
        result = []
        excl = exclude or set()
        seen = set()
        for a in _all_agents_list(agents):
            k = a.get("model", "")
            if k not in seen and k not in excl and agent_api_available(a):
                result.append(a)
                seen.add(k)
    if not result and fallback_levels:
        for fl in fallback_levels:
            result = _collect(fl)
            if result:
                break
    # ponytail: dedup across tiers (same model may appear in multiple levels)
    seen = set()
    deduped = []
    for a in result:
        k = a.get("model", "")
        if k not in seen:
            deduped.append(a); seen.add(k)

    # ── 路由学习者权重排序 ──
    # 按模型在所有任务类型下的平均 Hedge 权重降序,
    # 权重>1=近期成功多, <1=近期失败多, 1=冷启动
    if len(deduped) > 1:
        try:
            from singularity.scheduler.route_learner import load_learner
            learner = load_learner()
            if learner and learner._stats:
                model_weights: dict[str, float] = {}
                for stat in learner._stats.values():
                    prev = model_weights.get(stat.model, 1.0)
                    # avg across task_types for this model
                    model_weights[stat.model] = (prev + stat.hedge_weight) / 2
                if model_weights:
                    deduped.sort(
                        key=lambda a: model_weights.get(a.get("model", ""), 1.0),
                        reverse=True,
                    )
        except Exception as _e:
            logging.getLogger(__name__).warning("route learner sort failed: %s", _e)  # learner 挂了不阻塞选择

    # 注：这里曾经加过"轮换"（每次换个模型打头），已撤掉。
    # 理由：`any` 层那个列表**本质是"主力 + 备用"** —— 原设计就是永远用第一个、
    # 挂了才用下一个。轮换会让同一项目里调研用一个模型、架构用另一个、审查又换回来，
    # 出了问题不好归因，成本和效果也忽高忽低；而且两个模型的样本都变少，学习器收敛更慢。
    # 想验证"哪些模型配了但没跑过"，看用量页就够了（未使用 / 配额耗尽都会标出来）。

    # ── 熔断过滤：刚连挂的模型本轮跳过 ──
    # fail-open: 全池都熔断时原样返回，否则一个坏 key 能让整个调度停摆
    from singularity.scheduler import _model_breaker
    alive = [a for a in deduped if _model_breaker.is_available(a.get("model", ""))]
    return alive or deduped



from singularity.scheduler._dispatch_skills import *  # noqa: F401,F403
from singularity.scheduler._dispatch_exec import *  # noqa: F401,F403
from singularity.scheduler._dispatch_crud import *  # noqa: F401,F403
