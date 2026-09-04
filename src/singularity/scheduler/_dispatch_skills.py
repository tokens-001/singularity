__all__ = ['_load_mcp_for_agent', '_load_skills_for_agent', '_make_permission_checker', '_ntilc_filter', 'invalidate_mcp_cache', 'invalidate_skill_cache']

from singularity.scheduler.dispatcher import (
    load_agents, _ensure_agent_type, pick_agent, _build_agent_from_registry,
    DispatchResult, _CACHE_LOCK, _SKILL_CACHE, _MCP_CACHE,
)
from singularity.scheduler import config
from singularity.scheduler.log import timed
import os, json, time, logging
from pathlib import Path

def _ntilc_filter(task_desc: str, skills: dict) -> dict:
    """NTILC 工具检索: 语义匹配过滤无关 skill，省 ~95% 上下文。

    优先用 embedding (cosine 相似度), 模型不可用时降级为关键词重叠。
    """
    if not skills or len(skills) <= 3:
        return dict(skills)

    # 尝试 embedding 语义匹配
    try:
        from singularity.scheduler.memory import _embed, _cosine_sim
        task_emb = _embed(task_desc)
        if task_emb:
            scored = []
            for name, skill in skills.items():
                skill_text = f"{skill.description} {skill.name}"
                skill_emb = _embed(skill_text)
                if skill_emb:
                    sim = _cosine_sim(task_emb, skill_emb)
                    scored.append((sim, name, skill))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                relevant = [(n, s) for (o, n, s) in scored if o > 0.3]
                if len(relevant) < 2:
                    relevant = [(n, s) for (o, n, s) in scored[:2]]
                return dict(relevant)
    except Exception:
        pass  # embedding 不可用, 降级关键词

    # 关键词降级路径
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

