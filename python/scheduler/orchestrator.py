"""orchestrator.py — 调度闭环核心 (facade)。

设计契约 (修复 #7): 只有主线程写 tracker。
  - worker 线程 (v3 ThreadPool) 里的 run() 只做纯执行 (dispatch + validate),
    返回 BatchOutput, 不调任何 tracker.transition/cas/create。
  - 主线程的 _run_queue_v3 负责所有 tracker 写入。
  这样"单线程 tracker 写入"不变量真正成立, 不需要锁。

实现已拆分到 4 个内部模块:
  _types.py    — 共享数据结构 (RunContext, BatchOutput, _SnapProxy)
  _worktree.py — worktree 生命周期
  _exec.py     — 核心执行引擎 (run, _run_with_retry, decompose)
  _planner.py  — 分解物化 + D层委员会
"""

from __future__ import annotations

import json
import os
import re as _re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from typing import Optional

# ── 内部模块 ────────────────────────────────────────────
from ._types import RunContext, BatchOutput, _SnapProxy, _MAX_DEPTH
from ._exec import (
    _PLANNER_PREAMBLE, _inject_memory, _build_project_context,
    run, decompose, _run_with_retry,
    _save_trace, _save_planner_patch, _read_planner_patch,
)
from ._worktree import (
    _maybe_create_worktree, _cleanup_wt, _lock_wt, _unlock_wt,
    _anchor_ref, _release_ref, _build_merge_request,
)
from ._planner import (
    materialize_plan, _topo_sort, _materialize_in_main,
    _maybe_complete_parents,
    _run_committee, _run_committee_member,
    _synthesize_plans, _llm_synthesize as _llm_synth,
)

# ── SSE 事件队列（_exec 写入，主线程 _loop_worker 消费并喷出）──
# 每个元素: {"kind": "tool:start"|"tool:done"|"turn", "msg": ..., "ts": ...}
_pending_sse_events: list[dict] = []

# ── Goal 循环 ──────────────────────────────────────────────
from .goal_loop import GoalLoop

_GOAL_RE = _re.compile(r'^\[Goal\]\s*(.+?)\n', _re.ASCII)

from ._token_budget import record_tokens, get_usage_stats
from ._profiler import record_perf, get_perf_stats
from .execution_judge import judge, should_retry, build_reflexion_feedback
from .model_profile import ProfileStore as _ProfileStore
from .task_templates import get as _get_template, guess_template as _guess_template

# ── 现有依赖 ────────────────────────────────────────────
from . import config
from . import dispatcher as disp_mod
from . import router as router_mod
from . import snapshot as snap_mod
from . import tracker
from . import validator as val_mod
from . import neijinglu as nj_mod
from . import witness
from . import memory as mem_mod
from . import route_learner as rl_mod
from . import pre_search as pre_mod
from . import chancellor as chan_mod
from ._git_worktree import (
    Worktree, create as wt_create, cleanup as wt_cleanup,
    merge_back as wt_merge_back, commit_wt, changed_files_between,
)
from .tracker import TaskStatus

try:
    from .merge import MergeQueue, MergeRequest
except ImportError:
    MergeQueue = None  # type: ignore
    MergeRequest = None  # type: ignore

def run_queue(agents: dict, max_concurrent: int = 1) -> list[tuple]:
    """统一的调度循环入口。v3 支持 1..N 并发, 替代了 v2。"""
    return _run_queue_v3(agents, max_concurrent)


def schedule_policy(tasks: list) -> list:
    """拓扑自适应调度策略: 综合多信号排序就绪任务。

    信号权重:
      - starvation_score (1.0): 防饥饿, 等越久越优先
      - priority (0.5): 用户指定优先级
      - dependency_weight (0.5): 阻塞越多子任务越优先 (关键路径)
      - level_bonus (0.3): D > E+ > E, 复杂任务优先启动

    返回按综合得分降序排列的任务列表。
    """
    def _score(t) -> float:
        level_bonus = {"D": 3, "E+": 2, "E": 1}.get(t.route_level, 0)
        dep_weight = len(t.children) if hasattr(t, 'children') else 0
        return (
            1.0 * t.starvation_score +
            0.5 * t.priority +
            0.5 * dep_weight +
            0.3 * level_bonus
        )
    # 不修改原列表, 返回排序后的新列表
    return sorted(tasks, key=_score, reverse=True)


# ── 执行裁判 + 画像更新 ────────────────────────────────

_profile_store: _ProfileStore | None = None

def _get_profile() -> _ProfileStore:
    global _profile_store
    if _profile_store is None:
        _profile_store = _ProfileStore(config.QIDIAN_DIR / "model_profile.json")
        _profile_store.load()
    return _profile_store


# ── Judge Monitor 单例 ──
_judge_monitor = None  # lazy import

def _get_judge_monitor():
    """返回 JudgeMonitorStore 单例（lazy import 避免循环依赖）。"""
    global _judge_monitor
    if _judge_monitor is None:
        from . import judge_monitor as jm
        _judge_monitor = jm.JudgeMonitorStore(config.QIDIAN_DIR / "judge_monitor.json")
        _judge_monitor.load()
    return _judge_monitor


def _reorder_agents_by_rank(agents_list: list, ranked_models: list[str]) -> list:
    """按画像排名重排 agent 列表：排名靠前的模型优先。"""
    rank_map = {m: i for i, m in enumerate(ranked_models)}
    return sorted(
        agents_list,
        key=lambda a: rank_map.get(a.get("model", ""), 999),
    )


def _judge_and_profile(task, batch: BatchOutput) -> None:
    """执行裁判钩子：判分 + 画像更新 + Reflexion 重试。"""
    disp = batch.dispatch_result
    if disp is None or disp.executor_result is None:
        return

    output = disp.executor_result.raw_output or ""
    agent_cfg = disp.agent_cfg or {}

    # 1. 模板推断 + 注入（下次路由时参考）
    task_type = _guess_template(task.description)

    # 2. 裁判判分
    verdict = judge(task.description, output, task_type)

    model = agent_cfg.get("model", "unknown")

    # 2b. Judge Monitor: 记录裁判自身表现
    try:
        _jm = _get_judge_monitor()
        _jm.record(task_type=task_type, model=model, verdict=verdict,
                   template_id=task_type)
    except Exception as e:
        witness.heartbeat('orch', f'warn:{e}')

    # 3. 更新画像
    store = _get_profile()
    elapsed = getattr(disp.executor_result, "elapsed", 0) or 0
    tokens = getattr(disp.executor_result, "tokens", 0) or 0
    store.record(model, task_type, verdict.pass_, elapsed, tokens, verdict.failure_mode,
                 template_id=task_type)

    # 4. 失败 + 可重试 → Reflexion 注入（写到 batch，让上层重试）
    if not verdict.pass_ and should_retry(verdict, getattr(task, "retry_count", 0)):
        feedback = build_reflexion_feedback(verdict)
        batch.term_reason = f"judge_fail: {verdict.reason}"
        batch.judge_verdict = verdict
        batch.reflexion_feedback = feedback
        # 改判：validator 说 pass 但 judge 说 fail → 覆盖
        if batch.validation.action == "pass":
            batch.validation = batch.validation.__class__(
                verdict="fail", action="abort",
                unverified=[verdict.reason],
            )
    else:
        batch.judge_verdict = verdict

    # 5. 持久化画像
    try:
        store.save()
    except Exception as e:
        witness.heartbeat('orch', f'warn:{e}')
    # 5b. 持久化 judge monitor
    try:
        _jm = _get_judge_monitor()
        _jm.save()
    except Exception as e:
        witness.heartbeat('orch', f'warn:{e}')

    # 6. 交接记录
    try:
        from . import handoff as _hf
        h = _hf.create_handoff(task, batch)
        if h and getattr(task, "project_id", ""):
            _hf.append_to_project(task.project_id, h)
    except Exception as e:
        witness.heartbeat('orch', f'warn:{e}')


def _execute_one_task(task, agents: dict):
    """执行单个任务: 路由→预检→Goal/委员会/普通→返回(batch, route, snap)。"""
    # 路由
    if task.route_locked:
        route = router_mod.RouteResult(
            level=task.route_level, gate_required=task.route_gate,
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
    ranked_models = router_mod.rank_models_for_task(task.description, route.task_type, phase=project_phase, route_level=route.level)
    effective_agents = dict(agents)
    if ranked_models:
        effective_agents[route.level] = _reorder_agents_by_rank(
            agents.get(route.level, []), ranked_models)
    # 快照
    snap = snap_mod.take(task.id)
    ctx = RunContext(batch_id=task.id, snapshot_ref=snap.ref, merge_queue=None)
    # 执行分叉: Goal循环 / D层委员会 / 普通
    goal_match = _GOAL_RE.match(task.description)
    if goal_match:
        goal = goal_match.group(1).strip()
        _pending_sse_events.append({"kind": "system", "msg": f"Goal循环: {goal[:60]}", "ts": time.time(), "task_id": task.id})
        loop = GoalLoop(effective_agents)
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
            'agent_cfg': {}, 'level': route.level})()
    else:
        d_agents = effective_agents.get("D", [])
        use_committee = route.level == "D" and len(d_agents) >= 2
        use_fusion = route.task_type == "fusion"
        if use_committee:
            batch = _run_committee(task, ctx, effective_agents, d_agents)
        elif use_fusion:
            from ._exec import _run_fusion
            tier = "super" if route.level == "D" else ("triple" if route.level == "E+" else "dual")
            batch = _run_fusion(task, route.level, effective_agents, ctx=ctx, tier=tier)
        else:
            batch = _run_with_retry(task, ctx, effective_agents)
    batch.pre_search_skipped = pre.skipped
    batch.pre_search_reason = pre.reason
    batch.pre_search_top_decisions = pre.top_decisions
    batch.pre_search_memory = {"intent": pre.memory.intent, "narrative": pre.memory.narrative,
                               "entity_matches": pre.memory.entity_matches, "graph_coverage": pre.memory.graph_coverage}
    return batch, route, snap


def _finalize_result(task, batch, route, snap, results: list) -> str:
    """后处理: 裁判/写终态/trace/QA gate/Chancellor/escalation。返回 reason。"""
    _judge_and_profile(task, batch)
    validation = batch.validation
    term_reason = batch.term_reason
    disp_result = batch.dispatch_result
    if batch.planner_decomposed:
        _materialize_in_main(batch, task)
        reason = f"decomposed: {term_reason}"
    elif validation.action == "pass":
        tracker.transition(task.id, TaskStatus.DONE)
        _maybe_complete_parents(task.id)
        reason = f"pass: {term_reason}"
    elif validation.action == "rollback":
        snap_mod.rollback(snap)
        tracker.transition(task.id, TaskStatus.ROLLED_BACK, error=f"{validation.verdict}: {term_reason}")
        reason = f"rolled_back: {term_reason}"
    else:
        d_plan = _read_planner_patch(task.id)
        if d_plan and "escalation_exhausted" in term_reason:
            fix_task = tracker.create(f"[D方案执行] {task.description[:80]}", depends_on=[task.id], depth=task.depth)
            tracker.transition(fix_task.id, TaskStatus.PENDING, route_level="E+", route_locked=True)
            tracker.transition(task.id, TaskStatus.FAILED, error=f"已生成E+修复任务 {fix_task.id[:8]}: {term_reason}")
            reason = f"escalated_to_E+: {fix_task.id[:8]}"
        else:
            # 降级重试: 重试耗尽 → 自动拆分再提交 (每个子任务更小)
            retry_count = getattr(task, 'retry_count', 0)
            if retry_count >= getattr(task, 'max_retries', 3) and task.depth < _MAX_DEPTH:
                try:
                    subtasks = decompose(task.description, agents)
                    if subtasks and len(subtasks) > 1:
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
    _save_trace(task, route, snap, disp_result, validation, validation.action == "rollback",
                pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                pre_search_top_decisions=batch.pre_search_top_decisions, pre_search_memory=batch.pre_search_memory)
    # T1 挂钩: 任务完成后归档经验
    try:
        exec_out = disp_result.executor_result if disp_result else None
        mem_mod.archive_experience(
            task_id=task.id, description=task.description,
            status="done" if validation.action == "pass" else "failed",
            route_level=route.level, model=getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else "",
            elapsed_ms=getattr(exec_out, 'elapsed', 0) if exec_out else 0,
            tokens=getattr(exec_out, 'tokens', 0) if exec_out else 0,
            failure_mode=validation.verdict if validation.action != "pass" else "",
            files_changed=getattr(exec_out, 'changed_files', []) if exec_out else [],
        )
        # 同时记录到路由学习器
        try:
            learner = rl_mod.load_learner()
            learner.record(
                task_type=route.task_type, model=getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else "",
                level=route.level,
                success=validation.action == "pass",
                elapsed_ms=getattr(exec_out, 'elapsed', 0) if exec_out else 0,
                tokens=getattr(exec_out, 'tokens', 0) if exec_out else 0,
            )
            rl_mod.save_learner(learner)
        except Exception:
            pass
    except Exception:
        pass
    # 工具事件
    tool_events = getattr(batch, 'tool_events', []) or []
    for te in tool_events:
        te['task_id'] = task.id
        _pending_sse_events.append(te)
    turn = getattr(batch, 'turn_count', 0) or 0
    if turn > 0:
        _pending_sse_events.append({"kind": "turn", "msg": f"[{task.id[:8]}] 推理完成，共 {turn} 轮", "ts": time.time(), "task_id": task.id})
    # QA gate
    try:
        from .supervisor import supervise
        changed = disp_result.executor_result.changed_files if disp_result else []
        constraints, checklist = [], []
        pid = getattr(task, 'project_id', '')
        if pid:
            try:
                from .project import load as _load_proj
                proj = _load_proj(pid)
                if proj:
                    constraints = proj.constraints_checklist
                    if proj.architecture:
                        for tdef in proj.architecture.get("tasks", []):
                            if tdef.get("title", "") in task.description or tdef.get("id", "") in task.description:
                                acc = tdef.get("acceptance", "")
                                if acc:
                                    checklist.append(acc)
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
        sv = supervise(task.description, changed, constraints, checklist,
                      getattr(disp_result.executor_result, 'raw_output', '') if disp_result else '', task.id)
        if sv.verdict == "fail":
            tracker.transition(task.id, TaskStatus.FAILED, error=f"QA:fail: " + "; ".join(sv.issues[:2]))
            results.append((task.id, reason + " (QA拒绝)", validation))
            return reason + "; QA:fail"
        elif sv.verdict != "pass":
            tracker.transition(task.id, task.status, error=f"QA:{sv.verdict}: " + "; ".join(sv.issues[:2]))
            reason += f"; QA:{sv.verdict}"
    except Exception as e:
        witness.heartbeat('orch', f'warn:{e}')
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


def _dispatch_ready(dispatched: set, pool, agents, running_futures: dict) -> bool:
    """_run_queue_v3 步骤①②③: 选就绪→cas抢占→提交线程池。返回是否有新派发。"""
    ready = tracker.ready_tasks(exclude=dispatched)
    ready = schedule_policy(ready)
    dispatched_any = False
    for t in ready:
        if t.route_locked:
            route = router_mod.RouteResult(
                level=t.route_level, gate_required=t.route_gate,
                task_type=t.route_type)
        else:
            route = router_mod.route(t.description)
        pre = pre_mod.pre_search(t.description, route)
        pre_mod.apply_escalation(route, pre)
        if tracker.cas(t.id, TaskStatus.ROUTED, TaskStatus.DISPATCHED,
                       route_level=route.level, route_gate=route.gate_required,
                       route_type=route.task_type):
            snap = snap_mod.take(t.id)
            tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
            dispatched.add(t.id)
            fut = pool.submit(_execute_one_task, t, agents)
            running_futures[fut] = (t, route, snap, pre, time.time())
            dispatched_any = True
    return dispatched_any


def _reap_futures(running_futures: dict, pending_batches: dict,
                  mq, results: list) -> bool:
    """_run_queue_v3 步骤④: 回收已完成 future → _finalize_result 或入 pending。返回是否有回收。

    修复: 不再用 FIRST_COMPLETED 误杀未完成的并发任务。
    改为 ALL_COMPLETED + 短超时轮询，仅对超过 per-future deadline 的标记超时。
    """
    if not running_futures:
        return False
    now = time.time()
    deadline = 600  # per-future 超时阈值
    reaped = False
    # 先收割已完成的
    done_futs = [f for f in running_futures if f.done()]
    for fut in done_futs:
        t, route, snap, pre, submitted_at = running_futures.pop(fut)
        reaped = True
        try:
            batch, t_route, t_snap = fut.result()
        except Exception as e:
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"worker 异常: {e}")
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
            results.append((t.id, f"worker_error: {e}", None))
            try:
                _save_trace(t, route, snap, None, None, False)
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
            continue
        if batch.merge_request is not None:
            mq.submit(batch.merge_request)
            pending_batches[t.id] = (t, t_route, t_snap, batch)
        else:
            _finalize_result(t, batch, t_route, t_snap, results)
    # 检查超时: 仅杀超过 deadline 的 future
    for fut in list(running_futures.keys()):
        if fut.done():
            continue  # 下一轮 done_futs 收割
        t, route, snap, pre, submitted_at = running_futures.get(fut, (None,)*5)
        if t is not None and now - submitted_at > deadline:
            running_futures.pop(fut)
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"执行超时(>{deadline}s)")
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
            results.append((t.id, "timeout", None))
            try:
                _save_trace(t, route, snap, None, None, False)
            except Exception as e:
                witness.heartbeat('orch', f'warn:{e}')
            fut.cancel()
            reaped = True
    # 如果无事可收但还有 running future, 短暂 block 等下一个完成
    if not reaped and running_futures:
        wait(running_futures.keys(), timeout=5, return_when=FIRST_COMPLETED)
    return reaped


def _drain_pending(pending_batches: dict, mq, results: list) -> int:
    """_run_queue_v3 步骤⑥: drain merge queue → 合成功的标 DONE。返回 drain 数。"""
    if not pending_batches:
        return 0
    drained = 0
    merge_results = mq.drain()
    for mr in merge_results:
        if mr.task_id in pending_batches:
            t, route, snap, batch = pending_batches.pop(mr.task_id)
            if mr.status == "merged":
                tracker.transition(t.id, TaskStatus.DONE)
                _maybe_complete_parents(t.id)
                _release_ref(t.id)
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions, pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merged: {mr.new_head[:8]}", batch.validation))
            elif mr.status == "conflict":
                results.append((t.id, f"conflict: {mr.conflict_files}", batch.validation))
            else:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"merge {mr.status}")
                _release_ref(t.id)
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions, pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merge_failed", batch.validation))
            drained += 1
    return drained


def _run_queue_v3(agents: dict, max_concurrent: int) -> list[tuple]:
    """v3 统一调度循环: dispatch→reap→drain 三步，支持 1..N 并发。

    认知复杂度从 73 → ~15 (拆成 3 个 helper + 主循环)。
    """
    results: list[tuple] = []
    mq = MergeQueue()
    dispatched: set[str] = set()
    running_futures: dict = {}   # future -> (task, route, snap)
    pending_batches: dict = {}   # task_id -> (task, route, snap, batch)

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while True:
            _dispatch_ready(dispatched, pool, agents, running_futures)

            if not running_futures and not pending_batches:
                remaining = tracker.ready_tasks(exclude=dispatched)
                if not remaining:
                    break
                continue

            _reap_futures(running_futures, pending_batches, mq, results)
            _drain_pending(pending_batches, mq, results)

    return results


_consolidate_calls = 0

def consolidate_memory() -> int:
    """慢通道整合: embedding粗筛 + LLM精判 → 发现隐含因果边。
    每10次调用自动触发记忆生命周期维护。

    三档逻辑:
      - sim ≥ 0.85 + 时间<4h + 共享文件 → 高置信，直接加边（不调LLM）
      - sim < 0.55 → 丢弃
      - 0.55 ≤ sim < 0.85 → LLM精判（DeepSeek E层）

    返回添加的隐含边数。
    """
    global _consolidate_calls
    _consolidate_calls += 1

    # ponytail: 速率控制 — 最小间隔 5min, 每次最多判 5 对
    now = time.time()
    if _consolidate_calls > 1 and now - getattr(consolidate_memory, '_last_run', 0) < 300:
        return 0
    consolidate_memory._last_run = now
    _MAX_LLM_CANDIDATES = 5

    # 每10次运行一次记忆生命周期维护 + System2 模式提取
    if _consolidate_calls % 10 == 0:
        try:
            lc = mem_mod.auto_maintain()
            if lc.get("pruned", 0) > 0:
                _pending_sse_events.append({
                    "kind": "memory", "msg": f"记忆清理: {lc['pruned']} 过期事件",
                    "ts": time.time(),
                })
        except Exception as e:
            witness.heartbeat('orch', f'warn:{e}')
        # DCPM System2: 夜间异步模式提取
        try:
            s2 = mem_mod.system2_extract()
            if s2.get("added", 0) > 0:
                for ins in s2.get("insights", []):
                    _pending_sse_events.append({
                        "kind": "insight", "msg": ins.get("summary", ""),
                        "ts": time.time(),
                    })
        except Exception as e:
            witness.heartbeat('orch', f'warn:{e}')
    try:
        candidates = mem_mod.find_candidate_latent_edges()
        added = 0

        # ponytail: Tier 3 LLM 精判上限 — 只判 sim 最高的前 N 对
        tier3_capped = set()
        tier3_all = [c for c in candidates if 0.55 <= c.get("semantic_sim", 0) < 0.85]
        if len(tier3_all) > _MAX_LLM_CANDIDATES:
            tier3_all.sort(key=lambda x: -x["semantic_sim"])
            tier3_capped = {id(c) for c in tier3_all[_MAX_LLM_CANDIDATES:]}

        for c in candidates:
            sim = c["semantic_sim"]
            gap = c["time_gap_hours"]
            shared = c["shared_files"]

            # Tier 1: 高置信直接加边
            if sim >= 0.85 and gap < 4.0 and len(shared) >= 1:
                src, dst = _resolve_direction(c)
                if src:
                    mem_mod.add_inferred_causal_edge(
                        src, dst,
                        reason=f"high_conf:shared:{','.join(shared)} sim={sim:.2f} gap={gap:.1f}h"
                    )
                    added += 1
                continue

            # Tier 2: 低于阈值跳过
            if sim < 0.55:
                continue

            # Tier 3: LLM 精判, 超上限的跳过
            if id(c) in tier3_capped:
                continue
            src, dst = _resolve_direction(c)
            if not src:
                continue
            judge = _llm_judge_causal(c, src, dst)
            if judge.get("is_causal"):
                mem_mod.add_inferred_causal_edge(
                    src, dst,
                    reason=f"llm:{judge.get('reason','')}"
                )
                added += 1

        return added
    except Exception as e:
        try: witness.heartbeat("memory", f"warn:consolidate:{e}")
        except Exception: pass
        return 0


def _resolve_direction(c: dict) -> tuple:
    """根据时间戳判断因果方向: 早→晚。返回 (src, dst) 或 (None, None)。"""
    a, b = c["task_a"], c["task_b"]
    events = mem_mod._load_events()
    node_a = events.get(a)
    node_b = events.get(b)
    if not node_a or not node_b:
        return None, None
    if node_a.timestamp <= node_b.timestamp:
        return a, b
    return b, a


def _llm_judge_causal(c: dict, src: str, dst: str) -> dict:
    """用 DeepSeek (E层) 判断候选对是否有因果关系。

    返回: {"is_causal": bool, "reason": str}
    失败时返回 {"is_causal": False, "reason": "llm_error"}
    """
    import httpx

    prompt = f"""判断以下两个任务之间是否存在因果关系（一个导致了另一个）。

任务A [{src[:8]}]：{c.get('desc_a','')}
任务B [{dst[:8]}]：{c.get('desc_b','')}
共享文件：{', '.join(c.get('shared_files',[]))}
语义相似度：{c.get('semantic_sim',0):.3f}
时间间隔：{c.get('time_gap_hours',0):.1f}小时

只回答 JSON：{{"is_causal": true/false, "reason": "一句话原因"}}
如果任务A导致了任务B，is_causal=true。否则 false。不确定时 false。"""

    try:
        agents = disp_mod.load_agents()
        e_agents = agents.get("E", [])
        if not e_agents:
            return {"is_causal": False, "reason": "no_e_agent"}

        e_cfg = e_agents[0]
        api_key = os.environ.get(e_cfg.get("api_key_env", ""), "")
        if not api_key:
            return {"is_causal": False, "reason": "no_api_key"}

        base_url = e_cfg.get("base_url", "https://api.deepseek.com/v1")
        model = e_cfg.get("model", "deepseek-chat")

        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1,
        }

        client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        resp = client.post(
            f"{base_url}/chat/completions",
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"]

        import re as _re
        m = _re.search(r'\{[^}]+\}', raw)
        if m:
            return json.loads(m.group())
        return {"is_causal": False, "reason": "parse_error"}
    except Exception as e:
        try:
            import logging
            logging.getLogger("qidian").warning("llm_judge_causal: %s", e)
        except Exception as e:
            witness.heartbeat('orch', f'warn:{e}')
        return {"is_causal": False, "reason": f"llm_error:{e}"}