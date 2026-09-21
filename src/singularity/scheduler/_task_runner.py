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

from singularity.scheduler._exec import (
    _read_planner_patch,
    _run_with_retry,
    _save_trace,
    decompose,
    read_partial_started_at,
)
from singularity.scheduler._planner import (
    _materialize_in_main,
    _maybe_complete_parents,
    materialize_plan,
)

# ── 内部模块 (执行链) ──────────────────────────────────────
from singularity.scheduler._types import (
    _MAX_DEPTH,
    RunContext,
    _pending_sse_events,
)
from singularity.scheduler.goal_loop import GoalLoop

_GOAL_RE = _re.compile(r'^\[Goal\]\s*(.+?)\n', _re.ASCII)

# ── 画像 ──────────────────────────────────────────
from singularity.scheduler import chancellor as chan_mod

# ── 业务依赖 ────────────────────────────────────────────
from singularity.scheduler import config, tracker, witness
from singularity.scheduler import memory as mem_mod
from singularity.scheduler import pre_search as pre_mod
from singularity.scheduler import route_learner as rl_mod
from singularity.scheduler import router as router_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import validator as val_mod
from singularity.scheduler._token_budget import record_tokens
from singularity.scheduler.tracker import TaskStatus

# ═══════════════════════════════════════════════════════════════
# 任务收尾的三件事（两条路径共用）
# ═══════════════════════════════════════════════════════════════

def requeue_if_never_dispatched(task) -> bool:
    """**一次都没派发过** ⇒ 回 PENDING 重排（带次数上限）。改了终态返回 True。

    🔴 来历（2026-09-21 `round-20260921c` 真机，用户拍板）：
    并发只有 2、任务又互相依赖（都得等脚手架），**排在后面的任务，900 秒有效期
    在排队时就被等没了** —— 那一轮 11 个任务里 8 个失败，其中 **3 个一次模型调用
    都没发起过**（T5/T8/T11），却被判到终态；T5 的判词甚至是 QA 的
    「无文件改动；检测到 1 个偷懒信号」——**读起来像它偷懒，其实它没轮到**。
    实测时刻：T8 死在 18:47:08，而 T6 最后一次派发是 18:47:07 —— 它是**在别人
    让出位子后 1 秒被砍的**（同形的还有 T5@18:33:36、T10/T11@19:00:39）。

    判据用现成的 `read_partial_started_at`（它就是为"压根没发起过"造的，
    见 `_exec._mark_dispatch_started`）—— **不新加字段**。

    ⚠️ **探不出来就不改判定**（返回 False，照旧判终态）：判据本身失效时
    默认行为必须是最保守的那一边，不能因为探测失败就把任务放回队列转圈。
    ⚠️ 次数用现成的 `retry_count` / `max_retries` —— 它在这个文件里的语义
    本来就是"这个任务被重排了几次"（见下面 QA 中间态那段）。
    """
    try:
        if read_partial_started_at(task.id) is not None:
            return False                      # 派发过 ⇒ 那是"做过但没成"，照旧判终态
    except Exception as e:
        # 判据本身坏了 ⇒ 不改判定（返回 False，照旧判终态）。**但要出声** ——
        # 哑了的话"这条规则从没生效"和"这轮没任务落进来"长得一模一样。
        witness.warn("orch", f"requeue_probe_failed:{task.id}:"
                             f"{type(e).__name__}:{e}"[:180],
                     key="requeue_probe_failed")
        return False
    rc = int(getattr(task, "retry_count", 0) or 0)
    cap = int(getattr(task, "max_retries", 3) or 3)
    if rc >= cap:
        return False                          # 重排到上限 —— 不再无限转
    try:
        tracker.transition(
            task.id, TaskStatus.PENDING,
            error=f"未轮到：排队把 {config.TASK_DEADLINE_S:.0f}s 死线等没了、"
                  f"一次都没派发 → 重新入队（第 {rc + 1}/{cap} 次）",
            retry_count=rc + 1)
    except Exception as e:
        witness.warn("orch", f"requeue_transition_failed:{task.id}:"
                             f"{type(e).__name__}:{e}"[:180],
                     key="requeue_transition_failed")
        return False
    # ⚠️ 这两句**不包 try**：`witness.warn` 自己保证不上抛（见 `witness.warn` 的 S5 修复），
    # 包一层 `except: pass` 反而会被 `test_no_new_silent_except` 判成新的静默 except。
    witness.warn("orch", f"requeue_never_dispatched:{task.id}:"
                         f"第{rc + 1}/{cap}次"[:180],
                 key="requeue_never_dispatched")
    return True


def _archive_task_outcome(task, route, disp_result, failure_mode: str = "") -> None:
    """任务结束后归档：经验 / 用量 / 路由学习。三件都写盘。

    **必须在两条收尾路径上都调**：
      · `TaskRunner.finalize`      —— 单任务直接合并（v2 路径）
      · `orchestrator._drain_pending` —— v3 并行，任务走合并队列，合并完才收尾

    以前只有 finalize 调，`_drain_pending` 自己重写了一遍收尾（transition +
    _save_trace），**这三件整个漏掉**。实测后果（2026-09-11 真机验证）：
    跑完一个任务，`experiences.json` / `token_usage.json` **根本没被创建**，
    `route_learner.json` 一动不动 —— 而 `_save_trace` 是两边都有的，所以
    `events.json` 会正常长大，从外面看像是"归档跑了"，其实只跑了一半。

    每件各自 try：一件炸不该连累另外两件（以前 archive_experience 一抛，
    同一 try 里的用量统计和路由学习一起被跳过）。
    """
    exec_out = disp_result.executor_result if disp_result else None
    model = getattr(disp_result, 'agent_cfg', {}).get("model", "") if disp_result else ""
    tokens = getattr(exec_out, 'token_count', 0) if exec_out else 0
    elapsed = getattr(exec_out, 'elapsed', 0) if exec_out else 0

    try:
        mem_mod.archive_experience(
            task_id=task.id, description=task.description,
            status="done" if task.status == TaskStatus.DONE else "failed",
            route_level=task.route_level,
            model=model, elapsed_ms=elapsed, tokens=tokens,
            failure_mode=failure_mode,
            files_changed=getattr(exec_out, 'changed_files', []) if exec_out else [],
        )
    except Exception as e:
        witness.warn('orch', f'archive_experience:{e}'[:80])

    try:
        # 带上项目名：原来只传 id，`project_name` 字段就永远是空串，
        # 而 `by_project` 的响应里挂着它 —— 名不副实，谁用谁踩坑。
        _pname = ""
        _pid = getattr(task, 'project_id', '')
        if _pid:
            try:
                from singularity.scheduler import project as _proj
                _p = _proj.load(_pid)
                _pname = getattr(_p, 'name', '') or ''
            except Exception:
                pass      # 拿不到名字不该影响记账
        record_tokens(project_id=_pid, project_name=_pname, task_id=task.id,
                      model=model, level=task.route_level, tokens=tokens,
                      elapsed_s=elapsed)
    except Exception as e:
        # 静默吞掉 = token 账目悄悄丢失，成本统计对不上也查不出原因
        witness.warn('orch', f'record_tokens:{e}'[:80])

    try:
        # 走 record_outcome: load→record→save 全程持锁。分三步平铺的话，
        # 并发任务的整份快照会互相覆盖（lost update）。
        rl_mod.record_outcome(
            task_type=getattr(route, 'task_type', 'default'), model=model,
            level=task.route_level,
            # 成功与否看最终状态, 不看 batch.ok (QA 拒绝的任务 batch.ok 仍可能为 True)
            success=(task.status == TaskStatus.DONE),
            elapsed_ms=elapsed, tokens=tokens,
        )
    except Exception as e:
        witness.warn('orch', f'route_learner:{e}'[:80])


# ═══════════════════════════════════════════════════════════════
# TaskRunner
# ═══════════════════════════════════════════════════════════════

class TaskRunner:
    """单任务生命周期。

    封装: route → pre_search → execute → trace → QA。
    orchestrator 只需 import 这一个类。
    """

    def execute(self, task, agents: dict, merge_queue=None, deadline_at: float = 0.0):
        """执行单个任务: 路由→预检→Goal/委员会/普通→返回(batch, route, snap)。

        merge_queue: v3 并行时由 _run_queue_v3 传入, 使 _exec.run 走 v3 路径
          (commit_wt + 填 merge_request, 不直接 merge_back)。
          None → v2 路径 (直接 merge_back)。修复 reap bug 根因#1。

        deadline_at: 任务死亡时刻的**绝对值**，由调用方在 **submit 那一刻**算好传进来
          —— 池子排队那段也就算在同一把尺上了（见下面起表处那条注释）。
          `0.0`（测试/直接调用）⇒ 退回"本线程开头起算"。
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
        # 重试过（retry_count>0）才值得花 token 读历史全文 —— 跟调研阶段
        # "项目栽过才走 deep" 是同一条规则，只是信号换成任务自己的重试次数。
        pre = pre_mod.pre_search(task.description, route, deep=task.retry_count > 0)
        pre_mod.apply_escalation(route, pre)
        # 模型排名
        try:
            pid = getattr(task, "project_id", "")
            if pid:
                from . import project as proj_mod
                proj = proj_mod.load(pid)
                if proj:
                    pass
        except Exception as e:
            witness.warn('orch', f'{e}')
        # 快照 (修复 #1: 项目任务快照项目 repo)
        from . import project as proj_mod
        snap = snap_mod.take(task.id, repo_root=proj_mod.repo_root_for(task))
        # 起表 —— 这是**任务级唯一那把尺**。orchestrator 的 900s 收割也是从"派发那一刻"
        # 算的（`running_futures[fut] = (…, time.time())`）。
        # ⚠️ 但"本线程开头"**不等于**"派发那一刻"：池子满时任务先在队列里排队，
        # worker 才起来（2026-09-14 核外派「改动审阅」）。所以正常路径下由 orchestrator
        # 在 submit 前把死线**算好传进来**；`deadline_at=0`（测试/直接调用）才退回本行起算。
        ctx = RunContext(batch_id=task.id, snapshot_ref=snap.ref,
                         snapshot_method=snap.method, merge_queue=merge_queue,
                         deadline_at=deadline_at or (time.time() + config.TASK_DEADLINE_S))
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
            from ._types import BatchOutput
            from .executors.base import ExecutorResult
            batch = BatchOutput(ok=g_result.success, task_id=task.id,
                        term_reason=f"goal_{'met' if g_result.success else 'exhausted'}_{g_result.iterations}iter",
                        tool_events=[], turn_count=g_result.iterations,
                        validation=val_mod.ValidationReport(
                            verdict="通过" if g_result.success else "阻断",
                            action="pass" if g_result.success else "abort",
                            unverified=[f"Goal循环 {g_result.iterations}轮, 满足={g_result.success}"]))
            batch.dispatch_result = type('obj', (object,), {
                'executor_result': ExecutorResult(success=g_result.success, raw_output=g_result.final_output),
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
        # 人工取消：终态意图，不再拆分、不再跑 QA。见下面的 `elif cancelled` / QA 那段。
        # ⚠️ 前缀 `"cancelled"` 而不是 `"cancelled_by_user"` —— 还有 `cancelled_during_pause`
        # 那一族（`_exec.py:540`），漏了它等于同一个洞换个字符串（2026-09-14，逆向审抓到）。
        cancelled = term_reason.startswith("cancelled")
        if batch.planner_decomposed:
            try:
                _materialize_in_main(batch, task)
            except Exception as e:
                witness.warn('orch', f'materialize:{e}')
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
        elif cancelled:
            # 用户点过取消 ⇒ 收 FAILED。**不能落进下面那条 else**：它带"重试耗尽 →
            # 自动拆分再提交"的分支，那会在用户已经叫停之后再派一批子任务出去花钱。
            tracker.transition(task.id, TaskStatus.FAILED, error="用户手动取消")
            reason = "cancelled"
        else:
            d_plan = _read_planner_patch(task.id)
            # ⚠️ **`escalation_exhausted` 已经没有任何写者**（2026-09-14，外派⑤核出、我复核属实）：
            # 生产侧 `_exec.py:755` 早就把它改名成 `no_escalation_path`（为了把真实原因写进终态，
            # 见那里的注释），而**消费侧没跟着改** ⇒ 这个"D 方案自动修复"分支**永远不触发**，
            # 而三条测试一直拿旧词当输入、全绿地给死功能作证。
            # 两个词都收：新词是现役，旧词只在历史 trace / 旧数据里出现。
            if d_plan and ("no_escalation_path" in term_reason
                           or "escalation_exhausted" in term_reason):
                fix_task = tracker.create(
                    f"[D方案执行] {task.description[:80]}",
                    depends_on=[task.id], depth=task.depth)
                tracker.transition(fix_task.id, TaskStatus.PENDING,
                                 route_locked=True)
                tracker.transition(task.id, TaskStatus.FAILED,
                                 error=f"已生成修复任务 {tracker.short_id(fix_task.id)}: {term_reason}")
                reason = f"auto_fix: {tracker.short_id(fix_task.id)}"
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
                                        f"[{tracker.short_id(task.id)}] 自动拆分: {est['task_count']}个子任务, "
                                        f"预估 ~{est['total_tokens']:,} tokens"
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
        # 取消的任务不跑 QA —— `supervise()` 是一次真模型调用，对已经叫停的任务
        # 再花这笔钱纯属浪费，结论也没人会看。
        if not qa_verdict and not cancelled:
            try:
                from .project import repo_root_for
                from .supervisor import our_side_stop_of, qa_context, supervise
                changed = disp_result.executor_result.changed_files if disp_result else []
                constraints, checklist = qa_context(task)
                sv = supervise(task.description, changed, constraints, checklist,
                              getattr(disp_result.executor_result, 'raw_output', '') if disp_result else '',
                              task.id, repo_root=str(repo_root_for(task)),
                              # 同 `_exec` 那处：把"是我方掐断的"带进判据（审计 A4）
                              our_side_stop=our_side_stop_of(
                                  disp_result.executor_result if disp_result else None))
                qa_verdict = sv.verdict
                qa_issues = list(sv.issues)
            except Exception as e:
                witness.warn('orch', f'{e}')
        if qa_verdict == "fail":
            qa_blocked = qa_fail = True
            # 🔴 「一次都没派发过」不是 QA 判词能描述的事 —— 它压根没轮到。
            # 放在**终态判定这一处**（而不是某一条来路上）是有意的：不管它是从
            # QA 判死、还是从收割超时过来的，这个判据都兜得住（"兜底要穷举它绕过了什么"）。
            if requeue_if_never_dispatched(task):
                qa_fail = False          # 没进终态：它还活着，别把它当失败推进
                reason += "; 未轮到→PENDING"
            else:
                tracker.transition(task.id, TaskStatus.FAILED,
                                 error="QA:fail: " + "; ".join(qa_issues[:2]))
        elif qa_verdict and qa_verdict != "pass":
            qa_blocked = True
            # 修复 reap bug 根因#2: QA 中间态(retry/escalate/block)转 PENDING 重新入队,
            # 回写 RUNNING 会永久卡死。
            # retry_count 必须在这里 +1：它是"这个任务被重排了几次"的计数，
            # 而全仓原来只有 tracker.recover()（进程重启）会写它 ——
            # 于是 _task_runner 里"重试耗尽 → 自动拆分再提交"那条分支**永远进不去**
            # （正常一次调度里 retry_count 恒为 0），任务只会无限重排。
            _rc = int(getattr(task, 'retry_count', 0) or 0) + 1
            tracker.transition(task.id, TaskStatus.PENDING,
                             error=f"QA:{qa_verdict}: " + "; ".join(qa_issues[:2]),
                             retry_count=_rc)
            reason += f"; QA:{qa_verdict}→PENDING(第{_rc}次)"

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
                    pre_search_memory=batch.pre_search_memory,
                    # 取消路径手里有事件、没有 disp_result —— 不传这份就白攥着（见 _save_trace）
                    tool_events=batch.tool_events)
        # T1 挂钩: 任务完成后归档经验 / 用量 / 路由学习。
        # 抽成共享函数是必须的 —— v3 的合并路径（orchestrator._drain_pending）以前
        # **一次都没调过这三件**，只有 _save_trace 两边都有，于是走合并队列的任务
        # 静默少做三件事（实测 experiences.json / token_usage.json 根本没被创建）。
        _archive_task_outcome(
            task, route, disp_result,
            # failure_mode 跟最终状态走: QA 拦下的任务 batch.ok 仍为 True, 不能记空
            failure_mode=("" if task.status == TaskStatus.DONE
                          else (f"QA:{qa_verdict}" if qa_blocked else validation.verdict)),
        )
        # 工具事件已由 openai_agent 实时上流(append 到 _pending_sse_events), 此处不再批量推, 避免重复
        turn = getattr(batch, 'turn_count', 0) or 0
        if turn > 0:
            _pending_sse_events.append({
                "kind": "turn", "msg": f"[{tracker.short_id(task.id)}] 推理完成，共 {turn} 轮",
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
            witness.warn('orch', f'{e}')
        results.append((task.id, reason, validation))
        return reason
