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
from singularity.scheduler._worktree import _release_ref, cleanup_task_artifacts
from singularity.scheduler._planner import _maybe_complete_parents
from singularity.scheduler._task_runner import TaskRunner, _archive_task_outcome

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


# ── F1: 集成合并异步化 — 解除调度循环阻塞 ──
# 集成合并含 pytest/docker subprocess (最长 ~150s), 不能在调度循环线程同步跑,
# 否则单项目合并期间全局任务派发/SSE 停摆。用独立线程池异步执行, 完成后回写 phase。
_merge_executor = None  # 惰性重建: 进程重启/shutdown 后 submit 会报 cannot schedule new futures
_merge_inflight: set[str] = set()  # 正在跑集成合并的 project_id, 防重入


def _get_merge_executor() -> ThreadPoolExecutor:
    """惰性获取合并线程池，进程重启/shutdown 后自动重建。"""
    global _merge_executor
    if _merge_executor is None or getattr(_merge_executor, "_shutdown", False):
        _merge_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="integrate")
    return _merge_executor


def run_queue(agents: dict, max_concurrent: int = 1) -> list[tuple]:
    """统一的调度循环入口。v3 支持 1..N 并发。"""
    return _run_queue_v3(agents, max_concurrent)


def schedule_policy(tasks: list) -> list:
    """拓扑自适应调度策略: 综合多信号排序就绪任务。

    信号权重:
      - starvation_score (1.0): 防饥饿, 等越久越优先
      - priority (0.5): 用户指定优先级
      - dependency_weight (0.5): 阻塞越多子任务越优先 (关键路径)
    两档后不再有 level_bonus (E/E+/D 已废弃, 统一 "any")。
    """
    def _score(t) -> float:
        dep_weight = len(t.children) if hasattr(t, 'children') else 0
        return (
            1.0 * t.starvation_score +
            0.5 * t.priority +
            0.5 * dep_weight
        )
    return sorted(tasks, key=_score, reverse=True)


def _dispatch_ready(dispatched: set, pool, agents, runner: TaskRunner,
                    running_futures: dict, mq) -> bool:
    """_run_queue_v3 步骤①②③: 选就绪→cas抢占→提交线程池。返回是否有新派发。"""
    ready = tracker.ready_tasks(exclude=dispatched)
    ready = schedule_policy(ready)
    dispatched_any = False
    for t in ready:
        if t.route_locked:
            # 两档后 RouteResult 不再带 level; route_level 仅作 trace 标签存于 task
            route = router_mod.RouteResult(
                gate_required=t.route_gate,
                task_type=t.route_type)
        else:
            route = router_mod.route(t.description)
        # 同上：重试过的任务才展开历史实际产出。
        pre = pre_mod.pre_search(t.description, route, deep=t.retry_count > 0)
        pre_mod.apply_escalation(route, pre)
        # PENDING/BLOCKED → ROUTED (若尚未路由)
        if t.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
            if not tracker.cas(t.id, t.status, TaskStatus.ROUTED,
                               route_level=t.route_level, route_gate=route.gate_required,
                               route_type=route.task_type):
                continue  # CAS 失败，下一轮重试
            t.status = TaskStatus.ROUTED
        if tracker.cas(t.id, TaskStatus.ROUTED, TaskStatus.DISPATCHED,
                       route_level=t.route_level, route_gate=route.gate_required,
                       route_type=route.task_type):
            from singularity.scheduler.project import repo_root_for
            snap = snap_mod.take(t.id, repo_root=repo_root_for(t))
            tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
            dispatched.add(t.id)
            fut = pool.submit(runner.execute, t, agents, mq)
            running_futures[fut] = (t, route, snap, pre, time.time())
            dispatched_any = True
    return dispatched_any


def _salvage_timed_out(task, elapsed_s: float, snap=None):
    """超时被杀时把**已知事实**留下来：改了哪些文件、有没有提交、实际跑了多久。

    原来这里给 `_save_trace` 传 `None` → trace 里 `changed_files=[]` /
    `elapsed=0` / `tokens=0`，看上去"这个任务什么都没干"。而 worktree 里
    文件其实是写全的（2026-09-11 探路轮实测：3 个任务各撞 900s 被杀，
    事后**完全查不出它们做过什么**）。

    ⚠️ **2026-09-12 更正：只跑 `git status --porcelain` 是不够的 —— 那只看得到
    「未提交」的改动。而 agent 干完一轮会自己 `commit_wt`（"agent changes in
    <taskid>"），提交之后 `git status` 是干净的** → 于是"干完了并提交了"和
    "什么都没干"在 trace 里长得一模一样。
    探路2 的 T2 就是这样：373 行测试 + 完整实现都提交了（worktree 里 `163a52a`），
    trace 里只剩一个 `__pycache__`、`agent_output` 23 字。
    修法：跟**执行前快照的 ref** 比 —— 和 `validator._diff_base` 同一招，
    它的注释早就写着"裸 git diff 恒为空"（09-11 修了 validator，这里没同步）。

    用量（token）：2026-09-12 起**读得回来了** —— 执行器每 dispatch 一次就把累计值
    落一盘（`_exec._persist_partial_usage`），这里读回。**它是下界**（超时那一刻
    正在飞的那次调用没算进去），一次都没落盘才留 None。**None = 不知道，不是没花钱。**
    """
    # 用量先读 —— **必须放在下面的 try 外面**：那段要跑 git、可能抛，
    # 抛了就落进 except 分支，账又跟着丢了（这正是本次要修的东西，别再绕回去）。
    # 执行器每 dispatch 一次落一盘（`_exec._persist_partial_usage`），这里读回来。
    # ⚠️ 它是**下界**：超时那一刻正在飞的那次模型调用不在里面。
    # 一条都没落（比如第一轮就超时）才如实留 None —— None 是"不知道"，不是"没花钱"。
    from singularity.scheduler._exec import read_partial_usage, read_partial_tool_events
    _partial_tokens, _partial_model = read_partial_usage(task.id)
    # tool_events 也一样：它平时只在内存/SSE 里过一遍，超时路径拿不到
    # ⇒ trace 里 `tool_batches.turns` 恒 0，"一次多动作"那套度量在超时任务上没法算。
    _partial_events = read_partial_tool_events(task.id)

    try:
        import subprocess
        from singularity.scheduler.project import repo_root_for
        from singularity.scheduler._git_worktree import _worktrees_dir
        try:
            from singularity.scheduler.validator import _diff_base
            base = _diff_base(snap)
        except Exception:
            base = ""

        repo_root = repo_root_for(task)
        files: list[str] = []
        commits: list[str] = []

        def _git(wt, *args) -> str:
            try:
                return subprocess.run(["git", *args], cwd=str(wt), capture_output=True,
                                      text=True, timeout=10).stdout or ""
            except Exception:
                return ""

        for wt in _worktrees_dir(repo_root).glob(f"{task.id}_*"):
            if base:
                # 与基准比：**已提交 + 未提交一起**看得见
                files += [f.strip() for f in
                          _git(wt, "diff", "--name-only", base).split("\n") if f.strip()]
                commits += [c.strip() for c in
                            _git(wt, "log", "--oneline", f"{base}..HEAD").split("\n")
                            if c.strip()]
            else:
                # 拿不到基准（copy 型快照 / 没传 snap）→ 退回旧行为，但**如实标注**
                for ln in _git(wt, "status", "--porcelain").split("\n"):
                    if ln.strip():
                        files.append(ln[3:].strip())

        files = sorted(set(files))
        tail = (f"，另有 {len(commits)} 个提交未合并（{commits[0].split()[0]}）"
                if commits else "")
        if not base:
            tail += "；⚠️ 拿不到执行前基准，这里**只**能看到未提交的改动"

        class _TimedOutResult:
            """只填**能确定的**字段；token 未知就 None（不是 0）。"""
            changed_files = files
            new_commits = commits
            raw_output = (f"(执行超时(>{int(elapsed_s)}s) 被杀，未及输出总结。"
                          f"磁盘上改动了 {len(files)} 个文件{tail}"
                          + (f"；已知花费 {_partial_tokens} token（下界，最后那次调用未计）"
                             if _partial_tokens else "；用量未知（一次都没落盘）") + ")")
            token_count = _partial_tokens or None
            elapsed = float(elapsed_s)  # 这个是真的：确实跑了这么久
            success = False
            error = "timeout"
            error_kind = "timeout"
            patch_path = ""
            # 边跑边落的那些事件 —— 补上之后超时任务的 tool_batches 才有数
            tool_events = _partial_events

        class _TimedOutDisp:
            executor_result = _TimedOutResult()
            # 带上模型名：记账要按模型单价算钱，空串会进 unpriced_models。
            agent_cfg = {"model": _partial_model}

        return _TimedOutDisp()
    except Exception as e:
        try:
            from singularity.scheduler import witness
            witness.warn("orchestrator", f"salvage_timeout:{type(e).__name__}:{e}"[:120])
        except Exception:
            pass
        # git 那段砸了**不等于账也不用记** —— 用量是先读的，跟 git 没关系。
        # 原来这里直接 return None：调用方连 token 都拿不到，`_save_trace` 还会
        # 拿到 None 写出一份"这个任务什么都没干"的假 trace。两头都错。
        _tok_txt = (f"；已知花费 {_partial_tokens} token（下界）" if _partial_tokens
                    else "；用量未知（一次都没落盘）")

        class _FallbackResult:
            changed_files: list = []
            new_commits: list = []
            raw_output = (f"(执行超时(>{int(elapsed_s)}s) 被杀；改动抢救失败"
                          f"({type(e).__name__}){_tok_txt})")
            token_count = _partial_tokens or None
            elapsed = float(elapsed_s)
            success = False
            error = "timeout"
            error_kind = "timeout"
            patch_path = ""
            tool_events = _partial_events

        class _FallbackDisp:
            executor_result = _FallbackResult()
            agent_cfg = {"model": _partial_model}

        return _FallbackDisp()


def _reap_futures(running_futures: dict, pending_batches: dict,
                  mq, runner: TaskRunner, results: list) -> bool:
    """_run_queue_v3 步骤④: 回收已完成 future → finalize 或入 pending。返回是否有回收。"""
    if not running_futures:
        return False
    now = time.time()
    deadline = 900  # per-future 超时阈值 (15min, 单模型写代码需多轮 读→写→测→改)
    reaped = False

    # 如果有 future 但全都未完成, 先等第一个完成 (最多 10s)
    if not any(f.done() for f in running_futures):
        wait(running_futures.keys(), timeout=10, return_when=FIRST_COMPLETED)

    # 收割所有已完成的
    for fut in list(running_futures.keys()):
        if not fut.done():
            continue
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
            _save_trace(t, route, snap, None, None, False)
            try:
                from singularity.scheduler.project import repo_root_for
                cleanup_task_artifacts(t.id, repo_root_for(t))
            except Exception:
                pass
            continue
        try:  # 性能记录: execute 耗时 (validate/merge 在 finalize 内部, 不计)
            from singularity.scheduler._profiler import record_perf
            # now/submitted_at 都是 time.time() 秒 → 字段叫 _ms, 必须 ×1000
            record_perf(t.id, t.route_type or "", 0.0, (now - submitted_at) * 1000, 0.0, 0.0)
        except Exception:
            pass
        if batch.merge_request:
            mq.submit(batch.merge_request)
            pending_batches[t.id] = (t, t_route, t_snap, batch)
        else:
            runner.finalize(t, batch, t_route, t_snap, results)

    # 超时检测
    for fut in list(running_futures.keys()):
        t, route, snap, pre, submitted_at = running_futures.get(fut, (None,)*5)
        if t is not None and now - submitted_at > deadline:
            running_futures.pop(fut)
            try:
                # 协作式中断: 写取消标记, 让执行线程在下一 turn 边界自行退出
                config.ensure_dirs()
                (config.CANCEL_DIR / f"{t.id}.json").write_text("{}", encoding="utf-8")
            except Exception:
                pass
            try:
                fut.cancel()
            except Exception:
                pass
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"执行超时(>{deadline}s)")
            except Exception:
                pass
            results.append((t.id, "timeout", None))
            # 抢救已知事实再落 trace —— 传 None 会让 trace 变成一份"什么都没干"的假象
            _salvaged = _salvage_timed_out(t, now - submitted_at, snap)
            _save_trace(t, route, snap, _salvaged, None, False)
            # 记账：超时任务**走不到** `_archive_task_outcome`（那是唯一记账入口，
            # 在 finalize 那条路上；超时分支直接判失败、不进 pending）⇒ 花掉的钱
            # 一条都不落。§55 要求超时路径留"烧了多少 token"，这是那半的兑现。
            try:
                _tk = int(getattr(_salvaged.executor_result, "token_count", 0) or 0)
                if _tk > 0:
                    from singularity.scheduler._token_budget import record_tokens
                    _pname = ""
                    _pid = getattr(t, "project_id", "") or ""
                    if _pid:
                        try:
                            from singularity.scheduler import project as _proj_mod
                            _pname = getattr(_proj_mod.load(_pid), "name", "") or ""
                        except Exception:
                            pass        # 拿不到名字不该影响记账
                    record_tokens(
                        project_id=_pid, project_name=_pname, task_id=t.id,
                        model=getattr(_salvaged, "agent_cfg", {}).get("model", ""),
                        level=getattr(t, "route_level", "any"), tokens=_tk,
                        elapsed_s=now - submitted_at,
                    )
            except Exception as _e:
                try:
                    from singularity.scheduler import witness
                    witness.warn("orch", f"timeout_record_tokens:{type(_e).__name__}"[:120])
                except Exception:
                    pass
            # ⚠️ **记忆这一侧也要进** —— 原来超时只写 trace，`index_task` 走的是
            # `_exec.py` 那条正常收尾路径，被 deadline 砍掉就整个跳过。
            # 后果：**干完了但超时的任务，经验永远进不了记忆**（探路2 的 T2/T3 实测：
            # 373 行测试 + 计数核都写了，events.json 里轨迹是 0 字）。
            # 同族：09-12 修的 §55（trace 侧）—— 这是它的记忆侧。
            try:
                from singularity.scheduler import memory as _mem
                _er = getattr(_salvaged, "executor_result", None)
                _mem.index_task(
                    task_id=t.id,
                    description=t.description,
                    changed_files=list(getattr(_er, "changed_files", []) or []),
                    depends_on=getattr(t, "depends_on", []) or [],
                    created_at=getattr(t, "created_at", None),
                    trajectory=str(getattr(_er, "raw_output", "") or ""),
                    force=True,   # 超时条目要留下，别被去重吃掉
                )
            except Exception as _e:
                try:
                    from singularity.scheduler import witness
                    witness.warn("orch", f"timeout_index_task:{type(_e).__name__}"[:120])
                except Exception:
                    pass
            try:
                from singularity.scheduler.project import repo_root_for
                _release_ref(t.id, repo_root=repo_root_for(t))
            except Exception:
                pass
            reaped = True

    return reaped


def _record_ledger(proj, extra: dict) -> None:
    """把这次交付的结局追加进流程账本（P6）。**失败也要记** —— 见调用点注释。"""
    try:
        from singularity.scheduler import _process_ledger
        _process_ledger.record(proj, extra)
    except Exception as e:
        try:
            from singularity.scheduler import witness
            witness.warn("ledger", f"record:{type(e).__name__}:{e}"[:120])
        except Exception:
            pass


def reconcile_projects() -> list[dict]:
    """周期对账：把「状态说的」和「磁盘上真有的」比一遍，漂移就报出来。

    分析里 P4 的"检出时延收尾"：**状态漂移要能早发现**，别等项目卡死了才回头查。
    比三样：

      ① `project.task_ids` 里有多少任务**磁盘上真的还在**
      ② 项目 `phase` 和它 `lineage` 里最后一条 phase 流转**对不对得上**
         （流转断了 = 有地方绕过 `set_phase` 直接改了状态）
      ③ **预算**：花了多少 vs 预算多少

    ⚠️ **只报不改。** 自动"纠正"状态比不报更危险 —— 它会把真的问题抹平成假的
    一致，而这正是本项目反复踩的那类坑（静默兜底 = 编造）。
    """
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import tracker as tracker_mod
    drifts: list[dict] = []
    try:
        projects = proj_mod.recover_all()
    except Exception as e:
        return [{"project_id": "-", "kind": "reconcile_error",
                 "detail": f"recover_all 失败: {type(e).__name__}: {e}"[:100]}]

    for proj in projects or []:
        pid = getattr(proj, "id", "") or "-"
        try:
            ids = list(getattr(proj, "task_ids", []) or [])
            missing = [tid for tid in ids if tracker_mod.read_task(tid) is None]

            lin = [e for e in (getattr(proj, "lineage", []) or [])
                   if isinstance(e, dict) and e.get("action") == "phase"]
            last_to = lin[-1].get("to") if lin else None
            phase = getattr(getattr(proj, "phase", None), "value", None)

            from singularity.scheduler._token_budget import project_budget_state
            level, spent, bmsg = project_budget_state(
                pid, getattr(proj, "token_budget_total", 0) or 0)
        except Exception as e:
            drifts.append({"project_id": pid, "kind": "reconcile_error",
                           "detail": f"{type(e).__name__}: {e}"[:100]})
            continue

        if missing:
            drifts.append({"project_id": pid, "kind": "task_ids_missing",
                           "detail": (f"{len(missing)}/{len(ids)} 个 task_id 磁盘上已不存在: "
                                      f"{[m[-8:] for m in missing[:5]]}")})
        if last_to and phase and last_to != phase:
            drifts.append({"project_id": pid, "kind": "phase_drift",
                           "detail": (f"phase={phase}，但 lineage 最后一条流转到 {last_to}"
                                      f"（有地方绕过 set_phase 改了状态）")})
        if level == "stop":
            drifts.append({"project_id": pid, "kind": "budget", "detail": bmsg})

    for d in drifts:
        try:
            from singularity.scheduler import witness
            witness.warn("reconcile", f"{d['kind']}:{d['project_id'][-8:]}:{d['detail'][:90]}"[:170])
        except Exception:
            pass
    return drifts


def _drain_pending(pending_batches: dict, mq, results: list) -> int:
    """_run_queue_v3 步骤⑥: drain merge queue → 合成功的标 DONE。返回 drain 数。"""
    if not pending_batches:
        return 0
    from singularity.scheduler.project import repo_root_for
    drained = 0
    merge_results = mq.drain()
    for mr in merge_results:
        if mr.task_id in pending_batches:
            t, route, snap, batch = pending_batches.pop(mr.task_id)
            if mr.status == "merged":
                tracker.transition(t.id, TaskStatus.DONE)
                _maybe_complete_parents(t.id)
                _release_ref(t.id, repo_root=repo_root_for(t))
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merged: {mr.new_head[:8]}", batch.validation))
                failure_mode = ""
            elif mr.status == "conflict":
                err = mr.conflict_files or mr.reason or "未知冲突"
                tracker.transition(t.id, TaskStatus.CONFLICT_HELD,
                                 error=f"conflict: {err}")
                _release_ref(t.id, repo_root=repo_root_for(t))
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"conflict: {mr.conflict_files}", batch.validation))
                failure_mode = f"merge_conflict: {err}"
            else:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"merge {mr.status}")
                _release_ref(t.id, repo_root=repo_root_for(t))
                _save_trace(t, route, snap, batch.dispatch_result, batch.validation, False,
                            pre_search_skipped=batch.pre_search_skipped,
                            pre_search_reason=batch.pre_search_reason,
                            pre_search_top_decisions=batch.pre_search_top_decisions,
                            pre_search_memory=batch.pre_search_memory)
                results.append((t.id, f"merge_failed", batch.validation))
                failure_mode = f"merge_{mr.status}"
            # 经验归档 / 用量统计 / 路由学习 —— **这条路径以前完全不调**，
            # 只有 _save_trace 上面调了，于是走合并队列的任务这三件静默少做。
            # 实测（2026-09-11 真机验证）：跑完一个任务 experiences.json /
            # token_usage.json 根本没被创建，route_learner.json 一动不动。
            fresh = tracker.read_task(t.id)
            if fresh is not None:
                t.status = fresh.status      # transition 只改盘上对象，内存里还是旧状态
            _archive_task_outcome(t, route, batch.dispatch_result, failure_mode=failure_mode)
            drained += 1
    return drained


def _run_queue_v3(agents: dict, max_concurrent: int) -> list[tuple]:
    """v3 调度循环: dispatch→reap→drain 三步，支持 1..N 并发。

    修复 reap bug (2026-07-02): 根因不是 daemon 线程/future.done() 异步, 而是
    merge_queue 被硬编码 None → execute 走 v2 直接 merge_back → batch.merge_request
    恒 None → reap 永远走 finalize 分支。现 mq 传入 execute, v3 路径恢复。
    """
    results: list[tuple] = []
    mq = MergeQueue()
    dispatched: set[str] = set()
    running_futures: dict = {}
    pending_batches: dict = {}
    runner = TaskRunner()

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        while True:
            _dispatch_ready(dispatched, pool, agents, runner, running_futures, mq)

            if not running_futures and not pending_batches:
                # 队列无活任务也要推进阶段 (delivering/integrating/reviewing 依赖此推进, 否则永久卡死)
                _auto_trigger_test_fix(agents, results)
                remaining = tracker.ready_tasks(exclude=dispatched)
                if not remaining:
                    break
                time.sleep(0.5)
                continue

            _reap_futures(running_futures, pending_batches, mq, runner, results)
            _drain_pending(pending_batches, mq, results)
            _auto_trigger_test_fix(agents, results)

    return results


def _auto_trigger_test_fix(agents: dict, results: list[tuple]) -> None:
    """检查项目阶段推进: EXECUTING → INTEGRATING → REVIEWING (D2 集成合并)。

    F1: 集成合并异步化 — executing 任务全完成后只推进 phase→INTEGRATING,
    把 _run_integration_merge 扔进 _merge_executor 后台跑, 调度循环不阻塞。
    """
    try:
        from singularity.scheduler import project as proj_mod
        for proj in proj_mod.list_all():
            if proj.phase.value == "executing":
                # P2: 首次进入 → 拆解架构为任务
                if not proj.task_ids:
                    _decompose_and_create_tasks(proj, agents)
                    if not proj.task_ids:
                        # 拆不出任务 = 架构产物不可用。实测链路：融合失败（模型欠费）
                        # → 掉到通用合成 → 产物不是合法架构 JSON → parse_error
                        # → decompose 得 0 个任务。
                        # **必须在这里拦住**：下面的推进判据要求 `proj.task_ids` 非空，
                        # 空的话两条分支都不进 —— 项目**无声地永久卡在 executing**：
                        # 没任务可跑、推不动、没有终态、也没有任何告警（2026-09-11
                        # 真流水线实测：卡了 13 分钟，日志一行都没有）。
                        if not any(i.get("kind") == "no_decomposable_tasks" for i in proj.issues):
                            proj.issues.append({
                                "kind": "no_decomposable_tasks",
                                "message": "架构产物拆不出任务，无可执行内容（架构解析失败？）",
                                "ts": time.time(),
                            })
                            proj_mod.save(proj)
                            witness.warn("orch", f"project_no_tasks:{proj.id[:8]}"[:80])
                        continue
                pending = [tid for tid in proj.task_ids
                          if tracker.read_task(tid) and tracker.read_task(tid).status not in (
                              tracker.TaskStatus.DONE, tracker.TaskStatus.ROLLED_BACK,
                              tracker.TaskStatus.FAILED, tracker.TaskStatus.DECOMPOSED)]
                if not pending and proj.task_ids:
                    # FAILED 也在"终态"集合里，所以这里必须再分一次：**一个都没成功
                    # 就没有可交付的东西**。以前不看这个 —— 7 个任务全失败的项目照样
                    # 一路推到 DONE 并播报"交付完成!"，用户看到的和事实完全相反
                    # （2026-09-11 探针实测：7 任务全 failed，项目 phase=done）。
                    done_ids = [tid for tid in proj.task_ids
                                if (t := tracker.read_task(tid))
                                and t.status == tracker.TaskStatus.DONE]
                    if not done_ids:
                        # 记一条 issue 并**停在这里等人处理**，不再往交付推。
                        # 已记过就不再重复（调度循环每 tick 都会走到这里，否则刷屏）。
                        if not any(i.get("kind") == "all_tasks_failed" for i in proj.issues):
                            proj.issues.append({
                                "kind": "all_tasks_failed",
                                "message": f"{len(proj.task_ids)} 个任务全部失败，无可交付内容",
                                "ts": time.time(),
                            })
                            proj_mod.save(proj)
                            witness.warn("orch", f"project_all_tasks_failed:"
                                                 f"{proj.id[:8]}:{len(proj.task_ids)}"[:80])
                    else:
                        # D2: 推进到集成合并阶段, 异步跑 (不阻塞调度循环)
                        n_failed = len(proj.task_ids) - len(done_ids)
                        proj.set_phase(proj_mod.Phase.INTEGRATING,
                                        f"任务全部到终态(失败 {n_failed})→集成合并")
                        proj_mod.save(proj)
                        _pending_sse_events.append({
                            "kind": "system",
                            # 别再说"全部任务完成" —— 有失败时如实报数
                            "msg": (f"项目 {proj.name}: {len(done_ids)} 个任务完成"
                                    + (f"，{n_failed} 个失败" if n_failed else "")
                                    + "，进入集成合并"),
                            "ts": time.time(), "project_id": proj.id,
                        })
                        if proj.id not in _merge_inflight:
                            _merge_inflight.add(proj.id)
                            _get_merge_executor().submit(_run_integration_merge_async, proj.id, agents)
            elif proj.phase.value == "delivering":
                # S1: 自动交付打包 (轻量, 同步即可)
                ok, detail = _run_delivery(proj)
                if ok:
                    proj.set_phase(proj_mod.Phase.DONE, f"交付完成: {detail[:60]}")
                    proj_mod.save(proj)
                    _record_ledger(proj, {"delivery": "ok", "detail": detail[:120]})
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付完成! {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
                else:
                    # 失败也记 —— **账本要的是结局，不是成功集**（只记成功的话，
                    # digest 里永远一片大好，下一轮照着做还是撞同一堵墙）
                    _record_ledger(proj, {"delivery": "failed", "detail": detail[:120]})
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付失败, 需人工处理 - {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
            elif proj.phase.value == "reviewing":
                # P3: 验收阶段 — 直接推GATE3等人审（FIXING 已删，状态不可达）
                from singularity.scheduler.workflow import run_test_fix_loop
                run_test_fix_loop(proj, agents)
            elif proj.phase.value == "integrating":
                # 重启恢复: 若没在跑则提交 (已在跑的跳过防重入)
                if proj.id not in _merge_inflight:
                    _merge_inflight.add(proj.id)
                    _get_merge_executor().submit(_run_integration_merge_async, proj.id, agents)
    except Exception as e:
        # S6: 不再静默吞错 — 记录并通知, 避免项目卡死无反馈
        try:
            witness.warn('orch', f'auto_trigger:{e}')
        except Exception:
            pass


def _decompose_and_create_tasks(proj, agents: dict) -> None:
    """P2 兜底: 项目进了 executing 却一个任务都没有时，从架构再拆一次。

    正常路径用不到它 —— `run_phase` → `_workflow_phases._run_execution` 在项目进入
    executing **之前**就把任务建好了。这条只在"没建上"时兜底。

    ⚠️ 它以前读 `<项目目录>/architecture.json` —— **全仓没有任何代码写这个文件**
    （唯一提及它的就是这里），所以永远卡在第一步 `if not arch_path.exists(): return`：
    一次都没救成功过。真进入"executing 且没任务"的项目，只能一直卡着（2026-09-11
    真流水线实测：卡了 13 分钟、零日志）。
    改成读 `proj.architecture` —— 跟 `_run_execution` 同源。
    """
    try:
        arch_json = proj.architecture or {}
        if not isinstance(arch_json, dict):
            return

        from singularity.scheduler.execution_judge import decompose_architecture
        tasks = decompose_architecture(arch_json)
        if not tasks:
            return

        # 项目代码写进独立 git 仓库 (与奇点仓库隔离, 修复 #1)
        from singularity.scheduler.project import ensure_repo
        ensure_repo(proj.id)

        # 与 _run_execution 对齐。这条兜底原来只写 desc，丢了四样东西：
        #   ① **depends_on 完全没有** —— 架构里的 DAG 被压平，本该串行的任务同时开跑，
        #      产物互相看不见 + 争同一批文件 + 合并冲突；
        #   ② route_level 取 t["suggested_level"]，而那是"层"(impl/backend)不是档位；
        #   ③ 不绑 route_role → 角色提示词不会注入（那条链路本来就死过两个半月）；
        #   ④ 旧 task_ids 不清 —— 与重规划那条同一个病（见 _workflow_phases）。
        from singularity.scheduler.roles import get_phase_role
        from singularity.scheduler.project import Phase
        role_key = get_phase_role(Phase.EXECUTING) or "implementer"
        proj.task_ids = []
        id_map: dict[str, str] = {}
        for idx, t in enumerate(tasks):
            local_id = t.get("id", "") or f"T{idx+1}"
            arch_deps = t.get("depends_on", []) or t.get("depends_on_local_id", [])
            dep_ids = [id_map[d] for d in arch_deps if d in id_map]
            task = tracker.create(t["desc"], project_id=proj.id, depends_on=dep_ids)
            tracker.transition(task.id, tracker.TaskStatus.PENDING,
                             route_level="any",
                             route_locked=True,
                             route_role=role_key)
            proj.task_ids.append(task.id)
            id_map[local_id] = task.id

        from singularity.scheduler.project import save
        save(proj)
        _pending_sse_events.append({
            "kind": "system", "msg": f"架构拆解完成: {len(tasks)} 个任务已入队",
            "ts": time.time(), "project_id": proj.id,
        })
    except Exception as e:
        try:
            witness.warn('orch', f'decompose:{e}')
        except Exception:
            pass


def _run_integration_merge_async(project_id: str, agents: dict) -> None:
    """F1: 后台线程跑集成合并 + 后续 phase 推进。

    完成后回写 phase (→REVIEWING 调 run_test_fix_loop, 或 →EXECUTING/GATE2 重试),
    释放 _merge_inflight, SSE 通知。任何异常都不抛出 (后台线程无法冒泡)。
    """
    from singularity.scheduler import project as proj_mod
    try:
        proj = proj_mod.load(project_id)
        if proj is None:
            return
        ok, detail = _run_integration_merge(proj)
        if ok:
            # D1: 检查审查失败次数是否触顶
            from singularity.scheduler._review import check_review_fail_limit
            fail_check = check_review_fail_limit(proj.id,
                getattr(proj, 'review_failures', 0))
            if fail_check["blocked"]:
                proj.set_phase(proj_mod.Phase.GATE2, fail_check["reason"])
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": fail_check["reason"],
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                proj.set_phase(proj_mod.Phase.REVIEWING, "集成合并通过")
                proj_mod.save(proj)
                from singularity.scheduler.workflow import run_test_fix_loop
                msg = run_test_fix_loop(proj, agents)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并通过 → REVIEWING {msg[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
        else:
            # 集成合并失败 → 计数
            integrate_fails = getattr(proj, 'integrate_failures', 0) + 1
            proj.integrate_failures = integrate_fails
            if integrate_fails >= proj_mod._INTEGRATE_MAX_RETRIES:
                # 触顶 → 打回架构 (GATE2)
                proj.set_phase(proj_mod.Phase.GATE2, f"集成合并{integrate_fails}次失败(上限{proj_mod._INTEGRATE_MAX_RETRIES})")
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并{integrate_fails}次失败→升GATE2: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                # 回实现层重试
                proj.set_phase(proj_mod.Phase.EXECUTING, f"集成合并失败第{integrate_fails}次→回实现层重试")
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并失败({integrate_fails}/{proj_mod._INTEGRATE_MAX_RETRIES})→回实现层: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
    except Exception as e:
        try:
            witness.warn('orch', f'integrate_async:{e}')
            _pending_sse_events.append({
                "kind": "system", "msg": f"集成合并异常: {e}",
                "ts": time.time(), "task_id": project_id,
            })
        except Exception:
            pass
    finally:
        _merge_inflight.discard(project_id)


def _run_integration_merge(proj) -> tuple[bool, str]:
    """D2 集成合并: 拓扑合并 + 集成测试 + 冒烟构建。

    返回 (ok, detail)。
    ponytail: 集成测试跑 test_cases.json 中的 integration 用例, 没有则跳过。
    """
    import subprocess
    from pathlib import Path as _Path
    from singularity.scheduler.project import repo_dir as _repo_dir
    # 修复: 原来取 config.PROJECT_ROOT = 奇点自己的仓库 —— 于是集成检查的是奇点的工作区
    # (你正在改奇点时项目会被无理由打回), 跑的是奇点的 test_cases.json。
    root = str(_repo_dir(proj.id))
    if not (_Path(root) / ".git").exists():
        return False, f"项目仓库不存在: {root}"

    # 1) 拓扑合并: 检查所有 worktree 已合并 (merge_queue drain 已处理)
    #    此处做最终一致性检查: git status 是否干净
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10, cwd=root)
        if r.returncode != 0:
            return False, f"git status 失败: {r.stderr[:100]}"
        dirty = [l for l in (r.stdout or "").split("\n") if l.strip() and not l.startswith("??")]
        if dirty:
            return False, f"工作区不干净 ({len(dirty)} 个变更)"
    except Exception as e:
        return False, f"git status 异常: {e}"

    # 2) 集成测试: 跑 test_cases.json 中的 integration 用例
    tc_path = _Path(root) / "test_cases.json"
    if tc_path.exists():
        try:
            import json as _json
            tc = _json.loads(tc_path.read_text())
            integration_cases = tc.get("integration", [])
            if integration_cases:
                # 跑 pytest (如果项目有测试)
                r = subprocess.run(
                    ["python3", "-m", "pytest", "-q", "--tb=short", "-k", "test_integration"],
                    capture_output=True, text=True, timeout=120, cwd=root)
                if r.returncode != 0:
                    return False, f"集成测试失败: {(r.stdout+r.stderr)[:200]}"
        except Exception as e:
            return False, f"集成测试异常: {e}"

    # 3) 冒烟构建检查: 是否存在可构建产物
    # ponytail: 轻量检查 — 有 Dockerfile 则验证语法, 有 pyproject.toml 则 pip install --dry-run
    if (_Path(root) / "Dockerfile").exists():
        try:
            r = subprocess.run(["docker", "build", "--check", "."], capture_output=True, text=True, timeout=30, cwd=root)
            if r.returncode != 0:
                return False, f"Docker build check 失败: {(r.stdout+r.stderr)[:200]}"
        except FileNotFoundError:
            pass  # docker 不可用, 跳过
        except Exception as e:
            return False, f"冒烟构建异常: {e}"

    return True, "集成合并通过"


def _run_delivery(proj) -> tuple[bool, str]:
    """S1 交付: 代码归档 + 产物打包 + 交付文档 + 报告归档。

    返回 (ok, detail)。
    ponytail: 不自动部署到生产 — 部署风险高且涉及用户基础设施。
    """
    import subprocess
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from singularity.scheduler.project import repo_dir as _repo_dir
    # 修复: 原来是奇点自己的仓库 —— tag 打在奇点上, 产物探测读的是奇点的 pyproject.toml
    root = str(_repo_dir(proj.id))
    if not (_Path(root) / ".git").exists():
        return False, f"项目仓库不存在: {root}"

    deliverables = {
        "code_ref": "", "artifacts": [], "docs": [], "reports": [],
        "handed_over_at": _dt.now().isoformat(),
    }

    # 1) 代码归档: 打 tag
    try:
        tag_name = f"release/{proj.id}-{_dt.now().strftime('%Y%m%d%H%M')}"
        r = subprocess.run(["git", "tag", tag_name], capture_output=True, text=True, timeout=15, cwd=root)
        if r.returncode == 0:
            deliverables["code_ref"] = tag_name
        else:
            # 无 git 或不成功 → 用 HEAD commit
            r2 = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=root)
            deliverables["code_ref"] = r2.stdout.strip()[:12] if r2.returncode == 0 else "unknown"
    except Exception:
        deliverables["code_ref"] = "unknown"

    # 2) 交付文档: 收集 README + 部署说明
    # 应用数据留在奇点数据目录（原先 root=.qidian 恰好等于 QIDIAN_DIR，改成项目仓库后必须钉住）
    docs_dir = config.QIDIAN_DIR / "deliverables" / proj.id
    docs_dir.mkdir(parents=True, exist_ok=True)
    for fname in ["README.md", "DEPLOY.md", "README"]:
        src = _Path(root) / fname
        if src.exists():
            import shutil
            dst = docs_dir / fname
            shutil.copy2(str(src), str(dst))
            deliverables["docs"].append(fname)

    # 3) 报告归档: QA/Security/Test cases
    for fname in ["qa_report.json", "security_report.json", "test_cases.json", "review_report.json"]:
        src = _Path(root) / fname
        if src.exists():
            deliverables["reports"].append(fname)

    # 4) 产物打包: 按项目类型
    if (_Path(root) / "pyproject.toml").exists():
        deliverables["artifacts"].append({"name": "python-package", "type": "package"})
    if (_Path(root) / "Dockerfile").exists():
        deliverables["artifacts"].append({"name": "docker-image", "type": "image"})
    if (_Path(root) / "package.json").exists():
        deliverables["artifacts"].append({"name": "npm-package", "type": "package"})

    # 写交付清单
    manifest_path = docs_dir / "delivery_manifest.json"
    manifest_path.write_text(_json.dumps(deliverables, ensure_ascii=False, indent=2))

    return True, f"交付完成: tag={deliverables['code_ref'][:16]}, 文档={len(deliverables['docs'])}, 制品={len(deliverables['artifacts'])}"

