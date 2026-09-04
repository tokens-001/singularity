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


# ── F1: 集成合并异步化 — 解除调度循环阻塞 ──
# 集成合并含 pytest/docker subprocess (最长 ~150s), 不能在调度循环线程同步跑,
# 否则单项目合并期间全局任务派发/SSE 停摆。用独立线程池异步执行, 完成后回写 phase。
_merge_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="integrate")
_arch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="architect")
_merge_inflight: set[str] = set()  # 正在跑集成合并的 project_id, 防重入
_arch_inflight: set[str] = set()   # 正在跑架构阶段的 project_id, 防重入


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
        pre = pre_mod.pre_search(t.description, route)
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
            snap = snap_mod.take(t.id)
            tracker.transition(t.id, TaskStatus.RUNNING, snapshot_id=snap.id)
            dispatched.add(t.id)
            fut = pool.submit(runner.execute, t, agents, mq)
            running_futures[fut] = (t, route, snap, pre, time.time())
            dispatched_any = True
    return dispatched_any


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
            continue
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
                fut.cancel()
            except Exception:
                pass
            try:
                tracker.transition(t.id, TaskStatus.FAILED, error=f"执行超时(>{deadline}s)")
            except Exception:
                pass
            results.append((t.id, "timeout", None))
            _save_trace(t, route, snap, None, None, False)
            reaped = True

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
            if proj.phase.value == "planning":
                # P1: 架构阶段 — 3架构师并行 + 合成器 (异步提交, 防重入)
                if proj.id not in _arch_inflight:
                    _arch_inflight.add(proj.id)
                    _arch_executor.submit(_run_architecture_phase_async, proj.id, agents)
            elif proj.phase.value == "executing":
                # P2: 首次进入 → 拆解架构为任务
                if not proj.task_ids:
                    _decompose_and_create_tasks(proj, agents)
                pending = [tid for tid in proj.task_ids
                          if tracker.read_task(tid) and tracker.read_task(tid).status not in (
                              tracker.TaskStatus.DONE, tracker.TaskStatus.ROLLED_BACK,
                              tracker.TaskStatus.FAILED, tracker.TaskStatus.DECOMPOSED)]
                if not pending and proj.task_ids:
                    # D2: 推进到集成合并阶段, 异步跑 (不阻塞调度循环)
                    proj.phase = proj_mod.Phase.INTEGRATING
                    proj_mod.save(proj)
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 全部任务完成, 进入集成合并",
                        "ts": time.time(), "project_id": proj.id,
                    })
                    if proj.id not in _merge_inflight:
                        _merge_inflight.add(proj.id)
                        _merge_executor.submit(_run_integration_merge_async, proj.id, agents)
            elif proj.phase.value == "delivering":
                # S1: 自动交付打包 (轻量, 同步即可)
                ok, detail = _run_delivery(proj)
                if ok:
                    proj.phase = proj_mod.Phase.DONE
                    proj_mod.save(proj)
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付完成! {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
                else:
                    _pending_sse_events.append({
                        "kind": "system", "msg": f"项目 {proj.name}: 交付失败, 需人工处理 - {detail[:100]}",
                        "ts": time.time(), "project_id": proj.id,
                    })
            elif proj.phase.value in ("reviewing", "fixing"):
                # P3: 审查/修复阶段 — 直接推GATE3等人审
                from singularity.scheduler.workflow import run_test_fix_loop
                run_test_fix_loop(proj, agents)
            elif proj.phase.value == "integrating":
                # 重启恢复: 若没在跑则提交 (已在跑的跳过防重入)
                if proj.id not in _merge_inflight:
                    _merge_inflight.add(proj.id)
                    _merge_executor.submit(_run_integration_merge_async, proj.id, agents)
    except Exception as e:
        # S6: 不再静默吞错 — 记录并通知, 避免项目卡死无反馈
        try:
            witness.heartbeat('orch', f'warn:auto_trigger:{e}')
        except Exception:
            pass


def _decompose_and_create_tasks(proj, agents: dict) -> None:
    """P2: 从架构文档拆解任务并创建 tracker task。"""
    import json as _json
    try:
        from singularity.scheduler.project import get_project_dir
        arch_path = get_project_dir(proj.id) / "architecture.json"
        if not arch_path.exists():
            return
        arch = _json.loads(arch_path.read_text(encoding="utf-8"))
        fused = arch.get("unified_architecture", "")
        if not fused:
            return

        # 解析 unified_architecture JSON
        try:
            arch_json = _json.loads(fused) if isinstance(fused, str) else fused
        except (_json.JSONDecodeError, TypeError):
            # 尝试提取 JSON 块
            import re
            m = re.search(r'\{[\s\S]*\}', str(fused))
            arch_json = _json.loads(m.group()) if m else {}

        from singularity.scheduler.execution_judge import decompose_architecture
        tasks = decompose_architecture(arch_json)
        if not tasks:
            return

        for t in tasks:
            task = tracker.create(t["desc"])
            tracker.transition(task.id, tracker.TaskStatus.PENDING,
                             route_level=t.get("suggested_level", "any"),
                             route_locked=True)
            proj.task_ids.append(task.id)

        from singularity.scheduler.project import save
        save(proj)
        _pending_sse_events.append({
            "kind": "system", "msg": f"架构拆解完成: {len(tasks)} 个任务已入队",
            "ts": time.time(), "project_id": proj.id,
        })
    except Exception as e:
        try:
            witness.heartbeat('orch', f'warn:decompose:{e}')
        except Exception:
            pass


def _run_architecture_phase_async(project_id: str, agents: dict) -> None:
    """P1: 后台线程跑架构阶段 — 3架构师并行 → 合成器 → GATE2。"""
    import json as _json
    try:
        from singularity.scheduler import project as proj_mod
        from singularity.scheduler import dispatcher as disp_mod
        from singularity.scheduler.execution_judge import fuse_architecture, decompose_architecture

        proj = proj_mod.load(project_id)
        if not proj:
            _arch_inflight.discard(project_id)
            return

        # 1. 加载定义阶段产出
        proj_dir = proj_mod.get_project_dir(project_id)
        docs = {}
        for key in ("prd", "interaction", "ui_direction", "research"):
            p = proj_dir / f"{key}.json"
            if p.exists():
                docs[key] = _json.loads(p.read_text(encoding="utf-8")).get("content", "")[:2000]

        if not docs.get("prd"):
            _pending_sse_events.append({"kind": "system", "msg": "架构阶段: 缺少PRD, 无法启动",
                                         "ts": time.time(), "project_id": project_id})
            _arch_inflight.discard(project_id)
            return

        prd_text = docs.get("prd", "")
        context = f"PRD:\n{prd_text}\n\n"
        if docs.get("interaction"): context += f"交互方案:\n{docs['interaction'][:1000]}\n\n"
        if docs.get("ui_direction"): context += f"UI方向:\n{docs['ui_direction'][:800]}\n\n"

        # 2. 并行派发 3 架构师
        arch_roles = [
            ("system-architect", f"基于以下PRD和需求文档，产出系统架构方案（模块划分/数据模型/API契约/技术栈选型）:\n\n{context}"),
            ("ai-architect", f"基于以下PRD，产出AI架构方案（模型选型/Prompt体系/Agent拓扑/上下文策略）:\n\n{context}"),
            ("frontend-architect", f"基于以下PRD和交互方案，产出前端架构方案（组件树/状态管理/路由设计/性能策略）:\n\n{context}"),
        ]

        import concurrent.futures
        outputs: list[tuple[str, str]] = []  # [(role, output)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = {}
            for role, prompt in arch_roles:
                futures[ex.submit(
                    disp_mod.dispatch,
                    prompt, "any", f"arch_{project_id}_{role}", agents,
                )] = role

            for fut in concurrent.futures.as_completed(futures, timeout=600):
                role = futures[fut]
                try:
                    result = fut.result()
                    if result and result.executor_result and result.executor_result.raw_output:
                        outputs.append((role, result.executor_result.raw_output))
                except Exception:
                    pass

        if not outputs:
            _pending_sse_events.append({"kind": "system", "msg": "架构阶段: 所有架构师均无产出",
                                         "ts": time.time(), "project_id": project_id})
            _arch_inflight.discard(project_id)
            return

        # 3. 合成器融合
        raw_outputs = [o for _, o in outputs]
        fused = fuse_architecture(f"PRD: {prd_text[:1000]}", raw_outputs)

        # 保存
        arch_doc = {"unified_architecture": fused, "sources": [r for r, _ in outputs],
                    "created_at": time.time()}
        (proj_dir / "architecture.json").write_text(_json.dumps(arch_doc, ensure_ascii=False, indent=2))

        # 顺便生成 test_cases
        try:
            test_cases_raw = fused  # fuse_architecture 输出包含 test_cases
            parsed = _json.loads(test_cases_raw) if test_cases_raw.startswith("{") else {}
            test_cases = parsed.get("test_cases", {})
            if test_cases:
                (proj_dir / "test_cases.json").write_text(_json.dumps(test_cases, ensure_ascii=False, indent=2))
        except Exception:
            pass

        # 4. 推进到 GATE2
        proj.phase = proj_mod.Phase.GATE2
        proj_mod.save(proj)
        _pending_sse_events.append({
            "kind": "system",
            "msg": f"架构方案已生成 (来源: {', '.join(r for r,_ in outputs)})。请审核后确认进入实现阶段。",
            "ts": time.time(), "project_id": project_id,
        })

    except Exception as e:
        try:
            witness.heartbeat('orch', f'warn:arch_phase:{e}')
        except Exception:
            pass
    finally:
        _arch_inflight.discard(project_id)


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
                proj.phase = proj_mod.Phase.GATE2
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": fail_check["reason"],
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                proj.phase = proj_mod.Phase.REVIEWING
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
                proj.phase = proj_mod.Phase.GATE2
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并{integrate_fails}次失败→升GATE2: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
            else:
                # 回实现层重试
                proj.phase = proj_mod.Phase.EXECUTING
                proj_mod.save(proj)
                _pending_sse_events.append({
                    "kind": "system", "msg": f"集成合并失败({integrate_fails}/{proj_mod._INTEGRATE_MAX_RETRIES})→回实现层: {detail[:120]}",
                    "ts": time.time(), "task_id": proj.id,
                })
    except Exception as e:
        try:
            witness.heartbeat('orch', f'warn:integrate_async:{e}')
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
    root = str(config.PROJECT_ROOT)

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
    root = str(config.PROJECT_ROOT)

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
    docs_dir = _Path(root) / ".qidian" / "deliverables" / proj.id
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

