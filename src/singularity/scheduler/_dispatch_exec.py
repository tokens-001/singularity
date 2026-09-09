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
from singularity.scheduler._io import apply_json_patch
from singularity.scheduler import model_registry
import json, os, time, logging, threading

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


def _run_no_tools(agent_cfg: dict, prompt: str, tag: str, level: str,
                  baseline_ref: str = "", cwd: str = "") -> str | None:
    """跑一个禁工具的单模型调用, 返回 raw_output 或 None(失败静默)。"""
    agent_cfg = _ensure_agent_type(agent_cfg)
    agent_cfg = {**agent_cfg, "no_tools": True}
    etype = agent_cfg.get("type", "claude-cli")
    executor_cls = _EXECUTOR_BY_TYPE.get(etype)
    if not executor_cls:
        return None
    try:
        result = _run_executor(executor_cls, agent_cfg, prompt, tag, level,
                               baseline_ref=baseline_ref, cwd=cwd)
        return result.raw_output if result and result.raw_output else None
    except Exception:
        return None


def _is_slow_model(model_id: str) -> bool:
    """慢模型判定: speed=slow 或 reasoning(思考链)。慢模型只出初稿, 不参与辩论后续轮。"""
    e = model_registry.get(model_id)
    return bool(e and (e.speed == "slow" or e.reasoning))


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


def _debate(task: str, members: list[tuple], chain: list[dict], task_id: str,
            level: str, baseline_ref: str = "", cwd: str = "",
            max_rounds: int = 2) -> list[tuple]:
    """多轮辩论: 交叉评审→修订→收敛。members: [(model, raw_output)]。

    每轮: 每个模型评审其他方案(挑缺陷) → 每个模型吸收对自己的点评修订方案。
    收敛: 本轮评审 vs 上轮评审相似度 > 0.85 视为无新缺陷, 或达 max_rounds 硬上限。
    """
    import concurrent.futures
    import difflib

    plans = {m: o for m, o in members}
    models = [m for m, _ in members]
    agent_by_model = {a.get("model"): a for a in chain}
    # C: 慢模型只出初稿, 不评审/不修订; 快模型正常辩论(兜底: 全慢则都参与)
    slow = {m for m in models if _is_slow_model(m)}
    reviewers = [m for m in models if m not in slow] or models
    prev_review = ""

    for rnd in range(1, max_rounds + 1):
        # ── 阶段A: 交叉评审(并行) ──
        def _review(reviewer):
            others = [(m, plans[m]) for m in models if m != reviewer]
            # 完整传入他人方案, 不漏评后半部分缺陷 (输出仅点评 2-4 条, 输入完整划算)
            parts = "\n\n".join(f"【{m}】\n{p}" for m, p in others)
            prompt = (f"你是架构委员会成员，正在评审其他成员的方案。\n"
                      f"任务背景:\n{task}\n\n{parts}\n\n"
                      f"请逐一点评每位成员的方案，指出缺陷、遗漏、风险、可补充点。"
                      f"用「【成员名】点评：...」格式，每位 2-4 条，简洁。")
            return reviewer, _run_no_tools(agent_by_model.get(reviewer), prompt,
                                           f"{task_id}_rev_{reviewer[:6]}_{rnd}",
                                           level, baseline_ref, cwd)

        review_map = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(reviewers), 4)) as ex:
            futs = {ex.submit(_review, m): m for m in reviewers}
            done, _ = concurrent.futures.wait(futs, timeout=300)
            for fut in done:
                reviewer, r = fut.result()
                if r:
                    review_map[reviewer] = r
        review_text = "\n".join(f"评审({m}):\n{r}" for m, r in review_map.items())

        # ── 阶段B: 修订(并行) ──
        def _revise(model):
            my_plan = plans[model]
            others_review = "\n".join(
                f"来自 {rv} 的点评:\n{r}" for rv, r in review_map.items() if rv != model)
            # 增量修订: 模型只输出补丁(改动)。方案与点评完整传入——补丁 path 依赖看到全文，
            # 截断会让模型盲猜路径; 输出已从 20k 缩到 2k, 输入完整是划算的。
            prompt = (f"这是你的架构方案(JSON):\n{my_plan}\n\n"
                      f"其他成员对你方案的点评:\n{others_review}\n\n"
                      f"请吸收合理意见，输出 RFC 6902 JSON Patch 描述改动，不要重复原方案全文。\n"
                      f"每条形如 {{\"op\":\"replace|add|remove\",\"path\":\"/tasks/0/description\",\"value\":\"...\"}}，\n"
                      f"只列出需要改的字段，直接输出补丁数组。")
            return model, _run_no_tools(agent_by_model.get(model), prompt,
                                        f"{task_id}_rvs_{model[:6]}_{rnd}",
                                        level, baseline_ref, cwd)

        new_plans = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(reviewers), 4)) as ex:
            futs = {ex.submit(_revise, m): m for m in reviewers}
            done, _ = concurrent.futures.wait(futs, timeout=300)
            for fut in done:
                model, r = fut.result()
                if r:
                    new_plans[model] = apply_json_patch(plans[model], r)
        plans.update(new_plans)  # 只更新快模型; 慢模型保留初稿

        # ── 收敛判定 ──
        if prev_review and review_text and difflib.SequenceMatcher(None, prev_review, review_text).ratio() > 0.85:
            break
        prev_review = review_text

    return [(m, plans[m]) for m in models if m in plans]


def _dispatch_committee(task: str, level: str, task_id: str, agents: dict,
                        chain: list[dict], feedback: str = "",
                        baseline_ref: str = "", cwd: str = "") -> DispatchResult:
    """多模型委员会: 所有可用D模型并行产出→合成。"""
    import concurrent.futures

    # 席位视角: 按成员顺序轮转分配, 不依赖模型名(任何模型组合都能碰撞出差异)。
    # ponytail: 顺序轮转够用; 若要"按模型特性自适应分席"再优化。
    # QIDIAN_COMMITTEE_NO_PERSPECTIVE=1 时关闭视角注入(无视角基线, 用于 A/B 评测多视角价值)
    if os.environ.get("QIDIAN_COMMITTEE_NO_PERSPECTIVE") == "1":
        _PERSPECTIVES = [None, None, None, None]
    else:
        _PERSPECTIVES = [
            "你关注: 风险点、边界条件、回滚策略。方案必须稳健,不能炸。",
            "你关注: 有没有完全不同的思路?业界最新实践是什么?大胆提替代方案。",
            "你关注: 这方案能落地吗?需要多少个文件?现有代码风格兼容吗?复杂度实际是多少?",
            "你关注: 和现有架构的一致性。不要引入不兼容的变更。",
        ]

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
        done, _ = concurrent.futures.wait(futures, timeout=300, return_when='ALL_COMPLETED')
        for fut in done:
            agent_cfg = futures[fut]
            try:
                raw = fut.result()
                if raw:
                    outputs.append((agent_cfg.get("model", "?"), raw))
            except Exception:
                pass  # 单个模型失败不阻断委员会

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
    from .execution_judge import _is_architecture_task, fuse_architecture

    # 多轮辩论: 架构任务且 ≥2 产出 → 交叉评审收敛(互相补充缺陷)
    if _is_architecture_task(task):
        try:
            outputs = _debate(task, outputs, chain, task_id, level,
                              baseline_ref=baseline_ref, cwd=cwd)
        except Exception:
            pass  # 辩论失败 → 用初稿继续融合

    if _is_architecture_task(task):
        # 架构方案: 两阶段 fusion (Step 2)
        raw_outputs = [o for _, o in outputs]
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



