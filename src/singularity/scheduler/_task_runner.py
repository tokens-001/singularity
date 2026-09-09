"""_task_runner.py — 单任务生命周期 (架构 #1.1)。

封装全链路: route → pre_search → execute → judge → profile → trace → QA → chancellor。

类:
  TaskRunner — execute(task, agents) → (batch, route, snap)
            — finalize(task, batch, route, snap, results) → reason

ponytail: 零新增行为，纯搬迁从 orchestrator.py。
"""

from __future__ import annotations

import logging
import re as _re
import time
from pathlib import Path

# ── 内部模块 (执行链) ──────────────────────────────────────
from singularity.scheduler._types import (
    RunContext, BatchOutput, _SnapProxy, _MAX_DEPTH, _pending_sse_events,
)
from singularity.scheduler._exec import (
    _PLANNER_PREAMBLE, _inject_memory, _build_project_context,
    run, decompose, _run_with_retry,
    _save_trace, _save_planner_patch, _read_planner_patch,
)
from singularity.scheduler._worktree import (
    _maybe_create_worktree, _cleanup_wt, _lock_wt, _unlock_wt,
    _anchor_ref, _release_ref, _build_merge_request,
)
from singularity.scheduler._planner import (
    materialize_plan, _topo_sort, _materialize_in_main,
    _maybe_complete_parents,
)
from singularity.scheduler.goal_loop import GoalLoop

_GOAL_RE = _re.compile(r'^\[Goal\]\s*(.+?)\n', _re.ASCII)

# ── 画像 ──────────────────────────────────────────
from singularity.scheduler._token_budget import record_tokens, get_usage_stats
from singularity.scheduler._profiler import get_perf_stats

# ── 业务依赖 ────────────────────────────────────────────
from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import router as router_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import tracker
from singularity.scheduler import validator as val_mod
from singularity.scheduler import neijinglu as nj_mod
from singularity.scheduler import witness
from singularity.scheduler import memory as mem_mod
from singularity.scheduler import route_learner as rl_mod
from singularity.scheduler import pre_search as pre_mod
from singularity.scheduler import chancellor as chan_mod
from singularity.scheduler._git_worktree import (
    Worktree, create as wt_create, cleanup as wt_cleanup,
    merge_back as wt_merge_back, commit_wt, changed_files_between,
)
from singularity.scheduler.tracker import TaskStatus

# ── 单例 ─────────────────────────────────────────────────

def _reorder_agents_by_rank(agents_list: list, ranked_models: list[str]) -> list:
    """按画像排名重排 agent 列表：排名靠前的模型优先。"""
    rank_map = {m: i for i, m in enumerate(ranked_models)}
    return sorted(
        agents_list,
        key=lambda a: rank_map.get(a.get("model", ""), 999),
    )



# ═══════════════════════════════════════════════════════════════
# TaskRunner
# ═══════════════════════════════════════════════════════════════

class TaskRunner:
    """单任务生命周期。

    封装: route → pre_search → execute → trace → QA。
    orchestrator 只需 import 这一个类。
    """

    def execute(self, task, agents: dict, merge_queue=None):
        """执行单个任务: 路由→预检→Goal/委员会/普通→返回(batch, route, snap)。

        merge_queue: v3 并行时由 _run_queue_v3 传入, 使 _exec.run 走 v3 路径
          (commit_wt + 填 merge_request, 不直接 merge_back)。
          None → v2 路径 (直接 merge_back)。修复 reap bug 根因#1。
        """
        from singularity.scheduler.log import set_trace_id
        set_trace_id(task.id)  # 本任务生命周期内 log_event 都带 trace_id
        # 路由
        if task.route_locked:
            route = router_mod.RouteResult(
                gate_required=task.route_gate,
                task_type=task.route_type)
        else:
            route = router_mod.route(task.description)
        # 预检
        pre = pre_mod.pre_search(task.description, route)
        pre_mod.apply_escalation(route, pre)
        # 模型排名
        project_phase = None
        try:
            pid = getattr(task, "project_id", "")
            if pid:
                from . import project as proj_mod
                proj = proj_mod.load(pid)
                if proj:
                    project_phase = proj.phase.value
        except Exception as e:
            witness.heartbeat('orch', f'warn:{e}')
        # 快照 (修复 #1: 项目任务快照项目 repo)
        from . import project as proj_mod
        snap = snap_mod.take(task.id, repo_root=proj_mod.repo_root_for(task))
        ctx = RunContext(batch_id=task.id, snapshot_ref=snap.ref, merge_queue=merge_queue)
        # ── 代码上下文注入 (codegraph) ──
        if pre.code_context:
            task.description = f"{task.description}\n\n[代码结构上下文]\n{pre.code_context}"

        # 执行分叉: Goal循环 / 普通 (两档后不分层级)
        goal_match = _GOAL_RE.match(task.description)
        if goal_match:
            goal = goal_match.group(1).strip()
            _pending_sse_events.append({
                "kind": "system", "msg": f"Goal循环: {goal[:60]}",
                "ts": time.time(), "task_id": task.id,
            })
            loop = GoalLoop(agents)
            g_result = loop.run(task, goal, max_iter=5)
            from ._types import BatchOutput as _BO
            from .executors.base import ExecutorResult as _ER
            batch = _BO(ok=g_result.success, task_id=task.id,
                        term_reason=f"goal_{'met' if g_result.success else 'exhausted'}_{g_result.iterations}iter",
                        tool_events=[], turn_count=g_result.iterations,
                        validation=val_mod.ValidationReport(
                            verdict="通过" if g_result.success else "阻断",
                            action="pass" if g_result.success else "abort",
                            unverified=[f"Goal循环 {g_result.iterations}轮, 满足={g_result.success}"]))
            batch.dispatch_result = type('obj', (object,), {
                'executor_result': _ER(success=g_result.success, raw_output=g_result.final_output),
                'agent_cfg': {}, 'level': ''})()
        else:
            # 两档后: 从全池选 agent, 不按层级
            batch = _run_with_retry(task, ctx, agents)
        batch.pre_search_skipped = pre.skipped
        batch.pre_search_reason = pre.reason
        batch.pre_search_top_decisions = pre.top_decisions
        batch.pre_search_code_context = pre.code_context
        batch.pre_search_memory = {
            "intent": pre.memory.intent, "narrative": pre.memory.narrative,
            "entity_matches": pre.memory.entity_matches, "graph_coverage": pre.memory.graph_coverage,
        }
        return batch, route, snap

    def finalize(self, task, batch, route, snap, results: list) -> str:
        """后处理: 写终态/trace/QA gate/Chancellor/escalation。返回 reason。"""
        validation = batch.validation
        term_reason = batch.term_reason
        disp_result = batch.dispatch_result
        if batch.planner_decomposed:
            try:
                _materialize_in_main(batch, task)
            except Exception as e:
                witness.heartbeat('orch', f'warn:materialize:{e}')
            reason = f"decomposed: {term_reason}"
        elif batch.ok:
            # DONE 延后到 QA gate 之后 (见下): QA 判 fail/retry 时任务必须还能转 FAILED/PENDING
            reason = f"pass: {term_reason}"
        elif validation.action == "rollback":
            from . import project as proj_mod
            snap_mod.rollback(snap, repo_root=proj_mod.repo_root_for(task))
            tracker.transition(task.id, TaskStatus.ROLLED_BACK,
                             error=f"{validation.verdict}: {term_reason}")
            reason = f"rolled_back: {term_reason}"
        else:
            d_plan = _read_planner_patch(task.id)
            if d_plan and "escalation_exhausted" in term_reason:
                fix_task = tracker.create(
                    f"[D方案执行] {task.description[:80]}",
                    depends_on=[task.id], depth=task.depth)
                tracker.transition(fix_task.id, TaskStatus.PENDING,
                                 route_locked=True)
                tracker.transition(task.id, TaskStatus.FAILED,
                                 error=f"已生成修复任务 {fix_task.id[:8]}: {term_reason}")
                reason = f"auto_fix: {fix_task.id[:8]}"
            else:
                # 降级重试: 重试耗尽 → 自动拆分再提交
                retry_count = getattr(task, 'retry_count', 0)
                if retry_count >= getattr(task, 'max_retries', 3) and task.depth < _MAX_DEPTH:  # ponytail: 安全上限6，达到需人工
                    try:
                        subtasks = decompose(task.description)
                        if subtasks and len(subtasks) > 1:
                            # ── Token 估算 ──
                            try:
                                from ._planner import estimate_tokens
                                est = estimate_tokens(subtasks, task.description)
                                _pending_sse_events.append({
                                    "kind": "token_estimate", "msg": (
                                        f"[{task.id[:8]}] 自动拆分: {est['task_count']}个子任务, "
                                        f"预估 ~{est['total_tokens']:,} tokens (${est['est_cost_usd']:.2f})"
                                    ), "ts": time.time(), "task_id": task.id, "estimate": est,
                                })
                            except Exception as _e:
                                logging.getLogger(__name__).warning("token estimate event failed: %s", _e)
                            child_ids = materialize_plan(task.id, subtasks)
                            tracker.transition(task.id, TaskStatus.DECOMPOSED,
                                error=f"重试{retry_count}次后自动拆分→{len(child_ids)}个子任务")
                            reason = f"auto_decomposed: {len(child_ids)} children"
                        else:
                            tracker.transition(task.id, TaskStatus.FAILED,
                                error=f"重试{retry_count}次仍崩且无法拆分: {term_reason}")
                            reason = f"exhausted: {term_reason}"
                    except Exception:
                        tracker.transition(task.id, TaskStatus.FAILED,
                            error=f"recover: 重试 {retry_count} 次仍崩, 转 FAILED")
                        reason = f"exhausted: {term_reason}"
                else:
                    tracker.transition(task.id, TaskStatus.FAILED,
                        error=f"{validation.verdict}: {term_reason}")
                    reason = f"failed: {term_reason}"
        # ── QA gate: 必须在标 DONE 之前 ──
        # 原顺序 (先标 DONE → 再 QA) 让 QA 形同虚设: 任务已 DONE 改判不动, 但 return
        # 又报 QA:fail, 状态与返回值矛盾。改为 QA 通过才标 DONE。
        qa_blocked = False   # QA 判 fail/retry/escalate → 不许标 DONE
        qa_fail = False
        # worker 里跑过门禁 (有 merge_request 的任务) 就复用它的判定, 不重复跑 supervise
        qa_verdict = getattr(batch, "qa_verdict", "") or ""
        qa_issues = list(getattr(batch, "qa_issues", []) or [])
        if not qa_verdict:
            try:
                from .supervisor import supervise, qa_context
                from .project import repo_root_for
                changed = disp_result.executor_result.changed_files if disp_result else []
                constraints, checklist = qa_context(task)
                sv = supervise(task.description, changed, constraints, checklist,
                              getattr(disp_result.executor_result, 'raw_output', '') if disp_result else '',
                              task.id, repo_root=str(repo_root_for(task)))
                qa_verdict = sv.verdict
                qa_issues = list(sv.issues)
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
        if qa_verdict == "fail":
            qa_blocked = qa_fail = True
            tracker.transition(task.id, TaskStatus.FAILED,
                             error=f"QA:fail: " + "; ".join(qa_issues[:2]))
        elif qa_verdict and qa_verdict != "pass":
            qa_blocked = True
            # 修复 reap bug 根因#2: QA 中间态(retry/escalate/block)转 PENDING 重新入队,
            # 回写 RUNNING 会永久卡死。
            tracker.transition(task.id, TaskStatus.PENDING,
                             error=f"QA:{qa_verdict}: " + "; ".join(qa_issues[:2]))
            reason += f"; QA:{qa_verdict}→PENDING"

        # QA 通过才标 DONE + 推进父任务。QA 拒绝的任务实际失败了, 不能推进父任务。
        # planner_decomposed 的父任务走 DECOMPOSED (等子任务聚合), 也不能标 DONE。
        if batch.ok and not batch.planner_decomposed and not qa_blocked:
            tracker.transition(task.id, TaskStatus.DONE)
            _maybe_complete_parents(task.id)

        # 同步内存态: transition() 只改盘上对象, 传进来的 task.status 还停在调度时的 routed,
        # 不同步则 _save_trace / archive_experience 记的是旧状态。
        fresh = tracker.read_task(task.id)
        if fresh is not None:
            task.status = fresh.status

        _save_trace(task, route, snap, disp_result, validation, validation.action == "rollback",
                    pre_search_skipped=batch.pre_search_skipped,
                    pre_search_reason=batch.pre_search_reason,
                    pre_search_top_decisions=batch.pre_search_top_decisions,
                    pre_search_memory=batch.pre_search_memory)
        # T1 挂钩: 任务完成后归档经验
        try:
            exec_out = disp_result.executor_result if disp_result else None
            mem_mod.archive_experience(
                task_id=task.id, description=task.description,
                status="done" if task.status == TaskStatus.DONE else "failed",
                route_level=route.level,
                model=getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else "",
                elapsed_ms=getattr(exec_out, 'elapsed', 0) if exec_out else 0,
                tokens=getattr(exec_out, 'token_count', 0) if exec_out else 0,
                # failure_mode 跟最终状态走: QA 拦下的任务 batch.ok 仍为 True, 不能记空
                failure_mode=("" if task.status == TaskStatus.DONE
                              else (f"QA:{qa_verdict}" if qa_blocked else validation.verdict)),
                files_changed=getattr(exec_out, 'changed_files', []) if exec_out else [],
            )
            # 用量统计: 记录真实 token 消耗 (只统计, 不做预算拦截)
            try:
                record_tokens(
                    project_id=getattr(task, 'project_id', ''),
                    task_id=task.id,
                    model=getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else "",
                    level=route.level,
                    tokens=getattr(exec_out, 'token_count', 0) if exec_out else 0,
                )
            except Exception:
                pass
            # 同时记录到路由学习器
            try:
                learner = rl_mod.load_learner()
                learner.record(
                    task_type=route.task_type,
                    model=getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else "",
                    level=route.level,
                    # 成功与否看最终状态, 不看 batch.ok (QA 拒绝的任务 batch.ok 仍可能为 True)
                    success=(task.status == TaskStatus.DONE),
                    elapsed_ms=getattr(exec_out, 'elapsed', 0) if exec_out else 0,
                    tokens=getattr(exec_out, 'token_count', 0) if exec_out else 0,
                )
                rl_mod.save_learner(learner)
            except Exception as e:
                try:
                    witness.heartbeat('orch', f'warn:route_learner:{e}')
                except Exception:
                    pass
        except Exception as e:
            try:
                witness.heartbeat('orch', f'warn:archive_experience:{e}')
            except Exception:
                pass
        # 工具事件已由 openai_agent 实时上流(append 到 _pending_sse_events), 此处不再批量推, 避免重复
        turn = getattr(batch, 'turn_count', 0) or 0
        if turn > 0:
            _pending_sse_events.append({
                "kind": "turn", "msg": f"[{task.id[:8]}] 推理完成，共 {turn} 轮",
                "ts": time.time(), "task_id": task.id,
            })
        # QA gate 已上移到标 DONE 之前 (见上方), 此处只剩 QA 拒绝的提前出口:
        # 保持改动前语义 — 跳过 Chancellor, 但 trace/经验归档已在上方落盘。
        if qa_fail:
            results.append((task.id, reason + " (QA拒绝)", validation))
            return reason + "; QA:fail"
        # Chancellor
        try:
            changed = disp_result.executor_result.changed_files if disp_result else []
            report = chan_mod.assess(task.description, term_reason, changed)
            if report.severity in ("alert", "critical"):
                report.task_ids = [task.id]
                chan_mod.save_report(report)
        except Exception as e:
            witness.heartbeat('orch', f'warn:{e}')
        results.append((task.id, reason, validation))
        return reason
