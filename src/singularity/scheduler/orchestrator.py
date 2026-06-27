"""orchestrator.py — 调度闭环核心 (facade)。

设计契约 (修复 #7): 只有主线程写 tracker。
  - worker 线程 (v3 ThreadPool) 里的 TaskRunner.execute() 只做纯执行 (dispatch + validate),
    返回 BatchOutput, 不调任何 tracker.transition/cas/create。
  - 主线程的 _run_queue_v3 负责所有 tracker 写入。

架构 #1.1: 任务生命周期已抽到 _task_runner.TaskRunner。
  orchestrator 只管队列调度 (dispatch → reap → drain 三步循环)。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

# ── 队列调度所需 (精简后) ──────────────────────────────────
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler._exec import _save_trace
from singularity.scheduler._worktree import _release_ref
from singularity.scheduler._planner import _maybe_complete_parents
from singularity.scheduler._task_runner import TaskRunner

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler import router as router_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import witness
from singularity.scheduler import pre_search as pre_mod
from singularity.scheduler.tracker import TaskStatus

try:
    from .merge import MergeQueue, MergeRequest
except ImportError:
    MergeQueue = None  # type: ignore
    MergeRequest = None  # type: ignore


def run_queue(agents: dict, max_concurrent: int = 1) -> list[tuple]:
    """统一的调度循环入口。v3 支持 1..N 并发。"""
    return _run_queue_v3(agents, max_concurrent)


def schedule_policy(tasks: list) -> list:
    """拓扑自适应调度策略: 综合多信号排序就绪任务。

    信号权重:
      - starvation_score (1.0): 防饥饿, 等越久越优先
      - priority (0.5): 用户指定优先级
      - dependency_weight (0.5): 阻塞越多子任务越优先 (关键路径)
      - level_bonus (0.3): D > E+ > E, 复杂任务优先启动
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
    return sorted(tasks, key=_score, reverse=True)


def _dispatch_ready(dispatched: set, pool, agents, runner: TaskRunner,
                    running_futures: dict) -> bool:
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
        # PENDING → ROUTED (若尚未路由)
        if t.status == TaskStatus.PENDING:
            if not tracker.cas(t.id, TaskStatus.PENDING, TaskStatus.ROUTED,
                               route_level=route.level, route_gate=route.gate_required,
                               route_type=route.task_type):
                continue  # CAS 失败，下一轮重试
            t.status = TaskStatus.ROUTED
        if tracker.cas(t.id, TaskStatus.ROUTED, TaskStatus.DISPATCHED,
                       route_level=route.level, route_gate=route.gate_required,
                       route_type=route.task_type):
            snap = snap_mod.take(t.id)
            tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
            dispatched.add(t.id)
            fut = pool.submit(runner.execute, t, agents)
            running_futures[fut] = (t, route, snap, pre, time.time())
            dispatched_any = True
    return dispatched_any


def _reap_futures(running_futures: dict, pending_batches: dict,
                  mq, runner: TaskRunner, results: list) -> bool:
    """_run_queue_v3 步骤④: 回收已完成 future → finalize 或入 pending。返回是否有回收。

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
            except Exception:
                pass
            results.append((t.id, f"worker_error: {e}", None))
            try:
                _save_trace(t, route, snap, None, None, False)
            except Exception:
                pass
            continue
        if batch.merge_request is not None:
            mq.submit(batch.merge_request)
            pending_batches[t.id] = (t, t_route, t_snap, batch)
        else:
            runner.finalize(t, batch, t_route, t_snap, results)
    # 检查超时: 仅杀超过 deadline 的 future
    for fut in list(running_futures.keys()):
        if fut.done():
            continue
        t, route, snap, pre, submitted_at = running_futures.get(fut, (None,)*5)
        if t is not None and now - submitted_at > deadline:
            running_futures.pop(fut)
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"执行超时(>{deadline}s)")
            except Exception:
                pass
            results.append((t.id, "timeout", None))
            try:
                _save_trace(t, route, snap, None, None, False)
            except Exception:
                pass
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
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merged: {mr.new_head[:8]}", batch.validation))
            elif mr.status == "conflict":
                tracker.transition(t.id, TaskStatus.CONFLICT_HELD,
                                 error=f"conflict: {mr.conflict_files}")
                _release_ref(t.id)
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"conflict: {mr.conflict_files}", batch.validation))
            else:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"merge {mr.status}")
                _release_ref(t.id)
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merge_failed", batch.validation))
            drained += 1
    return drained


def _run_queue_v3(agents: dict, max_concurrent: int) -> list[tuple]:
    """v3 统一调度循环: dispatch→reap→drain 三步，支持 1..N 并发。"""
    results: list[tuple] = []
    mq = MergeQueue()
    dispatched: set[str] = set()
    running_futures: dict = {}
    pending_batches: dict = {}
    runner = TaskRunner()

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while True:
            _dispatch_ready(dispatched, pool, agents, runner, running_futures)

            if not running_futures and not pending_batches:
                remaining = tracker.ready_tasks(exclude=dispatched)
                if not remaining:
                    break
                # ponytail: avoid busy-wait when queue drains mid-loop
                import time as _time; _time.sleep(0.5)
                continue

            _reap_futures(running_futures, pending_batches, mq, runner, results)
            _drain_pending(pending_batches, mq, results)

            # ── 项目任务全部完成 → 自动触发 test-fix 循环 ──
            _auto_trigger_test_fix(agents, results)

    return results


def _auto_trigger_test_fix(agents: dict, results: list[tuple]) -> None:
    """检查是否有 EXECUTING 阶段的项目所有任务完成，自动推进到 REVIEWING。"""
    try:
        from singularity.scheduler import project as proj_mod
        from singularity.scheduler.workflow import run_test_fix_loop
        for proj in proj_mod.list_all():
            if proj.phase.value == "executing":
                pending = [tid for tid in proj.task_ids
                          if tracker.read_task(tid) and tracker.read_task(tid).status not in (
                              tracker.TaskStatus.DONE, tracker.TaskStatus.ROLLED_BACK,
                              tracker.TaskStatus.FAILED, tracker.TaskStatus.DECOMPOSED)]
                if not pending and proj.task_ids:
                    proj.phase = proj_mod.Phase.REVIEWING
                    proj_mod.save(proj)
                    msg = run_test_fix_loop(proj, agents)
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"Auto REVIEWING → {msg[:120]}",
                        "ts": time.time(), "task_id": proj.id,
                    })
    except Exception:
        pass  # 不阻塞主循环

