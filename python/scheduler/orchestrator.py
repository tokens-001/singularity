"""orchestrator.py — 调度闭环核心 (facade)。

设计契约 (修复 #7): 只有主线程写 tracker。
  - worker 线程 (v3 ThreadPool) 里的 run() 只做纯执行 (dispatch + validate),
    返回 BatchOutput, 不调任何 tracker.transition/cas/create。
  - 主线程的调度循环 (_run_queue_v2 / _run_queue_v3) 负责所有 tracker 写入。
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
from . import pre_search as pre_mod
from . import chancellor as chan_mod
from .executors.worktree import (
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
    if max_concurrent <= 1:
        return _run_queue_v2(agents)
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
    except Exception:
        pass

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
    except Exception:
        pass
    # 5b. 持久化 judge monitor
    try:
        _jm = _get_judge_monitor()
        _jm.save()
    except Exception:
        pass

    # 6. 交接记录
    try:
        from . import handoff as _hf
        h = _hf.create_handoff(task, batch)
        if h and getattr(task, "project_id", ""):
            _hf.append_to_project(task.project_id, h)
    except Exception:
        pass


def _run_queue_v2(agents: dict) -> list[tuple]:
    """v2 顺序: 主线程同步调 run(merge_queue=None), 终态在这写。"""
    results: list[tuple] = []

    while True:
        stalled = witness.check_stalled()
        if stalled:
            pass

        # 拓扑自适应: 从所有就绪任务中选最优
        ready = tracker.list_pending()
        if not ready:
            break
        ready = schedule_policy(ready)
        task = ready[0]

        # 尊重 planner 建议层级, 不重新路由 (建议 #6)
        if task.route_locked:
            route = router_mod.RouteResult(
                level=task.route_level, gate_required=task.route_gate,
                task_type=task.route_type,
            )
        else:
            route = router_mod.route(task.description)

        # ── I 层预检 (知识库 + MAGMA 记忆) ──
        pre = pre_mod.pre_search(task.description, route)
        pre_mod.apply_escalation(route, pre)

        # ── 画像路由: 给 dispatch 提供模型偏好 ──
        # 提取项目阶段（相位感知路由）
        project_phase = None
        try:
            pid = getattr(task, "project_id", "")
            if pid:
                from . import project as proj_mod
                proj = proj_mod.load(pid)
                if proj:
                    project_phase = proj.phase.value
        except Exception:
            pass

        ranked_models = router_mod.rank_models_for_task(
            task.description, route.task_type, phase=project_phase,
        )
        effective_agents = agents
        if ranked_models:
            # 构造临时 lineup: 画像排名靠前的优先
            effective_agents = dict(agents)  # 浅拷贝
            effective_agents[route.level] = _reorder_agents_by_rank(
                agents.get(route.level, []), ranked_models,
            )

        tracker.transition(
            task.id, TaskStatus.ROUTED,
            route_level=route.level, route_gate=route.gate_required,
            route_type=route.task_type,
        )
        snap = snap_mod.take(task.id)
        tracker.transition(task.id, TaskStatus.RUNNING, snapshot_id=snap.id)

        ctx = RunContext(batch_id=task.id, snapshot_ref=snap.ref, merge_queue=None)

        # D层委员会: 多agent并行出方案，合成最优
        d_agents = effective_agents.get("D", [])
        use_committee = route.level == "D" and len(d_agents) >= 2
        if use_committee:
            batch = _run_committee(task, ctx, effective_agents, d_agents)
        else:
            batch = _run_with_retry(task, ctx, effective_agents)

        batch.pre_search_skipped = pre.skipped
        batch.pre_search_reason = pre.reason
        batch.pre_search_top_decisions = pre.top_decisions
        batch.pre_search_memory = {
            "intent": pre.memory.intent,
            "narrative": pre.memory.narrative,
            "entity_matches": pre.memory.entity_matches,
            "graph_coverage": pre.memory.graph_coverage,
        }

        # ── 执行裁判钩子 ──
        _judge_and_profile(task, batch)

        # 主线程写终态 (修复 #3: 无 merge_request → 直接 DONE)
        validation = batch.validation
        term_reason = batch.term_reason
        disp_result = batch.dispatch_result

        if batch.planner_decomposed:
            # planner 分解了 → parent 转 DECOMPOSED, materialize 在这做 (主线程)
            _materialize_in_main(batch, task)
            reason = f"decomposed: {term_reason}"
        elif validation.action == "pass":
            # 修复 #3: 无 merge_request → 直接 DONE
            tracker.transition(task.id, TaskStatus.DONE)
            _maybe_complete_parents(task.id)
            reason = f"pass: {term_reason}"
        elif validation.action == "rollback":
            snap_mod.rollback(snap)
            tracker.transition(task.id, TaskStatus.ROLLED_BACK, error=f"{validation.verdict}: {term_reason}")
            reason = f"rolled_back: {term_reason}"
        else:
            # D 层分析完有方案 → 建新任务给 E+ 执行
            d_plan = _read_planner_patch(task.id)
            if d_plan and "escalation_exhausted" in term_reason:
                fix_task = tracker.create(
                    f"[D方案执行] {task.description[:80]}",
                    depends_on=[task.id],
                    depth=task.depth,
                )
                tracker.transition(fix_task.id, TaskStatus.PENDING,
                                   route_level="E+", route_locked=True)
                tracker.transition(task.id, TaskStatus.FAILED,
                                   error=f"已生成E+修复任务 {fix_task.id[:8]}: {term_reason}")
                reason = f"escalated_to_E+: {fix_task.id[:8]}"
            else:
                tracker.transition(task.id, TaskStatus.FAILED, error=f"{validation.verdict}: {term_reason}")
                reason = f"failed: {term_reason}"

        _save_trace(task, route, snap, disp_result, validation, validation.action == "rollback",
                    pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                    pre_search_top_decisions=batch.pre_search_top_decisions,
                    pre_search_memory=batch.pre_search_memory)
        results.append((task.id, reason, validation))
        # QA Gate: 机械质检每任务产出 (项目上下文增强)
        try:
            from .supervisor import supervise
            changed = disp_result.executor_result.changed_files if disp_result else []
            constraints = []
            checklist = []
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
                except Exception:
                    pass
            sv = supervise(task.description, changed, constraints, checklist,
                          getattr(disp_result.executor_result, 'raw_output', '') if disp_result else '',
                          task.id)
            if sv.verdict != "pass":
                reason += f"; QA:{sv.verdict}"
                # 硬证据失败 → 阻止标 DONE, 标 FAILED
                if sv.verdict == "fail":
                    tracker.transition(t.id, TaskStatus.FAILED, error=f"QA:fail: " + "; ".join(sv.issues[:2]))
                    results.append((t.id, reason + " (QA拒绝)", validation))
                    continue  # 跳过 DONE 标记
                else:
                    tracker.transition(task.id, task.status, error=f"QA:{sv.verdict}: " + "; ".join(sv.issues[:2]))
        except Exception as e:
            try:
                from . import log as log_mod
                log_mod.warn("qa_gate", f"task={task.id}: {e}")
            except Exception:
                pass
            try: witness.heartbeat(task_id=task.id, status="error", detail=f"qa_gate:{e}")
            except: pass
        # 奇点: 评估是否需要奏报
        try:
            changed = disp_result.executor_result.changed_files if disp_result else []
            report = chan_mod.assess(task.description, term_reason, changed)
            if report.severity in ("alert", "critical"):
                report.task_ids = [task.id]
                chan_mod.save_report(report)
        except Exception as e:
            try:
                from . import log as log_mod
                log_mod.warn("chancellor", f"task={task.id}: {e}")
            except Exception:
                pass
            try: witness.heartbeat(task_id=task.id, status="error", detail=f"chancellor:{e}")
            except: pass

    return results


def _run_queue_v3(agents: dict, max_concurrent: int) -> list[tuple]:
    """v3 并行: worker 跑 run() 纯执行, 主线程回收后写 tracker + drain merge。"""
    if MergeQueue is None:
        return _run_queue_v2(agents)

    results: list[tuple] = []
    mq = MergeQueue()
    batch_id = f"batch_{int(time.time())}"
    batch_snap = snap_mod.take(batch_id)
    worktree_base = str(config.QIDIAN_DIR / "worktrees")
    ctx = RunContext(
        batch_id=batch_id, snapshot_ref=batch_snap.ref,
        worktree_base=worktree_base, merge_queue=mq,
    )

    dispatched: set[str] = set()
    running_futures: dict = {}  # future -> (task, route, snap)
    pending_batches: dict = {}  # task_id -> (task, route, snap, batch) 等 drain 后再判终态 (修复 #9)

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while True:
            # ① 选就绪任务 (ready_tasks 已把 PENDING/BLOCKED→ROUTED, 修复 #1)
            ready = tracker.ready_tasks(exclude=dispatched)
            ready = schedule_policy(ready)  # 拓扑自适应排序
            for t in ready:
                # ② cas 抢占 ROUTED→DISPATCHED (主线程写)
                # 尊重 planner 建议层级, 不重新路由 (建议 #6)
                if t.route_locked:
                    route = router_mod.RouteResult(
                        level=t.route_level, gate_required=t.route_gate,
                        task_type=t.route_type,
                    )
                else:
                    route = router_mod.route(t.description)

                # ── I 层预检 (知识库 + MAGMA 记忆) ──
                pre = pre_mod.pre_search(t.description, route)
                pre_mod.apply_escalation(route, pre)

                if tracker.cas(
                    t.id, TaskStatus.ROUTED, TaskStatus.DISPATCHED,
                    route_level=route.level, route_gate=route.gate_required,
                    route_type=route.task_type,
                ):
                    # 每任务独立 snapshot (v2 一致性)
                    snap = snap_mod.take(t.id)
                    tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
                    dispatched.add(t.id)
                    fut = pool.submit(_run_with_retry, t, ctx, agents)
                    running_futures[fut] = (t, route, snap, pre)

            if not running_futures and not pending_batches:
                remaining = tracker.ready_tasks(exclude=dispatched)
                if not remaining:
                    break
                continue

            # ④ 回收 (主线程, 这里才写 tracker)
            if running_futures:
                done, not_done = wait(running_futures.keys(), timeout=600, return_when=FIRST_COMPLETED)
                # 超时任务强制标记 FAILED
                for fut in not_done:
                    t, route, snap, pre = running_futures.pop(fut, (None, None, None, None))
                    if t is not None:
                        try:
                            tracker.transition(t.id, TaskStatus.FAILED, error="执行超时(>600s)")
                        except Exception:
                            pass
                        results.append((t.id, "timeout", None))
                        try:
                            _save_trace(t, route, snap, None, None, False)
                        except Exception:
                            pass
                    fut.cancel()
                for fut in done:
                    t, route, snap, pre = running_futures.pop(fut)
                    batch = fut.result()
                    batch.pre_search_skipped = pre.skipped
                    batch.pre_search_reason = pre.reason
                    batch.pre_search_top_decisions = pre.top_decisions
                    batch.pre_search_memory = {
                        "intent": pre.memory.intent,
                        "narrative": pre.memory.narrative,
                        "entity_matches": pre.memory.entity_matches,
                        "graph_coverage": pre.memory.graph_coverage,
                    }

                    # ── 执行裁判钩子（v3 路径也需要）──
                    _judge_and_profile(t, batch)

                    if batch.planner_decomposed:
                        _materialize_in_main(batch, t)
                        _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                                    pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                                    pre_search_top_decisions=batch.pre_search_top_decisions,
                                    pre_search_memory=batch.pre_search_memory)
                        results.append((t.id, f"decomposed: {batch.term_reason}", batch.validation))
                        continue

                    validation = batch.validation
                    if validation.action == "pass":
                        if batch.merge_request is not None:
                            # 修复 #3: 有 merge_request → submit, 等 drain 合成功才 DONE
                            mq.submit(batch.merge_request)
                            pending_batches[t.id] = (t, route, snap, batch)
                        else:
                            # 无 merge_request (planner/无wt) → 直接 DONE
                            tracker.transition(t.id, TaskStatus.DONE)
                            _maybe_complete_parents(t.id)
                            _save_trace(t, route, snap, batch.dispatch_result, validation, False,
                                        pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                                        pre_search_top_decisions=batch.pre_search_top_decisions,
                                        pre_search_memory=batch.pre_search_memory)
                            results.append((t.id, f"pass: {batch.term_reason}", validation))
                    elif validation.action == "rollback":
                        snap_mod.rollback(batch_snap)
                        tracker.transition(t.id, TaskStatus.ROLLED_BACK, error=f"{validation.verdict}: {batch.term_reason}")
                        _release_ref(t.id)
                        _save_trace(t, route, snap, batch.dispatch_result, validation, True,
                                    pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                                    pre_search_top_decisions=batch.pre_search_top_decisions,
                                    pre_search_memory=batch.pre_search_memory)
                        results.append((t.id, f"rolled_back: {batch.term_reason}", validation))
                    else:
                        tracker.transition(t.id, TaskStatus.FAILED, error=f"{validation.verdict}: {batch.term_reason}")
                        _release_ref(t.id)
                        _save_trace(t, route, snap, batch.dispatch_result, validation, False,
                                    pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                                    pre_search_top_decisions=batch.pre_search_top_decisions,
                                    pre_search_memory=batch.pre_search_memory)
                        results.append((t.id, f"failed: {batch.term_reason}", validation))

            # ⑥ drain (主线程), 合成功的 task 标 DONE (修复 #9: drain 后才定终态)
            if pending_batches:
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
                                        pre_search_top_decisions=batch.pre_search_top_decisions,
                                        pre_search_memory=batch.pre_search_memory)
                            results.append((t.id, f"merged: {mr.new_head[:8]}", batch.validation))
                        elif mr.status == "conflict":
                            # CONFLICT_HELD 已在 mq._park 标过, 保留 ref 等人; 非终态不存 trace
                            results.append((t.id, f"conflict: {mr.conflict_files}", batch.validation))
                        else:
                            tracker.transition(t.id, TaskStatus.FAILED, error=f"merge {mr.status}")
                            _release_ref(t.id)
                            _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                                        pre_search_skipped=batch.pre_search_skipped, pre_search_reason=batch.pre_search_reason,
                                        pre_search_top_decisions=batch.pre_search_top_decisions,
                                        pre_search_memory=batch.pre_search_memory)
                            results.append((t.id, f"merge_failed", batch.validation))

    return results


def consolidate_memory() -> int:
    """慢通道整合: embedding粗筛 + LLM精判 → 发现隐含因果边。

    三档逻辑:
      - sim ≥ 0.85 + 时间<4h + 共享文件 → 高置信，直接加边（不调LLM）
      - sim < 0.55 → 丢弃
      - 0.55 ≤ sim < 0.85 → LLM精判（DeepSeek E层）

    返回添加的隐含边数。
    """
    try:
        candidates = mem_mod.find_candidate_latent_edges()
        added = 0

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

            # Tier 3: LLM 精判 (0.55 ≤ sim < 0.85)
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
        except: pass
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
        except Exception:
            pass
        return {"is_causal": False, "reason": f"llm_error:{e}"}