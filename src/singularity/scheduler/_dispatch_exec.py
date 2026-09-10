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
from singularity.scheduler._io import apply_json_patch, _parse_patch_ops
from singularity.scheduler import model_registry
from singularity.scheduler import _model_breaker
import json, os, time, logging, threading

# ── 委员会收集初稿的时间预算 ──
# 单次模型调用本身有上限（claude-cli 300s / openai-agent 240s），所以一波的耗时
# 取决于最慢的那个模型。把波超时调小只会让慢模型白跑——输出被丢弃、token 照花。
# 这个 timeout 也不决定"何时返回"：调用点用 with ThreadPoolExecutor(...)，退出时
# shutdown(wait=True) 会 join 所有线程（实测 timeout=0.3s 仍等了 3s），它只决定
# "何时去读已完成的结果"。调小它救不了总耗时。
_WAVE_TIMEOUT = float(os.environ.get("QIDIAN_DEBATE_TIMEOUT", "300"))


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
    # 冷启动先验: 任务关键词匹配模型 strengths, 擅长的模型排到链首
    chain = _prefer_by_strengths(task, chain)

    # ── 架构任务: 委员会模式 (多模型并行 → fuse_architecture_v2 合成) ──
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
                _model_breaker.record_success(agent_cfg.get("model", ""))
                return DispatchResult(
                    level=level, agent_cfg=agent_cfg,
                    executor_result=result, attempts=attempt + 1,
                )
            exec_error = getattr(result, 'error', '') if result else 'no result'
            last_error = f"{agent_cfg.get('model', '?')}: 空输出" + (f" [{exec_error}]" if exec_error else "")
            _model_breaker.record_failure(agent_cfg.get("model", ""))
        except Exception as e:
            last_error = f"{agent_cfg.get('model', '?')}: {type(e).__name__}: {e}"[:200]
            _model_breaker.record_failure(agent_cfg.get("model", ""))

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


def _run_no_tools(agent_cfg: dict, prompt: str, tag: str, level: str,
                  baseline_ref: str = "", cwd: str = "") -> str | None:
    """跑一个禁工具的单模型调用, 返回 raw_output 或 None(失败静默)。"""
    agent_cfg = _ensure_agent_type(agent_cfg)
    agent_cfg = {**agent_cfg, "no_tools": True}
    etype = agent_cfg.get("type", "claude-cli")
    executor_cls = _EXECUTOR_BY_TYPE.get(etype)
    if not executor_cls:
        return None
    # 禁工具是委员会的前提（纯文本出方案，别改磁盘）。claude-cli 这类自带工具的执行器
    # 禁不掉 —— 告警让它在 trace 里可见，而不是假装禁住了。
    if not getattr(executor_cls, "honors_no_tools", False):
        witness.warn("dispatcher", f"no_tools_not_enforced:{etype}"[:80])
    try:
        result = _run_executor(executor_cls, agent_cfg, prompt, tag, level,
                               baseline_ref=baseline_ref, cwd=cwd)
    except Exception as e:
        # 以前这里静默 return None —— 委员会里模型失败完全看不见。
        # 实测 3 家阵容有 2 家无声无息没产出，只能靠猜（超时？空输出？）。
        witness.warn("dispatcher", f"no_tools_fail:{tag}:{type(e).__name__}"[:80])
        return None
    if result and result.raw_output:
        return result.raw_output
    err = getattr(result, "error", "") if result else "no result"
    witness.warn("dispatcher", f"no_tools_empty:{tag}:{err}"[:80])
    return None


# 任务关键词 → strengths 能力标签 (benchmark/手填的 strengths 作冷启动先验)
_STRENGTH_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("sql", "数据库", "查询", "query"), "数据查询"),
    (("重构", "refactor", "多文件"), "多文件重构"),
    (("长程", "多步", "多阶段"), "长程任务"),
]


def _strength_label_for(task: str) -> str:
    """从任务描述提取匹配的 strengths 标签, 无匹配返回空串。"""
    t = (task or "").lower()
    for kws, label in _STRENGTH_KEYWORDS:
        if any(k in t for k in kws):
            return label
    return ""


def _prefer_by_strengths(task: str, chain: list[dict]) -> list[dict]:
    """strengths 匹配重排: 擅长该任务能力的模型排到链首(冷启动先验)。"""
    label = _strength_label_for(task)
    if not label or len(chain) <= 1:
        return chain

    def _matches(a: dict) -> bool:
        m = model_registry.get(a.get("model", ""))
        return bool(m and label in (m.strengths or []))

    matched = [a for a in chain if _matches(a)]
    rest = [a for a in chain if not _matches(a)]
    return matched + rest if matched else chain




def _dispatch_committee(task: str, level: str, task_id: str, agents: dict,
                        chain: list[dict], feedback: str = "",
                        baseline_ref: str = "", cwd: str = "") -> DispatchResult:
    """多模型委员会: 所有可用D模型并行产出→合成。"""
    import concurrent.futures

    # 席位视角: 默认关闭 —— A/B 盲评证伪（有视角 31 vs 无视角 32，略输）：
    # 一句"你关注风险/创新"的提示词 = 伪碰撞，不产生真实差异。
    # 真正有效的是辩论轮（盲评 +3~5 分），别把两者搞混。
    # QIDIAN_COMMITTEE_PERSPECTIVE=1 可重新打开（保留做后续 A/B）。
    if os.environ.get("QIDIAN_COMMITTEE_PERSPECTIVE") == "1":
        _PERSPECTIVES = [
            "你关注: 风险点、边界条件、回滚策略。方案必须稳健,不能炸。",
            "你关注: 有没有完全不同的思路?业界最新实践是什么?大胆提替代方案。",
            "你关注: 这方案能落地吗?需要多少个文件?现有代码风格兼容吗?复杂度实际是多少?",
            "你关注: 和现有架构的一致性。不要引入不兼容的变更。",
        ]
    else:
        _PERSPECTIVES = [None, None, None, None]

    # 并行派发初稿 (禁工具, 直接输出 JSON 方案)
    outputs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chain), 4)) as ex:
        futures = {}
        for i, a in enumerate(chain):
            full_task = task
            if feedback:
                full_task = f"{task}\n\n---\n[上一轮校验反馈]\n{feedback}"
            perspective = _PERSPECTIVES[i % len(_PERSPECTIVES)]
            if perspective:
                full_task = f"{full_task}\n\n[你的视角] {perspective}"
            futures[ex.submit(_run_no_tools, a, full_task,
                              f"{task_id}_{a.get('model','?')[:8]}",
                              level, baseline_ref, cwd)] = a
        # 等待最多 300s 收集任意数量的完成结果
        done, _ = concurrent.futures.wait(futures, timeout=_WAVE_TIMEOUT, return_when='ALL_COMPLETED')
        for fut in done:
            agent_cfg = futures[fut]
            try:
                raw = fut.result()
                if raw:
                    outputs.append((agent_cfg.get("model", "?"), raw))
            except Exception:
                pass  # 单个模型失败不阻断委员会

    # 部分模型没产出 → 告警。否则委员会"3 家碰撞"实际只有 1 家，外面完全看不出来
    if len(outputs) < len(chain):
        got = {m for m, _ in outputs}
        miss = [a.get("model", "?") for a in chain if a.get("model") not in got]
        witness.warn("dispatcher",
                     f"committee_partial:{len(outputs)}/{len(chain)} 缺:{','.join(miss)}"[:80])

    if not outputs:
        raise RuntimeError("委员会所有模型均无产出")

    if len(outputs) == 1:
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        return DispatchResult(
            level=level,
            agent_cfg=chain[0],
            executor_result=ExecutorResult(success=True, raw_output=raw),
            attempts=1,
        )

    # 合成: 架构任务用专用 fusion，其他用通用委员会合成
    from .execution_judge import _is_architecture_task, fuse_architecture_v2

    if _is_architecture_task(task):
        # v2 是唯一路径。旧两阶段 2026-09-11 删除 —— 它本身有致命缺陷（取并集膨胀到
        # 输入之和 1.8×、撞 max_tokens 腰斩、丢过整个 tasks 段），却被当作 v2 失败时的
        # 兜底；而告警日志显示它**从未在真机触发过**。现在的兜底是 v2 内部的
        # 「提取失败换模型重试」，那条比它强得多。QIDIAN_FUSION_V2 开关随之一并删除
        # （它的语义本来就是"回退旧流程"，没有旧流程了）。
        fused = fuse_architecture_v2(task, list(outputs))
        if not fused:
            # 落到下面的通用合成：每条产出截断到 3000 字。架构方案 20k+ 字，
            # 这是**降级**不是等价替换，必须留痕。
            witness.warn("dispatcher", "fusion_empty_fallback_synthesis"[:80])
        if fused:
            # 委员会产物随 DispatchResult 回传给调用方（_workflow_phases 再落 ProjectState）。
            # 曾经写 QIDIAN_DIR/.last_fusion.json 这个全局单文件 —— 并发下会串项目：
            # Flask threaded=True + 调度循环 concurrent=2，HTTP(_api_projects.run_phase)
            # 和后台循环(app.py 结果处理)两条路径都能进委员会，两个架构任务先后写同一路径，
            # 后写的覆盖先写的，先写的那家读到的是**别人的**模型/产物；读不到时整段静默跳过。
            # 走内存没有这些问题，顺带修掉「非委员会路径捡到上一轮残留文件」。
            fusion_meta = {
                "models": [m for m, _ in outputs],
                "outputs": [o for _, o in outputs],
                "fused": fused,
                "count": len(outputs),
            }
            # 包装成 ExecutorResult 兼容格式
            class _FusionResult:
                # 字段要和 neijinglu.build_report / _save_trace 读的契约对齐,
                # 缺一个就 AttributeError → trace 静默不落盘
                raw_output = fused
                success = True
                error = ""
                changed_files: list = []
                patch_path = ""
                token_count = 0
                elapsed = 0.0
                tool_events: list = []

            # 各模型初稿 + 融合稿，给 _run_planning 落 ProjectState 用。
            # 必须在类体**外**赋值：class body 不做闭包查找，写在里面会 NameError。
            # 不带这个属性时调用方取到 None 直接跳过（非委员会路径）。
            _FusionResult.fusion_meta = fusion_meta
            return DispatchResult(
                level=level,
                agent_cfg={"model": f"fusion({','.join(m for m,_ in outputs)})"},
                executor_result=_FusionResult(),
                attempts=len(outputs) + 2,
            )
        # fusion 失败 → fallback 到通用合成

    # 通用委员会合成
    synthesizer = chain[0]
    synthesis_prompt = _build_synthesis_prompt(task, outputs)
    try:
        etype = synthesizer.get("type", "claude-cli")
        executor_cls = _EXECUTOR_BY_TYPE.get(etype)
        if executor_cls:
            # no_tools 必须带上 —— 委员会全程不改磁盘（见 _run_no_tools 的说明：
            # "禁工具是委员会的前提（纯文本出方案，别改磁盘）"）。兜底这条以前漏了：
            # 合成 agent 带着工具、cwd 又是奇点自己的仓库根，于是把目标项目的架构
            # 直接写进了**奇点仓库的 docs/**（2026-09-11 实测产出 docs/ARCHITECTURE.json）。
            synth_result = _run_executor(
                executor_cls, {**synthesizer, "no_tools": True}, synthesis_prompt,
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
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=ExecutorResult(success=True, raw_output=raw),
                              attempts=len(outputs))
    except Exception:
        model, raw = outputs[0]
        from singularity.scheduler.executors.base import ExecutorResult
        return DispatchResult(level=level, agent_cfg=chain[0],
                              executor_result=ExecutorResult(success=True, raw_output=raw),
                              attempts=len(outputs))


def _build_synthesis_prompt(task: str, outputs: list[tuple]) -> str:
    """构建委员会合成 prompt。"""
    parts = [f"【原始需求】\n{task}\n\n【委员会各模型产出】"]
    for i, (model, result) in enumerate(outputs, 1):
        parts.append(f"\n── 模型{i}: {model} ──\n{result[:3000]}")
    parts.append("""

【你的任务】
你是委员会主席。综合以上各模型的方案，产出一份最终方案。
- 取各方案之长，避各方案之短
- 如有冲突，选择论证更充分的观点
- 保持原有 JSON 格式（如各模型都输出 JSON）
- 不要引入各模型都没提到的新内容""")
    return "\n".join(parts)



