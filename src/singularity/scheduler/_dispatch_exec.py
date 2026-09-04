__all__ = ['_build_synthesis_prompt', '_dispatch_committee', '_run_executor', 'dispatch']

from singularity.scheduler.dispatcher import (
    load_agents, _ensure_agent_type, pick_agent, pick_agent_fallback_chain,
    agent_api_available, _build_agent_from_registry, DispatchResult,
    _EXECUTOR_BY_TYPE,
)
from singularity.scheduler._dispatch_skills import (
    _load_skills_for_agent, _load_mcp_for_agent, _make_permission_checker,
)
from singularity.scheduler import tracker, config
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler import witness
from singularity.scheduler.log import timed
import json, time, logging, threading

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
    """选 executor 并执行。架构任务: 委员会并行→合成; 其他: 单模型 fallback 链。"""
    chain = pick_agent_fallback_chain(agents, level, project_lineup=project_lineup)
    if not chain:
        raise RuntimeError(f"无可用 {level} 层 agent")

    # ── 架构任务: 委员会模式 (多模型并行 → fuse_architecture 合成) ──
    # 仅架构/系统设计类任务走 3 模型碰撞, research/QA/安全/实现 单模型即可
    from .execution_judge import _is_architecture_task
    if _is_architecture_task(task) and len(chain) >= 2:
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
    """多模型委员会: 所有可用D模型并行产出→合成。"""
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
            except Exception as _e:
                logging.getLogger(__name__).warning("heartbeat failed: %s", _e)
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
                    changed_files: list = []
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



