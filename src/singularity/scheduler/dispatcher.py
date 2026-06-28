"""dispatcher.py — 读 agents.toml 选 executor 并调用。

v2: 集成 api_store + model_registry，API 欠费/限流时自动跳过对应 agent。
project_lineup 支持项目级自定义编组。
"""

from __future__ import annotations
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

_ESCALATION = {}


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
        except Exception:
            pass

    # ponytail: coding任务需≥15轮，不论是否已有配置都强制下限
    if model:
        try:
            from . import model_registry as mr
            m = mr.get(model)
            registry_max = getattr(m, 'max_turns', 10) or 10 if m else 10
        except Exception:
            registry_max = 10
        current_max = agent_cfg.get("max_turns", 0)
        agent_cfg["max_turns"] = max(current_max, registry_max, 15)
    agent_cfg.setdefault("max_tool_turns", 5)
    agent_cfg.setdefault("request_template", {"model": model, "max_tokens": 8192})
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
                if not api_store.is_available(provider):
                    return False
        except Exception as e:
            from . import witness; witness.heartbeat("dispatch", "warn", status="error", detail=f"api_check:{e}")

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
            "roles": ["daily"],
            "sandbox": "worktree",
            "request_template": {"model": model_name, "max_tokens": 8192},
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
    level 为空时从全池选 (两档后不再强制 E/E+/D)。
    """
    candidates = agents.get(level, []) if level else _all_agents_list(agents)
    if not candidates:
        raise RuntimeError(f"无可用 agent (level={level or 'any'})")

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

    # 用户自定义排序
    custom_order = (_load_custom_agents().get("_order", {}) or {}).get(level, [])
    if custom_order:
        rank = {m: i for i, m in enumerate(custom_order)}
        available = [a for a in candidates if agent_api_available(a)]
        available.sort(key=lambda a: rank.get(a.get("model", ""), 999))
        if available:
            return available[0]

    # ── 路由学习者权重 ──
    try:
        from singularity.scheduler.route_learner import load_learner
        learner = load_learner()
        if learner and learner._stats:
            available = [a for a in candidates if agent_api_available(a)]
            if len(available) > 1:
                weights = {model: learner.get_weight(model, "fix")
                          for a in available
                          for model in [a.get("model", "")]
                          if model}
                if any(w > 0 for w in weights.values()):
                    available.sort(key=lambda a: weights.get(a.get("model", ""), 1.0), reverse=True)
    except Exception:
        pass  # learner 挂了不阻塞

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
    return deduped


def _ntilc_filter(task_desc: str, skills: dict) -> dict:
    """NTILC 神经工具检索: 关键词重叠过滤无关 skill，省 ~95% 上下文。

    论文: NTILC (2026.06) — 不相关工具造成 semantic blur，嵌入匹配降 O(N)→O(log N)。
    ponytail: 不做嵌入模型，关键词重叠已够。需要时加 sentence-transformers。
    """
    if not skills or len(skills) <= 3:
        return dict(skills)
    task_words = set(task_desc.lower().split())
    scored = []
    for name, skill in skills.items():
        desc_words = set(f"{skill.description} {skill.name}".lower().split())
        overlap = len(task_words & desc_words)
        scored.append((overlap, name, skill))
    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [(n, s) for (o, n, s) in scored if o > 0]
    if len(relevant) < 2:
        relevant = [(n, s) for (o, n, s) in scored[:2]]
    return dict(relevant)


def _load_skills_for_agent(level: str, model: str, task_desc: str = "") -> tuple[list, str, dict]:
    """为 agent 加载绑定的 Skill。返回 (tools, prompt, skills_dict)。

    P2-4: 结果按 (level, model) 缓存。失效见 invalidate_skill_cache。
    NTILC: task_desc 非空时按相关性过滤，省无关 skill 上下文。
    """
    key = (level, model)
    with _CACHE_LOCK:
        if key in _SKILL_CACHE:
            tools, prompt, skills = _SKILL_CACHE[key]
            if task_desc and skills:
                skills = _ntilc_filter(task_desc, skills)
                tools = [s.function_def for s in skills.values() if s.type == "tool" and s.function_def]
                prompt = "\n".join(s.body for s in skills.values() if s.type == "prompt" and s.body)
            return tools, prompt, skills
    try:
        from singularity.skills.skill_loader import (
            load_skills, get_tool_definitions, get_prompt_additions, get_agent_skills,
        )
        skill_names = get_agent_skills(level, model)
        if skill_names:
            all_skills = load_skills()
            skills = {n: all_skills[n] for n in skill_names if n in all_skills}
            if task_desc:
                skills = _ntilc_filter(task_desc, skills)
            result = (get_tool_definitions(skills), get_prompt_additions(skills), skills)
            with _CACHE_LOCK:
                _SKILL_CACHE[key] = result
            return result
    except Exception as e:
        witness.heartbeat('dispatcher', f'warn:{e}')
    return [], "", {}


def _load_mcp_for_agent() -> tuple[list, object]:
    """加载 MCP 工具。返回 (tools, executor_callable)。

    P2-4: 全局单条缓存。失效见 invalidate_mcp_cache。
    """
    global _MCP_CACHE
    with _CACHE_LOCK:
        if _MCP_CACHE is not None:
            return _MCP_CACHE
    try:
        from .mcp import get_registry
        reg = get_registry()
        tools = reg.get_openai_tools()
        result = (tools, reg.execute_tool)
        with _CACHE_LOCK:
            _MCP_CACHE = result
        return result
    except Exception as e:
        witness.heartbeat('dispatcher', f'warn:{e}')
    return [], None


def invalidate_skill_cache(level: str = None, model: str = None) -> None:
    """失效 Skill 缓存。

    不传参 = 清全部 (skill_add/skill_delete 触发);
    传 (level, model) = 只清那条 (agent_skill_update 触发)。
    """
    with _CACHE_LOCK:
        if level is None and model is None:
            _SKILL_CACHE.clear()
        else:
            _SKILL_CACHE.pop((level, model), None)


def invalidate_mcp_cache() -> None:
    """失效 MCP 缓存 (全局, 任一 MCP 写操作触发)。"""
    global _MCP_CACHE
    with _CACHE_LOCK:
        _MCP_CACHE = None


def _make_permission_checker() -> callable:
    """创建权限检查回调。"""
    try:
        from .permission import check_tool, check_path, check_command, needs_approval
        def _check(tool_name, args, agent_level, agent_model, task_id):
            ok, reason = check_tool(agent_level, agent_model, tool_name)
            if not ok:
                return False, reason
            if tool_name in ("read_file", "write_file") and args.get("path"):
                ok, reason = check_path(agent_level, agent_model, args["path"])
                if not ok:
                    return False, reason
            if tool_name == "run_command" and args.get("command"):
                ok, reason = check_command(agent_level, agent_model, args["command"])
                if not ok:
                    return False, reason
            if needs_approval(agent_level, agent_model, tool_name):
                try:
                    _pending_sse_events.append({"kind": "approval", "msg": f"[{task_id[:8]}] {tool_name} 需审批",
                                 "ts": time.time(), "task_id": task_id})
                except Exception as e:
                    witness.heartbeat('dispatcher', f'warn:{e}')
            return True, ""
        return _check
    except Exception:
        return None


@timed(name="dispatcher")
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
    """选 executor 并执行。D层:委员会并行→合成。其他层:fallback链。"""
    chain = pick_agent_fallback_chain(agents, level, project_lineup=project_lineup)
    if not chain:
        raise RuntimeError(f"无可用 {level} 层 agent")

    # ── D层委员会模式: 多模型并行 → 合成 ──
    if False:
        return _dispatch_committee(task, level, task_id, agents, chain, feedback,
                                   baseline_ref, cwd)

    # ── 单模型 fallback 链 ──
    last_error = ""
    for attempt, agent_cfg in enumerate(chain[:3]):
        agent_cfg = _ensure_agent_type(agent_cfg)
        etype = agent_cfg.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if not executor_cls:
            last_error = f"未知 executor type: {etype}"
            continue

        full_task = task
        if feedback:
            full_task = (
                f"{task}\n\n"
                f"---\n[上一轮校验反馈, 请据此修正]\n{feedback}"
            )

        try:
            result = _run_executor(
                executor_cls, agent_cfg, full_task, task_id, level,
                baseline_ref=baseline_ref, cwd=cwd,
            )
            if result and result.raw_output:
                return DispatchResult(
                    level=level, agent_cfg=agent_cfg,
                    executor_result=result, attempts=attempt + 1,
                )
            exec_error = getattr(result, 'error', '') if result else 'no result'
            last_error = f"{agent_cfg.get('model', '?')}: 空输出" + (f" [{exec_error}]" if exec_error else "")
        except Exception as e:
            last_error = f"{agent_cfg.get('model', '?')}: {type(e).__name__}: {e}"[:200]

    raise RuntimeError(f"{level} 层所有 agent 均失败: {last_error}")


def _run_executor(executor_cls, agent_cfg: dict, full_task: str, task_id: str,
                  level: str, baseline_ref: str = "", cwd: str = ""):
    """构建 executor 并执行。"""
    skill_tools, skill_prompt, skills = _load_skills_for_agent(
        level, agent_cfg.get("model", ""), task_desc=full_task)
    mcp_tools, mcp_executor = _load_mcp_for_agent()
    perm_checker = _make_permission_checker()

    executor: BaseExecutor = executor_cls(
        agent_cfg, full_task, task_id, baseline_ref=baseline_ref, cwd=cwd,
        agent_level=level,
        skills=skills, skill_tools=skill_tools, skill_prompt=skill_prompt,
        mcp_tools=mcp_tools, mcp_executor=mcp_executor,
        permission_checker=perm_checker,
    )
    return executor.run()


def _dispatch_committee(task: str, level: str, task_id: str, agents: dict,
                        chain: list[dict], feedback: str = "",
                        baseline_ref: str = "", cwd: str = "") -> DispatchResult:
    """D层委员会: 所有可用D模型并行产出→合成。"""
    import concurrent.futures

    def _run_one(agent_cfg):
        agent_cfg = _ensure_agent_type(agent_cfg)
        etype = agent_cfg.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if not executor_cls:
            return None, agent_cfg
        full_task = task
        if feedback:
            full_task = f"{task}\n\n---\n[上一轮校验反馈]\n{feedback}"
        try:
            result = _run_executor(
                executor_cls, agent_cfg, full_task,
                f"{task_id}_{agent_cfg.get('model','?')[:8]}", level,
                baseline_ref=baseline_ref, cwd=cwd,
            )
            return result, agent_cfg
        except Exception:
            try: witness.heartbeat('dispatch', 'warn:run_one')
            except Exception: pass
            return None, agent_cfg

    # 并行派发
    outputs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chain), 4)) as ex:
        futures = {ex.submit(_run_one, a): a for a in chain}
        # 等待最多 300s 收集任意数量的完成结果
        done, _ = concurrent.futures.wait(futures, timeout=300, return_when='ALL_COMPLETED')
        for fut in done:
            try:
                result, agent_cfg = fut.result()
                if result and result.raw_output:
                    outputs.append((agent_cfg.get("model", "?"), result))
            except Exception:
                pass  # 单个模型失败不阻断委员会

    if not outputs:
        raise RuntimeError("委员会所有模型均无产出")

    if len(outputs) == 1:
        model, result = outputs[0]
        return DispatchResult(
            level=level,
            agent_cfg=chain[0],
            executor_result=result,
            attempts=1,
        )

    # 合成: 架构任务用专用 fusion，其他用通用委员会合成
    from .execution_judge import _is_architecture_task, fuse_architecture

    if _is_architecture_task(task):
        # 架构方案: 两阶段 fusion (Step 2)
        raw_outputs = [r.raw_output for _, r in outputs]
        try:
            fused = fuse_architecture(task, raw_outputs, judge_model="deepseek-chat")
            if fused:
                # Save individual model outputs for display
                from singularity.scheduler.config import QIDIAN_DIR
                import json as _json
                proj_dir = QIDIAN_DIR / "projects"
                # Store in fusion metadata that the workflow can pick up
                fusion_meta = {
                    "models": [m for m, _ in outputs],
                    "outputs": raw_outputs,
                    "fused": fused,
                    "count": len(outputs),
                }
                # Write to a temp file that workflow can read
                meta_path = QIDIAN_DIR / ".last_fusion.json"
                meta_path.write_text(_json.dumps(fusion_meta, ensure_ascii=False, indent=2))
                # 包装成 ExecutorResult 兼容格式
                class _FusionResult:
                    raw_output = fused
                    success = True
                    error = ""
                return DispatchResult(
                    level=level,
                    agent_cfg={"model": f"fusion({','.join(m for m,_ in outputs)})"},
                    executor_result=_FusionResult(),
                    attempts=len(outputs) + 2,
                )
        except Exception:
            pass  # fusion 失败 → fallback 到通用合成

    # 通用委员会合成
    synthesizer = chain[0]
    synthesis_prompt = _build_synthesis_prompt(task, outputs)
    try:
        etype = synthesizer.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if executor_cls:
            synth_result = _run_executor(
                executor_cls, synthesizer, synthesis_prompt,
                f"{task_id}_synth", level,
                baseline_ref=baseline_ref, cwd=cwd,
            )
            if synth_result and synth_result.raw_output:
                return DispatchResult(
                    level=level,
                    agent_cfg={"model": f"committee({','.join(m for m,_ in outputs)})"},
                    executor_result=synth_result,
                    attempts=len(outputs) + 1,
                )

        # 合成失败: 返回第一个产出
        model, result = outputs[0]
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=result, attempts=len(outputs))
    except Exception:
        model, result = outputs[0]
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=result, attempts=len(outputs))


def _build_synthesis_prompt(task: str, outputs: list[tuple]) -> str:
    """构建委员会合成 prompt。"""
    parts = [f"【原始需求】\n{task}\n\n【委员会各模型产出】"]
    for i, (model, result) in enumerate(outputs, 1):
        parts.append(f"\n── 模型{i}: {model} ──\n{result.raw_output[:3000]}")
    parts.append("""

【你的任务】
你是委员会主席。综合以上各模型的方案，产出一份最终方案。
- 取各方案之长，避各方案之短
- 如有冲突，选择论证更充分的观点
- 保持原有 JSON 格式（如各模型都输出 JSON）
- 不要引入各模型都没提到的新内容""")
    return "\n".join(parts)


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
    disabled = custom.get("_disabled", {}).get(level, [])
    if model in disabled:
        disabled.remove(model)
        custom.setdefault("_disabled", {})[level] = disabled
        _save_custom_agents(custom)
        _notify_agent_change()
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
    _notify_agent_change()
    return cfg

def remove_agent(level: str = "", model: str = "") -> bool:
    """禁用 agent: 从 custom 删 + 加入 _disabled。两档后 level 可选。"""
    key = level or "any"
    disabled_key = level or "any"

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
    key = "E_plus" if level == "E+" else level

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
