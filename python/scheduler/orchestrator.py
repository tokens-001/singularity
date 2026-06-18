"""orchestrator.py — 调度闭环核心

设计契约 (修复 #7): 只有主线程写 tracker。
  - worker 线程 (v3 ThreadPool) 里的 run() 只做纯执行 (dispatch + validate),
    返回 BatchOutput, 不调任何 tracker.transition/cas/create。
  - 主线程的调度循环 (_run_queue_v2 / _run_queue_v3) 负责所有 tracker 写入。
  这样"单线程 tracker 写入"不变量真正成立, 不需要锁。

v2 (max_concurrent=1): run() 内直接 merge_back, 顺序执行, 主线程同步调
v3 (max_concurrent>1): worker 跑 run() 产出 MergeRequest 填进 BatchOutput,
  主线程回收后 mq.submit() + drain, 合成功才 DONE (修复 #3)
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from dataclasses import dataclass, field
from typing import Optional

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
except ImportError:  # noqa: BLE001
    MergeQueue = None  # type: ignore
    MergeRequest = None  # type: ignore

# 分解深度上限 (防无限递归)
_MAX_DEPTH = 3


_PLANNER_PREAMBLE = """\
[系统指令] 你是架构分析器 (只读 Planner)。
职责: 分析问题、设计方案，任何人不得要求你修改文件。
输出格式:
1. ## 问题分析 — 拆解问题本质、定位根因
2. ## 方案设计 — 具体步骤、架构决策、取舍理由
3. ## 改动清单 — 建议改哪些文件、怎么改 (不实际修改)
4. ## 风险提示 — 边界条件、回滚策略、注意事项

若任务可拆分为多个独立子任务, 在末尾输出 ```json 块:
```json
[
  {"desc": "子任务描述", "suggested_level": "E", "depends_on_local_id": []}
]
```
depends_on_local_id 用从 0 开始的索引指代同数组内的子任务。
不可拆分则不输出 json 块。

---
"""


@dataclass
class RunContext:
    batch_id: str
    snapshot_ref: str
    worktree_base: str = ""
    merge_queue: "Optional[MergeQueue]" = None


@dataclass
class BatchOutput:
    """worker 线程的纯返回值, 不含任何 tracker 写操作 (修复 #7)。"""
    ok: bool
    task_id: str = ""
    dispatch_result: object = None
    term_reason: str = ""
    validation: object = None
    merge_request: "Optional[MergeRequest]" = None
    planner_decomposed: bool = False  # planner 分解了子任务 (parent 不该 DONE)
    pre_search_skipped: bool = False
    pre_search_reason: str = ""
    pre_search_top_decisions: list = field(default_factory=list)
    pre_search_memory: dict = field(default_factory=dict)


def _inject_memory(description: str) -> str:
    """MAGMA 记忆注入: 查询相关历史，生成简短上下文前缀。"""
    try:
        mem_mod._ensure_dir()
        events = mem_mod._load_events()
        if not events or len(events) < 2:
            return ""
        result = mem_mod.query(description, beam_width=2, max_hops=2)
        items = result.get("traversal", {}).get("narrative", [])
        if not items:
            return ""
        lines = ["[相关历史]"]
        count = 0
        for item in items[:3]:
            desc = item.get("description", "")[:60]
            score = item.get("score", 0)
            if score < 0.01:
                continue
            similarity = item.get("similarity", "")
            tag = f"(相似度 {similarity})" if similarity else ""
            lines.append(f"- {desc} {tag}")
            count += 1
        if count == 0:
            return ""
        lines.append("参考以上历史任务的改动方案。\n")
        return "\n".join(lines)
    except Exception as e:
        try: witness.heartbeat("memory", f"warn:inject_memory:{e}")
        except: pass
        return ""


def _build_project_context(task) -> str:
    """项目上下文注入: 从 project 提取调研推荐+约束+验收标准。

    只在 task 有 project_id 且 project 存在时生效。
    返回空字符串表示无需注入。
    """
    pid = getattr(task, 'project_id', '')
    if not pid:
        return ""
    try:
        from .project import load as _load_proj
        proj = _load_proj(pid)
        if not proj:
            return ""
        parts = [f"[项目上下文] {proj.name}"]
        # 调研推荐
        if proj.research_report:
            rec = proj.research_report.get("recommendation", "")
            pitfalls = proj.research_report.get("pitfalls", [])
            if rec:
                parts.append(f"调研推荐: {rec[:200]}")
            if pitfalls:
                parts.append(f"注意事项: {'; '.join(pitfalls[:3])}")
        # 约束清单
        if proj.constraints_checklist:
            parts.append(f"约束清单: {'; '.join(proj.constraints_checklist[:5])}")
        # 架构验收标准 (匹配子任务)
        if proj.architecture:
            tasks = proj.architecture.get("tasks", [])
            desc = getattr(task, 'description', '')
            for tdef in tasks:
                if tdef.get("title", "") in desc or tdef.get("id", "") in desc:
                    acceptance = tdef.get("acceptance", "")
                    if acceptance:
                        parts.append(f"验收标准: {acceptance}")
                    break
        return "\n".join(parts) if len(parts) > 1 else ""
    except Exception:
        return ""


def run(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """纯执行: dispatch + validate, 返回 BatchOutput。

    修复 #7: 不调任何 tracker.transition/cas/create。调用方 (主线程) 负责状态机。
    修复 #5: 入口 _read(task.id) 重读, 不依赖传入的内存 Task 对象 (可能陈旧)。
    修复 #2: v3 路径用 commit_wt 拿到含改动的 commit, 再构造 MergeRequest。
    """
    # 修复 #5: 重读文件, 不信任传入的 task 内存对象
    fresh = tracker._read(task.id)
    if fresh is not None:
        task = fresh

    level = task.route_level
    route_gate = task.route_gate
    route_type = task.route_type

    feedback = ""
    last_validation = val_mod.ValidationReport(
        verdict="未知", action="abort",
        unverified=["dispatcher 未产出可校验结果"],
    )
    disp_result = None
    term_reason = "未执行"
    pending_merge_req = None
    planner_decomposed = False

    snap = _SnapProxy(ctx.snapshot_ref)

    # ── 执行前钩子 ──
    pre_warnings = val_mod.pre_execution_hook(task.description, snap)
    if pre_warnings:
        for w in pre_warnings:
            witness.heartbeat(task.id, f"pre_hook: {w[:80]}")

    # 容灾: 获取 fallback 链, 当前 agent 失败自动切下一个
    # 如果任务已重试多次，强制优先用 premium 模型
    total_failures = getattr(task, 'retry_count', 0)
    force_premium = total_failures >= 2  # 2次失败 → 跳过便宜模型直上 premium
    fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level)
    if force_premium and fallback_chain:
        # 把 premium 模型移到最前面 (model 名含 glm 或 opus)
        premium = [a for a in fallback_chain if any(p in a.get('model','').lower() for p in ('glm','opus'))]
        cheap = [a for a in fallback_chain if a not in premium]
        fallback_chain = premium + cheap
    tried_models: set[str] = set()

    while True:
        if not fallback_chain:
            break
        agent_cfg = fallback_chain[0]
        level_max = agent_cfg.get("max_turns", config.DEFAULT_MAX_TURNS)
        is_planner = agent_cfg.get("mode") == "planner"

        wt = _maybe_create_worktree(task.id, level, agent_cfg, ctx.snapshot_ref)
        cwd = str(wt.path) if wt else ""

        if is_planner and wt:
            _lock_wt(wt)

        for turn in range(1, level_max + 1):
            witness.heartbeat(task.id, level)

            # 检查人工取消标记
            cancel_path = config.CANCEL_DIR / f"{task.id}.json"
            if cancel_path.exists():
                cancel_path.unlink()
                wt and _cleanup_wt(wt)
                return BatchOutput(
                    ok=False, task_id=task.id,
                    term_reason="cancelled_by_user",
                    validation=val_mod.ValidationReport(
                        verdict="阻断", action="abort",
                        unverified=["用户手动取消"],
                    ),
                )

            effective_task = task.description
            # ── MAGMA 记忆注入 ──
            if turn == 1 and feedback == "":
                mem_ctx = _inject_memory(task.description)
                if mem_ctx:
                    effective_task = mem_ctx + "\n\n" + effective_task
            if is_planner:
                effective_task = _PLANNER_PREAMBLE + effective_task
            # ── 项目上下文注入 ──
            proj_ctx = _build_project_context(task)
            if proj_ctx:
                effective_task = proj_ctx + "\n\n---\n" + effective_task

            disp_result = disp_mod.dispatch(
                effective_task, level, task.id, agents,
                feedback=feedback, baseline_ref=ctx.snapshot_ref, cwd=cwd,
            )
            exec_result = disp_result.executor_result

            if not exec_result.success:
                # 容灾: 切下一个 agent
                tried_models.add(agent_cfg.get("model", ""))
                fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                _cleanup_wt(wt)
                if fallback_chain:
                    witness.heartbeat(task.id, f"fallback: {agent_cfg.get('model','')}→{fallback_chain[0].get('model','')}")
                    break  # 跳出 turn loop, 用新 agent
                last_validation = val_mod.ValidationReport(
                    verdict="未知",
                    action="abort",
                    unverified=[f"executor 失败 (已试 {len(tried_models)} agent): {exec_result.error_kind}: {exec_result.error}"],
                    turns_used=turn,
                )
                break

            # planner: 存 patch + 尝试分解 (先建后定: decompose 非空才 materialize)
            if is_planner:
                _save_planner_patch(task.id, exec_result.raw_output)
                subtasks = decompose(exec_result.raw_output)
                if subtasks:
                    # 分解成功 → 主线程 materialize (worker 不写 tracker)
                    # 这里标记让主线程处理, BatchOutput 带回 subtasks 信息
                    planner_decomposed = True
                    _cleanup_wt(wt)
                    return BatchOutput(
                        ok=True, task_id=task.id, dispatch_result=disp_result,
                        term_reason=f"decomposed (level={level}, turn={turn})",
                        validation=val_mod.ValidationReport(
                            verdict="通过", action="pass",
                            unverified=[f"planner 分解出 {len(subtasks)} 子任务"],
                        ),
                        planner_decomposed=True,
                    )
            elif wt:
                if ctx.merge_queue is not None:
                    # v3: commit_wt 拿含改动的 commit (修复 #2), 不直接 merge
                    branch_ref = commit_wt(wt)
                    if branch_ref:
                        _anchor_ref(task.id, branch_ref)  # 防 gc 回收 (重要 #3)
                        pending_merge_req = _build_merge_request(
                            task, branch_ref, ctx.snapshot_ref,
                        )
                else:
                    # v2: 直接 merge_back
                    mr = wt_merge_back(wt)
                    if not mr.ok:
                        reason = mr.reason or f"冲突文件: {mr.conflicts}"
                        last_validation = val_mod.ValidationReport(
                            verdict="阻断", action="abort",
                            unverified=[f"worktree merge 失败: {reason}"],
                            turns_used=turn,
                        )
                        term_reason = f"merge_conflict (level={level}, turn={turn})"
                        _cleanup_wt(wt)
                        return BatchOutput(
                            ok=False, task_id=task.id, dispatch_result=disp_result,
                            term_reason=term_reason, validation=last_validation,
                        )

            validation = val_mod.validate(
                candidate=exec_result.raw_output,
                gate_required=route_gate,
                task_type=route_type,
                changed_files=exec_result.changed_files,
                snap=snap, turn=turn, max_turns=level_max,
            )
            validation.confidence = quality.get("confidence", 0.5)
            validation.quality_signals = quality.get("quality_signals", {})
            last_validation = validation

            if validation.action == "pass":
                _cleanup_wt(wt)
                return BatchOutput(
                    ok=True, task_id=task.id, dispatch_result=disp_result,
                    term_reason=f"pass (level={level}, turn={turn})",
                    validation=validation,
                    merge_request=pending_merge_req,
                )

            if validation.action == "retry":
                fb = [json.dumps(validation.evidence, ensure_ascii=False, indent=2)]
                if quality.get("warnings"):
                    fb.append("质量警告:\n" + "\n".join(f"- {w}" for w in quality["warnings"]))
                if quality.get("failure_kind") and quality["failure_kind"] != "ok":
                    fb.append(f"失败类型: {quality['failure_kind']}, 置信度: {quality['confidence']:.2f}")
                feedback = "\n\n".join(fb)
                continue

            _cleanup_wt(wt)
            return BatchOutput(
                ok=False, task_id=task.id, dispatch_result=disp_result,
                term_reason=f"{validation.action} (level={level}, turn={turn})",
                validation=validation,
            )

        _cleanup_wt(wt)
        next_level = disp_mod.escalate(level)
        if next_level is None:
            term_reason = f"escalation_exhausted (level={level})"
            break
        level = next_level
        # 升级后重建 fallback 链 (新层级的新 agent 列表)
        fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level)
        tried_models = set()
        feedback = ""
        feedback = ""

    return BatchOutput(
        ok=False, task_id=task.id, dispatch_result=disp_result,
        term_reason=term_reason, validation=last_validation,
    )


def _build_merge_request(task, branch_ref: str, base_ref: str) -> "MergeRequest":
    changed = set(changed_files_between(base_ref, branch_ref))
    deps = list(task.depends_on) if task.depends_on else []
    return MergeRequest(
        task_id=task.id, branch=branch_ref, base_ref=base_ref,
        changed_files=changed, depends_on=deps,
    )


def _anchor_ref(task_id: str, commit_sha: str) -> None:
    """给悬空 commit 打锚定 ref, 防 git gc 回收 (重要 #3)。"""
    import subprocess as _sp
    ref = f"refs/qidian/pending/{task_id}"
    _sp.run(
        ["git", "update-ref", ref, commit_sha],
        cwd=str(config.PROJECT_ROOT), capture_output=True,
    )


def _release_ref(task_id: str) -> None:
    """清理锚定 ref (merge 成功/放弃后调)。"""
    import subprocess as _sp
    ref = f"refs/qidian/pending/{task_id}"
    _sp.run(
        ["git", "update-ref", "-d", ref],
        cwd=str(config.PROJECT_ROOT), capture_output=True,
    )


class _SnapProxy:
    def __init__(self, ref: str):
        self.ref = ref
        self.id = ref
        self.method = "git"


def _maybe_create_worktree(task_id: str, level: str, agent_cfg: dict, snapshot_ref: str = ""):
    if agent_cfg.get("sandbox") != "worktree":
        return None
    try:
        return wt_create(task_id, level, base_ref=snapshot_ref)  # 修复 #8
    except Exception:  # noqa: BLE001
        return None


def _cleanup_wt(wt) -> None:
    if wt is None:
        return
    _unlock_wt(wt)
    try:
        wt_cleanup(wt)
    except Exception:  # noqa: BLE001
        pass


def _lock_wt(wt: Worktree) -> None:
    """只读锁: 文件 r--r--r--, 目录 r-xr-xr-x (防遍历但可进入子路径)。"""
    if wt is None:
        return
    import stat, subprocess as _sp
    r = _sp.run(["git", "ls-files"], cwd=str(wt.path), capture_output=True, text=True)
    if r.returncode != 0:
        return
    for f in r.stdout.strip().splitlines():
        fp = wt.path / f
        try:
            if fp.is_dir():
                fp.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0555
            elif fp.is_file():
                fp.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0444
        except OSError:
            pass


def _unlock_wt(wt: Worktree) -> None:
    """解锁: 文件 rw-r--r--, 目录 rwxr-xr-x。"""
    if wt is None:
        return
    import stat, subprocess as _sp
    r = _sp.run(["git", "ls-files"], cwd=str(wt.path), capture_output=True, text=True)
    if r.returncode != 0:
        return
    for f in r.stdout.strip().splitlines():
        fp = wt.path / f
        try:
            if fp.is_dir():
                fp.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0755
            elif fp.is_file():
                fp.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 0644
        except OSError:
            pass


# ── 队列调度 (主线程, 所有 tracker 写在这) ────────────────────────────
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

        tracker.transition(
            task.id, TaskStatus.ROUTED,
            route_level=route.level, route_gate=route.gate_required,
            route_type=route.task_type,
        )
        snap = snap_mod.take(task.id)
        tracker.transition(task.id, TaskStatus.RUNNING, snapshot_id=snap.id)

        ctx = RunContext(batch_id=task.id, snapshot_ref=snap.ref, merge_queue=None)

        # D层委员会: 多agent并行出方案，合成最优
        d_agents = agents.get("D", [])
        use_committee = route.level == "D" and len(d_agents) >= 2
        if use_committee:
            batch = _run_committee(task, ctx, agents, d_agents)
        else:
            batch = _run_with_retry(task, ctx, agents)

        batch.pre_search_skipped = pre.skipped
        batch.pre_search_reason = pre.reason
        batch.pre_search_top_decisions = pre.top_decisions
        batch.pre_search_memory = {
            "intent": pre.memory.intent,
            "narrative": pre.memory.narrative,
            "entity_matches": pre.memory.entity_matches,
            "graph_coverage": pre.memory.graph_coverage,
        }

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
            # 从项目加载约束和验收标准
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
                tracker.transition(task.id, task.status, error=f"QA:{sv.verdict}: " + "; ".join(sv.issues[:2]))
        except Exception as e:
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
                done, _ = wait(running_futures.keys(), return_when=FIRST_COMPLETED)
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


# ═══════════════════════════════════════════════════════════
# MAGMA 慢通道: 后台内存整合 (Slow Path — Structural Consolidation)
# ═══════════════════════════════════════════════════════════

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
    import os, urllib.request, json as _json

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

        body = _json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"]

        import re as _re
        m = _re.search(r'\{[^}]+\}', raw)
        if m:
            return _json.loads(m.group())
        return {"is_causal": False, "reason": "parse_error"}
    except Exception as e:
        return {"is_causal": False, "reason": f"llm_error:{e}"}


def _materialize_in_main(batch: BatchOutput, parent_task) -> None:
    """planner 分解后, 主线程 materialize (worker 不写 tracker)。

    parent 转 DECOMPOSED, materialize_plan 建 children。
    """
    tracker.transition(parent_task.id, TaskStatus.DECOMPOSED)
    subtasks = decompose(batch.dispatch_result.executor_result.raw_output)
    if subtasks:
        materialize_plan(parent_task.id, subtasks)


def _maybe_complete_parents(task_id: str) -> None:
    """task 完成后冒泡触发父聚合, 递归到根 (修复 重要 #4: 嵌套分解不冒泡)。"""
    changed = False
    for p in tracker._tasks_dir().glob("*.json"):
        try:
            parent = tracker.Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
        if task_id in parent.children and parent.status not in {
            TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK
        }:
            if tracker.maybe_complete_parent(parent.id):
                # parent 刚转 DONE/FAILED → 递归冒泡到 grandparent
                _maybe_complete_parents(parent.id)
            break  # 一棵树里 task_id 只属于一个 parent


def _run_committee(task, ctx: RunContext, agents: dict, d_agents: list) -> BatchOutput:
    """D层委员会: 所有D agent并行出方案，独立不互看，合成最优。

    每个人扮演不同视角:
      - Opus: 稳——风险、边界、回滚
      - GPT:  新——替代思路、业界实践
      - DeepSeek: 实——落地性、文件量、复杂度
    """
    models = [a.get("model", "?") for a in d_agents]
    plans = []

    # 并行调度，每个人拿到相同的任务 + 不同视角
    futures = {}
    with ThreadPoolExecutor(max_workers=len(d_agents)) as pool:
        for agent_cfg in d_agents:
            single = dict(agents)
            single["D"] = [agent_cfg]
            fut = pool.submit(_run_committee_member, task, ctx, single, agent_cfg)
            futures[fut] = agent_cfg

        for fut in as_completed(futures):
            agent_cfg = futures[fut]
            try:
                batch = fut.result(timeout=300)
                if batch.ok and batch.dispatch_result:
                    raw = batch.dispatch_result.executor_result.raw_output
                    plans.append({
                        "model": agent_cfg.get("model", "?"),
                        "plan": raw[:8000],  # 截断，委员会不拼全文
                        "term": batch.term_reason,
                        "batch": batch,
                    })
            except Exception as e:
                plans.append({"model": agent_cfg.get("model", "?"), "error": str(e)})

    if not plans:
        # 全失败 → 回退普通模式
        return _run_with_retry(task, ctx, agents)

    # 合成: 机械拼接 + 标注各方贡献
    synthesis = _synthesize_plans(task.description, plans, models)

    # 用第一个成功的 batch 作为载体，替换 raw_output 为合成结果
    winner = next((p for p in plans if "batch" in p), None)
    if winner:
        batch = winner["batch"]
        exec_result = batch.dispatch_result.executor_result
        exec_result.raw_output = synthesis
        from . import dispatcher as _disp
        batch.dispatch_result = _disp.DispatchResult(
            level="D", agent_cfg={"model": "committee"},
            executor_result=exec_result, attempts=1,
        )
        batch.term_reason = f"committee({len(plans)}/{len(d_agents)}): " + ", ".join(p["model"] for p in plans)
        return batch

    return BatchOutput(ok=False, task_id=task.id,
                       term_reason="committee_all_failed",
                       validation=val_mod.ValidationReport(verdict="阻断", action="abort",
                           unverified=[f"委员会 {len(d_agents)} 人全败"]))


def _run_committee_member(task, ctx, agents, agent_cfg):
    """委员会单个成员: 按模型注入视角后独立执行。"""
    model = agent_cfg.get("model", "?")
    perspectives = {
        "opus": "你关注: 风险点、边界条件、回滚策略。方案必须稳健，不能炸。",
        "gpt": "你关注: 有没有完全不同的思路？业界最新实践是什么？大胆提替代方案。",
        "deepseek": "你关注: 这方案E/E+能落地吗？需要多少个文件？现有代码风格兼容吗？复杂度实际是多少？",
        "glm": "你关注: 和现有架构的一致性。不要引入不兼容的变更。",
    }
    extra = ""
    for k, v in perspectives.items():
        if k in model.lower():
            extra = f"\n\n[你的视角] {v}"
            break

    if extra:
        # 临时加视角到 task description
        orig = task.description
        task.description = f"{orig}{extra}"
        try:
            return _run_with_retry(task, ctx, agents)
        finally:
            task.description = orig  # 恢复
    return _run_with_retry(task, ctx, agents)


def _synthesize_plans(task_desc: str, plans: list, models: list) -> str:
    """委员会真合成: LLM 分析各方方案，提取共识+冲突+择优合并。

    先尝试调 DeepSeek (E层廉价) 做语义合成。
    LLM 失败时回退到机械拼接。
    """
    # 把各方方案压缩为摘要
    summaries = []
    for i, p in enumerate(plans):
        model = p.get("model", "?")
        plan_text = p.get("plan", p.get("error", "无输出"))
        # 提取 JSON 块或纯文本
        import re as _re2
        m = _re2.search(r"```json\s*\n(.*?)\n```", plan_text, _re2.DOTALL)
        if m:
            body = m.group(1)[:3000]
        else:
            body = plan_text[:3000]
        summaries.append(f"### 方案{i+1}: {model}\n{body}")

    summary_text = "\n\n".join(summaries)

    # 尝试 LLM 合成
    synthesis = _llm_synthesize(task_desc, summary_text, models)
    if synthesis:
        return synthesis

    # 回退: 机械拼接
    lines = [
        f"# 委员会方案合成 (机械)",
        f"任务: {task_desc[:200]}",
        f"参与: {', '.join(models)}",
        "",
        summary_text,
        "",
        "## 对比建议",
        f"共 {len(plans)} 份方案。请 Owner 对比各方案的架构/任务分解/风险，取长补短。",
    ]
    return "\n".join(lines)


def _llm_synthesize(task_desc: str, summary_text: str, models: list) -> str | None:
    """用 DeepSeek (E层) 分析多方方案，输出结构化合成。

    返回: 合成文本, 或 None (LLM不可用时回退机械拼接)
    """
    import os, urllib.request, json as _json

    prompt = f"""你是一个架构委员会主席。有 {len(models)} 位架构师({', '.join(models)})各自提出了方案。

## 任务
{task_desc[:500]}

## 各方方案
{summary_text[:8000]}

## 你的工作
请输出以下结构化分析(用中文)：

### 1. 共识点
各方方案一致同意的地方。

### 2. 分歧点
各方方案有冲突的地方，列出不同立场。

### 3. 择优决策
对每个分歧点，选择最好的方向并说明理由。

### 4. 最终方案
综合各方优点，给出一个最终方案概要（架构+任务分解+关键风险）。

直接输出markdown，不要JSON包裹。"""

    try:
        # 获取 E 层 agent 配置
        agents = disp_mod.load_agents()
        e_agents = agents.get("E", [])
        if not e_agents:
            return None

        e_cfg = e_agents[0]
        api_key = os.environ.get(e_cfg.get("api_key_env", ""), "")
        if not api_key:
            return None

        base_url = e_cfg.get("base_url", "https://api.deepseek.com/v1")
        model = e_cfg.get("model", "deepseek-chat")

        body = _json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]

        # 拼接最终输出
        header = f"""# 委员会方案合成 (LLM)
任务: {task_desc[:200]}
参与: {', '.join(models)}

"""
        return header + content

    except Exception:
        return None


def _run_with_retry(task, ctx: RunContext, agents: dict) -> BatchOutput:
    """worker 线程入口: 纯执行 + 重试。

    修复 #7: 不写 tracker。重试时回传 retry 信号, 主线程决定是否再派发。
    本函数在 worker 线程跑, 只调 run() (纯执行), 不碰 tracker。
    """
    retry = 0
    while retry <= task.max_retries:
        batch = run(task, ctx, agents)

        # ── 执行后钩子 ──
        try:
            exec_result = batch.dispatch_result.executor_result if batch.dispatch_result else None
            if exec_result:
                snap = snap_mod.Snapshot(id=task.id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                post_warnings = val_mod.post_execution_hook(exec_result, snap)
                if post_warnings:
                    batch.term_reason += f"; post_hook: {', '.join(post_warnings)}"
        except Exception as e:
            try: witness.heartbeat(task_id=task.id, status="error", detail=f"post_hook:{e}")
            except: pass

        if batch.validation.action == "pass" or batch.planner_decomposed:
            return batch
        if "merge_conflict" in batch.term_reason:
            return batch

        retry += 1
        if retry > task.max_retries:
            return batch

        # 重试 (v2: 主仓库可能有 merge 残留; v3: worktree 已在 run() 内部清理)
        if ctx.merge_queue is None:
            # v2: 主仓库 rollback 到快照基线 (只在主线程, 不并发)
            try:
                snap = snap_mod.Snapshot(id=ctx.batch_id, method="git", ref=ctx.snapshot_ref, created_at=0.0)
                snap_mod.rollback(snap)
            except Exception:  # noqa: BLE001
                pass
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
    except Exception:  # noqa: BLE001
        pass

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
        final_status = "rolled_back" if rolled_back else task.status.value
        mem_mod.update_attrs(task.id,
            status=final_status,
            route_level=route.level if route else "",
            route_type=route.task_type if route else "",
        )
    except Exception:
        pass


# ── Planner 分解 (decompose + materialize_plan 完整实现) ──────────────
import re as _re


def decompose(planner_raw_output: str) -> list[dict]:
    """解析 planner stdout 里的 ```json 子任务块。

    返回 [{desc, suggested_level, depends_on_local_id}, ...]。
    无 JSON 块或解析失败 → [] (当普通方案, 不分解)。
    """
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
            "suggested_level": str(item.get("suggested_level", "E")),
            "depends_on_local_id": list(item.get("depends_on_local_id", [])),
        })
    return subtasks


def materialize_plan(parent_id: str, subtasks: list[dict]) -> list[str]:
    """把子任务 dict 列表创建为真实 Task, 挂到 parent.children。

    - local_id → 真实 task_id 映射
    - 拓扑排序 (按 depends_on_local_id)
    - 环检测 → parent FAILED("循环依赖")
    - depth 上限检查 (>= _MAX_DEPTH 拒绝)
    - tracker.create(parent_id=parent_id) + set_children
    返回子 task_id 列表。
    """
    parent = tracker._read(parent_id)
    if parent is None:
        return []

    # depth 上限: parent 已达上限 → 拒绝再分解, parent 转 FAILED
    if parent.depth >= _MAX_DEPTH:
        tracker.transition(
            parent_id, TaskStatus.FAILED,
            error=f"分解深度达上限 {_MAX_DEPTH}, 拒绝再分解",
        )
        return []

    # 拓扑排序 + 环检测
    order = _topo_sort(subtasks)
    if order is None:
        tracker.transition(parent_id, TaskStatus.FAILED, error="循环依赖, 子任务图有环")
        return []

    # local_id → 真实 task_id
    local_to_real: dict[int, str] = {}
    child_ids: list[str] = []
    for local_id in order:
        st = subtasks[local_id]
        # 依赖的 local_id → 真实 id
        real_deps = [local_to_real[d] for d in st["depends_on_local_id"] if d in local_to_real]
        child = tracker.create(
            desc=st["desc"],
            priority=parent.priority,
            depends_on=real_deps,
            parent_id=parent_id,
        )
        # 子任务路由预设 (planner 建议的 level), 锁死防 router 覆盖 (建议 #6)
        tracker.transition(
            child.id, TaskStatus.PENDING,
            route_level=st["suggested_level"],
            route_locked=True,
        )
        local_to_real[local_id] = child.id
        child_ids.append(child.id)

    tracker.set_children(parent_id, child_ids)
    return child_ids


def _topo_sort(subtasks: list[dict]) -> "Optional[list[int]]":
    """按 depends_on_local_id 拓扑排序。有环返回 None。"""
    n = len(subtasks)
    in_deg = [0] * n
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, st in enumerate(subtasks):
        for dep in st.get("depends_on_local_id", []):
            if 0 <= dep < n and dep != i:  # 自环不算
                adj[dep].append(i)
                in_deg[i] += 1
    # Kahn
    from collections import deque
    q = deque(i for i in range(n) if in_deg[i] == 0)
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] == 0:
                q.append(v)
    if len(order) != n:
        return None  # 有环
    return order
