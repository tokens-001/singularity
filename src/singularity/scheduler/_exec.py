"""内部模块 — 核心执行引擎。

纯执行: dispatch + validate + trace。worker 线程安全，不写 tracker。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from pathlib import Path

from singularity.scheduler._types import RunContext, BatchOutput, _SnapProxy, _pending_sse_events
from singularity.scheduler._worktree import (
    _maybe_create_worktree, _cleanup_wt, _lock_wt, _unlock_wt,
    _anchor_ref, _build_merge_request,
)
from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import router as router_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import tracker
from singularity.scheduler import validator as val_mod
from singularity.scheduler import neijinglu as nj_mod
from singularity.scheduler import witness
from singularity.scheduler import memory as mem_mod
from singularity.scheduler import pre_search as pre_mod
from singularity.scheduler import chancellor as chan_mod
from singularity.scheduler.log import timed
from singularity.scheduler._git_worktree import (
    Worktree, create as wt_create, cleanup as wt_cleanup,
    merge_back as wt_merge_back, commit_wt, changed_files_between,
)
from singularity.scheduler.tracker import TaskStatus

# ponytail: context 函数提取到 _exec_context.py, 此文件 re-export 保持兼容
from singularity.scheduler._exec_context import (
    _PLANNER_PREAMBLE, _inject_memory, _build_project_context,
    _CONSTRUCT_WINDOW, _summarize_events, _construct_context,
)

# 全局底线：所有任务的约束，首轮无条件注入（原 karpathy-rules 技能，从技能层提升到全局层）
_GLOBAL_CONSTRAINTS = """全局底线（所有任务必须遵守）：
1. 不自作主张：不确定需求先澄清，不猜测；只做需求明确要求的
2. 简洁优先：用最少代码解决问题，优先标准库，不引入不必要的抽象
3. 精准修改：只改任务直接相关的代码，不顺手重构，diff 只含必要变更
4. 目标驱动：先定位根因再动手，改完验证，任务完成就停
5. 错误处理：输入为空、异常输入、边界情况都要处理，不留裸奔路径
6. 输出规范：多文件改动时，每个文件一个代码块，开头注明文件路径（如 <!-- @files: a.py,b.py -->）
"""


def _build_effective_task(task, turn: int, feedback: str, is_planner: bool,
                          tool_events: list = None, route_role: str = "") -> str:
    """拼接最终 prompt: 记忆注入 + 角色上下文 + planner preamble + 项目上下文。

    Step 4: route_role 非空时注入角色 system_prompt。
    """
    effective_task = task.description
    # ── 全局底线：所有任务首轮无条件注入 ──
    if turn == 1:
        effective_task = _GLOBAL_CONSTRAINTS + "\n\n" + effective_task
    # ── Step 4: 角色上下文注入 (首轮) ──
    if turn == 1 and route_role:
        role_ctx = _inject_role_context(route_role)
        if role_ctx:
            effective_task = role_ctx + "\n\n---\n" + effective_task
    # ── MAGMA 记忆注入 (仅首轮、无打回反馈时) ──
    if turn == 1 and feedback == "":
        mem_ctx = _inject_memory(task.description)
        if mem_ctx:
            effective_task = mem_ctx + "\n\n" + effective_task
    # ── ConstructContext: 工具历史裁剪 (turn≥2 且有工具事件时) ──
    if tool_events:
        ctx_ctx = _construct_context(tool_events, turn)
        if ctx_ctx:
            effective_task = ctx_ctx + "\n" + effective_task
    if is_planner:
        effective_task = _PLANNER_PREAMBLE + effective_task
    # ── 项目上下文注入 ──
    proj_ctx = _build_project_context(task)
    if proj_ctx:
        effective_task = proj_ctx + "\n\n---\n" + effective_task
    return effective_task


def _inject_role_context(route_role: str) -> str:
    """Step 4: 从 roles.toml 加载角色（system_prompt + persona 人格）作为执行上下文。"""
    try:
        from singularity.scheduler.roles import get_role
        role = get_role(route_role)
        if role:
            return role.get_full_prompt()
    except Exception as e:
        # 静默返回 "" = agent 悄悄丢掉角色提示词，行为差异在外面完全看不见
        witness.warn('orch', f'role_context:{route_role}:{e}'[:80])
    return ""


def _check_cancelled(task, all_tool_events: list) -> "BatchOutput | None":
    """检查人工取消标记。返回 BatchOutput 表示已取消; None 表示继续。"""
    cancel_path = config.CANCEL_DIR / f"{task.id}.json"
    if cancel_path.exists():
        cancel_path.unlink()
        return BatchOutput(
            ok=False, task_id=task.id,
            term_reason="cancelled_by_user",
            validation=val_mod.ValidationReport(
                verdict="阻断", action="abort",
                unverified=["用户手动取消"],
            ),
            tool_events=all_tool_events, turn_count=0,
        )
    return None


def _check_paused(task) -> bool:
    """检查人工暂停标记。有暂停信号→切 PAUSED 状态→阻塞等待恢复。

    confirm_changes 模式下, 每 turn 自动写暂停信号 (用户每步确认)。
    返回 True 表示已恢复继续; False 表示任务已终止。
    """
    from singularity.scheduler import tracker as tracker_mod

    # confirm_changes: 每 turn 自动暂停 (用户点了 resume 后下个 turn 再暂停)
    mode = getattr(task, 'execution_mode', 'auto_edit') or 'auto_edit'
    pause_path = config.PAUSE_DIR / f"{task.id}.json"
    if mode == "confirm_changes" and not pause_path.exists():
        config.ensure_dirs()
        pause_path.write_text(json.dumps({"task_id": task.id, "paused_at": time.time(), "auto": True}),
                            encoding="utf-8")

    if not pause_path.exists():
        return True  # 无暂停信号, 继续执行

    # 切到 PAUSED 状态
    tracker_mod.transition(task.id, tracker_mod.TaskStatus.PAUSED)

    # 阻塞等待: 轮询检测 pause 文件被删除=恢复信号
    import time as _time
    while pause_path.exists():
        _time.sleep(1)
        # 期间如果任务被取消, 退出等待
        cancel_path = config.CANCEL_DIR / f"{task.id}.json"
        if cancel_path.exists():
            return False

    # 恢复: 切回 RUNNING
    tracker_mod.transition(task.id, tracker_mod.TaskStatus.RUNNING)
    return True


def _process_planner_or_merge(task, ctx, turn, level, is_planner, wt,
                              exec_result, disp_result, all_tool_events,
                              pending_merge_req_holder: list, repo_root=None):
    """处理 executor 成功后的 planner 分解 / v3-v2 merge 分支。

    返回信号:
      None  → 继续 validate
      BatchOutput → 直接返回此结果 (planner 分解成功 / v2 merge 冲突)

    pending_merge_req_holder 是单元素 list, 用于 v3 路径回填 merge_request (保持引用语义)。
    """
    if is_planner:
        _save_planner_patch(task.id, exec_result.raw_output)
        subtasks = decompose(exec_result.raw_output)
        if subtasks:
            return BatchOutput(
                ok=True, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"decomposed (level={level}, turn={turn})",
                validation=val_mod.ValidationReport(
                    verdict="通过", action="pass",
                    unverified=[f"planner 分解出 {len(subtasks)} 子任务"],
                ),
                planner_decomposed=True,
                planner_subtasks=subtasks,  # 传给主线程, 避免二次解析
                tool_events=all_tool_events, turn_count=turn,
            )
    elif wt:
        if ctx.merge_queue is not None:
            # v3: commit_wt 拿含改动的 commit (修复 #2), 不直接 merge
            branch_ref = commit_wt(wt)
            if branch_ref:
                _anchor_ref(task.id, branch_ref, repo_root=repo_root)  # 防 gc 回收 (重要 #3)
                pending_merge_req_holder[0] = _build_merge_request(
                    task, branch_ref, ctx.snapshot_ref, repo_root=repo_root,
                )
        else:
            # v2: 直接 merge_back
            mr = wt_merge_back(wt, repo_root=repo_root)
            if not mr.ok:
                reason = mr.reason or f"冲突文件: {mr.conflicts}"
                return ("merge_conflict", level, turn, disp_result, all_tool_events, reason)
    return None


def _decide_cascade(task, level, turn, validation, disp_result, all_tool_events,
                    pending_merge_req, fallback_chain, tried_models, quality):
    """cascade routing 决策。

    返回 (action, payload):
      ("return", BatchOutput)  → 直接返回 (pass / cascade_accept / 非 retry 终态)
      ("break", None)          → 跳出 turn loop, 升级或换 agent (finally 清 wt)
      ("continue", feedback)   → 中置信 retry, 复用同一 wt
    """
    if validation.action == "pass":
        return ("return", BatchOutput(
            ok=True, task_id=task.id, dispatch_result=disp_result,
            term_reason=f"pass (level={level}, turn={turn})",
            validation=validation,
            merge_request=pending_merge_req,
            tool_events=all_tool_events, turn_count=turn,
        ))

    if validation.action == "retry":
        # 软质量硬门槛: 首轮软修复 (turn=1 retry), 触顶 (turn>=2) 仍软伤 → 不静默放行, 标失败升人工
        if quality.get("failure_kind") == "soft_quality" and turn >= 2:
            return ("return", BatchOutput(
                ok=False, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"soft_quality_gate (软质量软修一轮未达标, level={level}, turn={turn})",
                validation=validation, merge_request=pending_merge_req,
                tool_events=all_tool_events, turn_count=turn,
            ))
        conf = validation.confidence
        # 高置信 → 跳过升级，接受当前结果 (省钱)
        if conf >= 0.75:
            return ("return", BatchOutput(
                ok=True, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"cascade_accept (level={level}, conf={conf:.2f})",
                validation=validation, merge_request=pending_merge_req,
                tool_events=all_tool_events, turn_count=turn,
            ))
        # 低置信 + 还有更高级模型 → 立即升级，不浪费重试
        if conf < 0.35 and len(fallback_chain) > 1:
            return ("break", None)
        # 中置信 → 正常重试（给同一个模型改进机会, 复用同一 wt）
        fb_parts = [json.dumps(validation.evidence, ensure_ascii=False, indent=2)]
        if quality.get("warnings"):
            fb_parts.append("质量警告:\n" + "\n".join(f"- {w}" for w in quality["warnings"]))
        if quality.get("failure_kind") and quality["failure_kind"] != "ok":
            fb_parts.append(f"失败类型: {quality['failure_kind']}, 置信度: {quality['confidence']:.2f}")
        return ("continue", "\n\n".join(fb_parts))

    # rollback / abort 等非 retry 终态
    return ("return", BatchOutput(
        ok=False, task_id=task.id, dispatch_result=disp_result,
        term_reason=f"{validation.action} (level={level}, turn={turn})",
        validation=validation,
        tool_events=all_tool_events, turn_count=turn,
    ))


@timed(name="executor")
def run(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """纯执行: dispatch + validate, 返回 BatchOutput。

    修复 #7: 不调任何 tracker.transition/cas/create。调用方 (主线程) 负责状态机。
    修复 #5: 入口 _read(task.id) 重读, 不依赖传入的内存 Task 对象 (可能陈旧)。
    修复 #2: v3 路径用 commit_wt 拿到含改动的 commit, 再构造 MergeRequest。
    """
    # 修复 #5: 重读文件, 不信任传入的 task 内存对象
    fresh = tracker.read_task(task.id)
    if fresh is not None:
        task = fresh

    level = task.route_level
    route_gate = task.route_gate
    route_type = task.route_type
    # Step 4: 读取 layer→角色路由
    route_role = getattr(task, 'route_role', None) or ""

    feedback = ""
    last_validation = val_mod.ValidationReport(
        verdict="未知", action="abort",
        unverified=["dispatcher 未产出可校验结果"],
    )
    disp_result = None
    term_reason = "未执行"
    pending_merge_req = None
    planner_decomposed = False
    all_tool_events: list[dict] = []  # 收集所有 turn 的工具调用事件
    final_turn = 0                     # 实际推理轮次
    qa_verdict = ""                    # worker 内 QA 门禁判定, 随 batch 带回给 finalize 复用
    qa_issues: list = []

    # method 必须透传：审查层靠它判这个 ref 能不能当 diff 基准。漏了它，
    # `_diff_base` 恒返回空串 → 审查五道检查一起短路（2026-09-11 外派评审抓到的 P0）。
    snap = _SnapProxy(ctx.snapshot_ref, method=ctx.snapshot_method)

    # ── 执行前钩子 ──
    pre_warnings = val_mod.pre_execution_hook(task.description, snap)
    if pre_warnings:
        for w in pre_warnings:
            witness.warn("exec", f"pre_hook: {w[:80]}"[:200])

    # 「阶段 → 模型」里实现阶段配的那份名单。**这里和下面 :350 的 dispatch 要各传一次** ——
    # 本处这次只决定"用哪个 agent 建 worktree / 熔断后切谁"，真正调模型的是 dispatch()，
    # 而它在 _dispatch_exec 里会**自己重新选一遍链**。只传一处 → 指定的主力只生效一半
    # （外层按 A 建 worktree，内层实际调 B）。
    from . import phase_models as pm_mod
    _proj = None
    if getattr(task, "project_id", ""):
        try:
            from . import project as proj_mod
            _proj = proj_mod.load(task.project_id)
        except Exception:
            _proj = None                     # 项目读不到就当没配，别把执行拖挂
    exec_lineup, exec_restrict = pm_mod.selection("executing", _proj)

    # 容灾: 获取 fallback 链, 当前 agent 失败自动切下一个
    # 如果任务已重试多次，强制优先用 premium 模型
    force_premium = getattr(ctx, 'retry_count', 0) >= 2
    fallback_chain = disp_mod.pick_agent_fallback_chain(
        agents, level, fallback_levels=["any"],
        project_lineup=exec_lineup, restrict_to_lineup=exec_restrict)
    if force_premium and fallback_chain and not exec_restrict:
        # 受限时跳过：用户点名的主力不该因为"重试过两次"被换成别的
        # 把 premium 模型移到最前面 (model 名含 glm 或 opus)
        premium = [a for a in fallback_chain if any(p in a.get('model','').lower() for p in ('glm','opus'))]
        cheap = [a for a in fallback_chain if a not in premium]
        fallback_chain = premium + cheap
    tried_models: set[str] = set()

    # 修复 #1: 项目任务写进项目独立 repo, 独立任务写奇点仓库
    from . import project as proj_mod
    repo_root = proj_mod.repo_root_for(task)

    while True:
        if not fallback_chain:
            break
        agent_cfg = fallback_chain[0]
        level_max = config.DEFAULT_MAX_TURNS  # 打回上限；agent 的 max_turns 是模型推理轮数(openai_agent 内部用)，语义不同
        is_planner = agent_cfg.get("mode") == "planner"

        wt = _maybe_create_worktree(task.id, level, agent_cfg, ctx.snapshot_ref, repo_root=repo_root)
        cwd = str(wt.path) if wt else str(repo_root)  # ponytail: 无worktree时直接用仓库根

        # 修复 P1-1: worktree 生命周期对称。
        # try/finally 套在 while 迭代体内（非函数级）——fallback 切 agent 会重建 wt,
        # 每个 wt 必须在本迭代结束（return/break/异常）时清理；
        # 只有 retry 的 continue 复用同一 wt（不退出 try，不触发 finally）。
        try:
            if is_planner and wt:
                _lock_wt(wt)

            for turn in range(1, level_max + 1):
                final_turn = turn          # P3 修复: 失败兜底不再恒报 0 轮
                witness.heartbeat(task.id, level)

                # 检查人工取消标记
                cancelled = _check_cancelled(task, all_tool_events)
                if cancelled is not None:
                    return cancelled

                # 检查人工暂停标记 (GATE 人审)
                if not _check_paused(task):
                    # pause 期间被 cancel 了
                    return _check_cancelled(task, all_tool_events) or BatchOutput(
                        ok=False, task_id=task.id, term_reason="cancelled_during_pause",
                        validation=val_mod.ValidationReport(verdict="阻断", action="abort",
                            unverified=["暂停期间被取消"]),
                        tool_events=all_tool_events, turn_count=turn,
                    )

                effective_task = _build_effective_task(task, turn, feedback, is_planner,
                                                        tool_events=all_tool_events,
                                                        route_role=route_role)

                disp_result = disp_mod.dispatch(
                    effective_task, level, task.id, agents,
                    feedback=feedback, baseline_ref=ctx.snapshot_ref, cwd=cwd,
                    project_lineup=exec_lineup, restrict_to_lineup=exec_restrict,
                    # 角色标：planner 拆的子任务带"执行阶段角色"，dispatch 靠它
                    # 把实现任务挡在委员会外面（描述里带架构词汇不等于要出架构）。
                    route_role=route_role,
                    # 阶段名：技能解析用它走"阶段级绑定"那条轴（换模型不丢技能）。
                    phase="executing",
                )
                exec_result = disp_result.executor_result

                # ── 收集工具调用事件 ──
                if exec_result and getattr(exec_result, 'tool_events', None):
                    all_tool_events.extend(exec_result.tool_events)

                if not exec_result.success:
                    # 容灾: 切下一个 agent
                    tried_models.add(agent_cfg.get("model", ""))
                    fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                    if fallback_chain:
                        witness.warn("exec", f"fallback: {agent_cfg.get('model','')}→{fallback_chain[0].get('model','')}"[:200])
                        break  # 跳出 turn loop, 用新 agent (finally 清理本 wt)
                    last_validation = val_mod.ValidationReport(
                        verdict="未知",
                        action="abort",
                        unverified=[f"executor 失败 (已试 {len(tried_models)} agent): {exec_result.error_kind}: {exec_result.error}"],
                        turns_used=turn,
                    )
                    break

                # planner 分解 / v3-v2 merge 处理
                # pending_merge_req_holder: 单元素 list, 让子函数能回填 v3 的 merge_request
                pending_merge_req_holder = [pending_merge_req]
                pm_signal = _process_planner_or_merge(
                    task, ctx, turn, level, is_planner, wt,
                    exec_result, disp_result, all_tool_events,
                    pending_merge_req_holder, repo_root=repo_root,
                )
                pending_merge_req = pending_merge_req_holder[0]
                if pm_signal is not None:
                    if isinstance(pm_signal, BatchOutput):
                        return pm_signal  # planner 分解成功
                    # v2 merge 冲突信号: ("merge_conflict", level, turn, disp_result, all_tool_events, reason)
                    if isinstance(pm_signal, tuple) and len(pm_signal) >= 6:
                        _, level, turn, disp_result, all_tool_events, reason = pm_signal
                    else:
                        last_validation = val_mod.ValidationReport(verdict="阻断", action="abort",
                            unverified=[f"内部错误: 意外的 _process_planner_or_merge 返回类型"])
                        break  # 跳出 for turn 循环，走 escalation 路径
                    last_validation = val_mod.ValidationReport(
                        verdict="阻断", action="abort",
                        unverified=[f"worktree merge 失败: {reason}"],
                        turns_used=turn,
                    )
                    term_reason = f"merge_conflict (level={level}, turn={turn})"
                    return BatchOutput(
                        ok=False, task_id=task.id, dispatch_result=disp_result,
                        term_reason=term_reason, validation=last_validation,
                        tool_events=all_tool_events, turn_count=turn,
                    )

                validation = val_mod.validate(
                    candidate=exec_result.raw_output,
                    gate_required=route_gate,
                    task_type=route_type,
                    changed_files=getattr(exec_result, 'changed_files', []),
                    snap=snap, turn=turn, max_turns=level_max,
                    cwd=cwd,
                )
                # 补充质量信号
                try:
                    quality = val_mod.post_execution_hook(exec_result, snap)
                    validation.confidence = quality.get("confidence", 0.5)
                    validation.quality_signals = quality.get("quality_signals", {})
                except Exception:
                    quality = {"warnings": [], "failure_kind": "ok", "confidence": 0.5}

                # ── v2: independent tests + multi-model review ──
                changed = getattr(exec_result, 'changed_files', []) or []
                if changed:
                    from . import _review as rev_mod
                    rev_mod.run_post_exec_checks(
                        validation=validation, quality=quality,
                        exec_result=exec_result, task=task,
                        agent_cfg=agent_cfg, level=level, cwd=cwd,
                        changed=changed,
                        # 执行前快照的 ref 当 diff 基准。不传的话审查什么都看不到 ——
                        # worktree 里改动在 validate 之前就被 commit_wt 提交了。
                        base_ref=val_mod._diff_base(snap))

                # 审查结论接回决策: run_post_exec_checks 只改 quality["confidence"],
                # 而 _decide_cascade 读 validation.confidence (审查前就赋了值) → 惩罚传不到,
                # review_critical 仍可能被 cascade_accept (conf>=0.75) 放行合并。
                # 取二者较小值: 无发现时 quality 更高, min 取原值 → 行为不变。
                validation.confidence = min(
                    validation.confidence, quality.get("confidence", validation.confidence))

                last_validation = validation

                # ── QA 门禁 (supervisor): 硬证据失败 → 不合并 ──
                # 位置: merge_request 已构造、_decide_cascade 之前。清掉 merge_request
                # 后代码进不了主仓库 (v3 不 submit / v2 不 merge_back)。
                # 只拦 verdict=fail (硬证据); escalate/retry 是软信号, 不拦, 仅 SSE 通知 Owner。
                if pending_merge_req is not None:
                    try:
                        from .supervisor import supervise, qa_context
                        _cons, _check = qa_context(task)
                        # 改动文件在 worktree (cwd) 里, 不在项目 repo 根 —— 传错根
                        # 会让 _check_artifact 的 py_compile/ruff 因 (root/f).exists()
                        # 为假而静默跳过 (端到端实测: 语法错误文件被放行合并)
                        sv = supervise(task.description, changed, _cons, _check,
                                       getattr(exec_result, 'raw_output', '') or '',
                                       task.id, repo_root=cwd,
                                       tests_result=(quality or {}).get("test_result"))
                        qa_verdict = sv.verdict
                        qa_issues = list(sv.issues)
                        # "block" 必须和 "fail" 同等对待：supervise 在**模型隔离违规**
                        # （supervisor 与 implementer 同模型）时返回 "block"，注释写明是"硬锁"。
                        # 但消费端原来只认 "fail" / ("escalate","retry") —— "block" 两条都不中，
                        # 直接穿过去、**合并照走**，那个硬锁从来没生效过。
                        if sv.verdict in ("fail", "block"):
                            pending_merge_req = None
                            quality.setdefault("warnings", []).append(
                                ("QA 阻断: " if sv.verdict == "block" else "QA 硬证据失败: ")
                                + "; ".join(sv.issues[:2] or [getattr(sv, "reason", "")]))
                            quality["failure_kind"] = "qa_fail"
                            # 压低置信度, 否则 _decide_cascade 的 cascade_accept 会直接放行
                            validation.confidence = min(validation.confidence, 0.3)
                            validation.action = "retry" if turn < level_max else "abort"
                            validation.unverified.append(
                                "QA 硬证据失败 (未合并): " + "; ".join(sv.issues[:2]))
                        elif sv.verdict in ("escalate", "retry"):
                            _pending_sse_events.append({
                                "kind": "system",
                                "msg": f"[{task.id[:8]}] QA 软信号 {sv.verdict} (不拦合并): "
                                       + "; ".join(sv.issues[:1]),
                                "ts": time.time(), "task_id": task.id,
                            })
                    except Exception as e:
                        witness.warn(task.id, f"qa_gate:{e}")

                cascade_action, payload = _decide_cascade(
                    task, level, turn, validation, disp_result, all_tool_events,
                    pending_merge_req, fallback_chain, tried_models, quality,
                )
                if cascade_action == "return":
                    payload.qa_verdict = qa_verdict
                    payload.qa_issues = qa_issues
                    return payload
                if cascade_action == "break":
                    # 低置信 cascade_skip: 标记 tried 后 break 升级
                    tried_models.add(agent_cfg.get("model", ""))
                    fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                    if fallback_chain:
                        witness.warn("exec", f"cascade_skip:{agent_cfg.get('model','')}→{fallback_chain[0].get('model','')} conf={validation.confidence:.2f}"[:200])
                    break  # 跳出 turn loop，用更好的模型 (finally 清理本 wt)
                # cascade_action == "continue": 中置信 retry
                feedback = payload
                continue

            next_level = disp_mod.escalate(level)
            if next_level is None:
                # escalate() **恒返回 None** —— `_ESCALATION` 是空表且全仓无人填，
                # 也就是说"升级到下一档"这条设计路径从未生效过。
                # 原来无论内层因为什么 break 到这里，终态一律写 escalation_exhausted，
                # 把真实原因（低置信 / cascade 跳过 / executor 全失败）从终态上抹掉了 ——
                # 排障时只看得到"升级用尽"，看不到"其实根本没有升级档"。
                # 带上最后一次校验的 action，让终态说真话。
                _why = getattr(last_validation, "action", "") or "unknown"
                term_reason = f"no_escalation_path (level={level}, last_action={_why})"
                break
            level = next_level
            # 升级后重建 fallback 链 (新层级的新 agent 列表)
            fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level)
            tried_models = set()
            feedback = ""
        finally:
            _cleanup_wt(wt)

    return BatchOutput(
        ok=False, task_id=task.id, dispatch_result=disp_result,
        term_reason=term_reason, validation=last_validation,
        tool_events=all_tool_events, turn_count=final_turn,
        qa_verdict=qa_verdict, qa_issues=qa_issues,
    )

def _run_with_retry(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """worker 线程入口: 纯执行 + 重试。

    修复 #7: 不写 tracker。重试时回传 retry 信号, 主线程决定是否再派发。
    本函数在 worker 线程跑, 只调 run() (纯执行), 不碰 tracker。
    """
    retry = 0
    while retry <= task.max_retries:
        ctx.retry_count = retry  # ponytail: 传入 run() 用于 force_premium 判定
        batch = run(task, ctx, agents)

        # ── 执行后钩子 ──
        try:
            exec_result = batch.dispatch_result.executor_result if batch.dispatch_result else None
            if exec_result:
                snap = snap_mod.Snapshot(id=task.id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                post_warnings = val_mod.post_execution_hook(exec_result, snap)
                if post_warnings and post_warnings.get("warnings"):
                    batch.term_reason += f"; post_hook: {', '.join(post_warnings['warnings'])}"
        except Exception as e:
            try: witness.warn(task.id, f"post_hook:{e}")
            except Exception: pass

        if batch.ok or batch.planner_decomposed:
            return batch
        if batch.term_reason.startswith(("merge_conflict", "soft_quality_gate")):
            return batch

        retry += 1
        if retry > task.max_retries:
            return batch

        # 重试 (v2: 主仓库可能有 merge 残留; v3: worktree 已在 run() 内部清理)
        if ctx.merge_queue is None:
            # v2: 主仓库 rollback 到快照基线 (只在主线程, 不并发)
            try:
                snap = snap_mod.Snapshot(id=ctx.batch_id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                from . import project as proj_mod
                snap_mod.rollback(snap, repo_root=proj_mod.repo_root_for(task))
            except Exception as e:
                witness.warn('exec', f'{e}')
        # v3: 不碰 PROJECT_ROOT —— 主仓库未动, worktree 已由 run() 内部 _cleanup_wt 清理
        # retry_count 由主线程在回收时按需写; worker 不写

    return batch


def _save_planner_patch(task_id: str, content: str) -> None:
    patch_path = config.PATCH_DIR / f"{task_id}_plan.md"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(content, encoding="utf-8")


def _read_planner_patch(task_id: str) -> str | None:
    """读 D 层的分析方案 patch，用于创建 E+ 修复任务。"""
    patch_path = config.PATCH_DIR / f"{task_id}_plan.md"
    if not patch_path.exists():
        return None
    try:
        return patch_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _save_trace(task, route, snap, disp_result, validation, rolled_back: bool,
                pre_search_skipped: bool = False, pre_search_reason: str = "",
                pre_search_top_decisions: list = None, pre_search_memory: dict = None) -> None:
    # ponytail: 幂等保护 — 终态路径互斥但防误调用
    trace_path = config.TRACE_DIR / f"{task.id}.json"
    if trace_path.exists():
        return  # 已写过的 trace 不覆盖, 避免不一致
    try:
        report = nj_mod.build_report(
            task=task.description, route=route,
            executor_result=disp_result.executor_result if disp_result else None,
            validation=validation, snapshot=snap, rolled_back=rolled_back,
            pre_search_skipped=pre_search_skipped,
            pre_search_reason=pre_search_reason,
            pre_search_top_decisions=pre_search_top_decisions,
            pre_search_memory=pre_search_memory,
        )
        nj_mod.save_trace(report, task.id)
    except Exception as e:
        witness.warn('exec', f'{e}')

    # ── MAGMA 多图记忆索引 + 状态更新 ──
    try:
        changed_files = disp_result.executor_result.changed_files if disp_result else []
        mem_mod.index_task(
            task_id=task.id,
            description=task.description,
            changed_files=changed_files,
            depends_on=task.depends_on,
            created_at=task.created_at,
        )
        # 补充事件属性: 终态 + route info
        # route_level 取自 task 而非 route —— RouteResult 没有 level 字段（两档制后
        # E/E+/D 已废弃，见 pre_search.apply_escalation 的注释）。曾经读 route.level，
        # 每任务必抛 AttributeError 被下面那层 except 吞掉，于是这条 update_attrs 从没执行过。
        final_status = "rolled_back" if rolled_back else task.status.value
        mem_mod.update_attrs(task.id,
            status=final_status,
            route_level=task.route_level,
            route_type=route.task_type if route else "",
        )
    except Exception as e:
        witness.warn('exec', f'{e}')


def _safe_dep_list(v):
    """depends_on_local_id: int or list[int] → list[int]."""
    if isinstance(v, int): return [v]
    if isinstance(v, list): return v
    return []


def decompose(planner_raw_output: str) -> list[dict]:
    """解析 planner stdout 里的 ```json 子任务块。

    返回 [{desc, suggested_level, depends_on_local_id}, ...]。
    无 JSON 块或解析失败 → [] (当普通方案, 不分解)。
    """
    import re as _re
    # 抓 ```json ... ``` 块
    m = _re.search(r"```json\s*\n(.*?)\n```", planner_raw_output, _re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    # 校验每条结构
    subtasks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if "desc" not in item:
            continue
        subtasks.append({
            "desc": str(item["desc"]),
            "suggested_level": str(item.get("suggested_level", "any")),
            "depends_on_local_id": _safe_dep_list(item.get("depends_on_local_id", [])),
        })
    return subtasks


