"""内部模块 — 核心执行引擎。

纯执行: dispatch + validate + trace。worker 线程安全，不写 tracker。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from pathlib import Path

from singularity.scheduler._types import RunContext, BatchOutput, _SnapProxy
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


def _build_effective_task(task, turn: int, feedback: str, is_planner: bool,
                          tool_events: list = None, route_role: str = "") -> str:
    """拼接最终 prompt: 记忆注入 + 角色上下文 + planner preamble + 项目上下文。

    Step 4: route_role 非空时注入角色 system_prompt。
    """
    effective_task = task.description
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
    """Step 4: 从 roles.toml 加载角色 system_prompt 作为执行上下文。"""
    try:
        from singularity.scheduler.roles import get_role
        role = get_role(route_role)
        if role and role.system_prompt:
            return role.system_prompt
    except Exception:
        pass
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


def _process_planner_or_merge(task, ctx, turn, level, is_planner, wt,
                              exec_result, disp_result, all_tool_events,
                              pending_merge_req_holder: list):
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
                _anchor_ref(task.id, branch_ref)  # 防 gc 回收 (重要 #3)
                pending_merge_req_holder[0] = _build_merge_request(
                    task, branch_ref, ctx.snapshot_ref,
                )
        else:
            # v2: 直接 merge_back
            mr = wt_merge_back(wt)
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

    snap = _SnapProxy(ctx.snapshot_ref)

    # ── 执行前钩子 ──
    pre_warnings = val_mod.pre_execution_hook(task.description, snap)
    if pre_warnings:
        for w in pre_warnings:
            witness.heartbeat(task.id, f"pre_hook: {w[:80]}")

    # 容灾: 获取 fallback 链, 当前 agent 失败自动切下一个
    # 如果任务已重试多次，强制优先用 premium 模型
    force_premium = getattr(ctx, 'retry_count', 0) >= 2
    fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level, fallback_levels=["any"])
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
        cwd = str(wt.path) if wt else str(config.PROJECT_ROOT)  # ponytail: 无worktree时直接用项目根

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

                effective_task = _build_effective_task(task, turn, feedback, is_planner,
                                                        tool_events=all_tool_events,
                                                        route_role=route_role)

                disp_result = disp_mod.dispatch(
                    effective_task, level, task.id, agents,
                    feedback=feedback, baseline_ref=ctx.snapshot_ref, cwd=cwd,
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
                        witness.heartbeat(task.id, f"fallback: {agent_cfg.get('model','')}→{fallback_chain[0].get('model','')}")
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
                    pending_merge_req_holder,
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
                    changed_files=exec_result.changed_files,
                    snap=snap, turn=turn, max_turns=level_max,
                )
                # 补充质量信号
                try:
                    quality = val_mod.post_execution_hook(exec_result, snap)
                    validation.confidence = quality.get("confidence", 0.5)
                    validation.quality_signals = quality.get("quality_signals", {})
                except Exception:
                    quality = {"warnings": [], "failure_kind": "ok", "confidence": 0.5}

                # ── v2: independent tests + multi-model review ──
                changed = exec_result.changed_files or []
                if changed:
                    from . import _review as rev_mod
                    rev_mod.run_post_exec_checks(
                        validation=validation, quality=quality,
                        exec_result=exec_result, task=task,
                        agent_cfg=agent_cfg, level=level, cwd=cwd,
                        changed=changed)

                last_validation = validation

                cascade_action, payload = _decide_cascade(
                    task, level, turn, validation, disp_result, all_tool_events,
                    pending_merge_req, fallback_chain, tried_models, quality,
                )
                if cascade_action == "return":
                    return payload
                if cascade_action == "break":
                    # 低置信 cascade_skip: 标记 tried 后 break 升级
                    tried_models.add(agent_cfg.get("model", ""))
                    fallback_chain = [a for a in fallback_chain if a.get("model", "") not in tried_models]
                    if fallback_chain:
                        witness.heartbeat(task.id, f"cascade_skip:{agent_cfg.get('model','')}→{fallback_chain[0].get('model','')} conf={validation.confidence:.2f}")
                    break  # 跳出 turn loop，用更好的模型 (finally 清理本 wt)
                # cascade_action == "continue": 中置信 retry
                feedback = payload
                continue

            next_level = disp_mod.escalate(level)
            if next_level is None:
                term_reason = f"escalation_exhausted (level={level})"
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
                if post_warnings:
                    batch.term_reason += f"; post_hook: {', '.join(post_warnings)}"
        except Exception as e:
            try: witness.heartbeat(task_id=task.id, status="error", detail=f"post_hook:{e}")
            except Exception: pass

        if batch.validation.action == "pass" or batch.planner_decomposed:
            return batch
        if batch.term_reason.startswith("merge_conflict"):
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
            except Exception as e:
                witness.heartbeat('exec', f'warn:{e}')
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
        witness.heartbeat('exec', f'warn:{e}')

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
    except Exception as e:
        witness.heartbeat('exec', f'warn:{e}')


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


