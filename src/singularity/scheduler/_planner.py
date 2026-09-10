"""内部模块 — Planner 分解 & 多模型委员会。

子任务分解物化 + 多模型 agent 并行规划 + LLM/机械合成。
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Optional

from singularity.scheduler._types import RunContext, BatchOutput, _MAX_DEPTH, _pending_sse_events
from singularity.scheduler._exec import _run_with_retry, decompose
from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import validator as val_mod
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler import witness


def _materialize_in_main(batch: BatchOutput, parent_task) -> None:
    """planner 分解后, 主线程 materialize (worker 不写 tracker)。

    parent 转 DECOMPOSED, materialize_plan 建 children。
    同时推送 token 估算到前端。
    """
    tracker.transition(parent_task.id, TaskStatus.DECOMPOSED)
    # 优先用 worker 线程的分解结果 (避免二次解析导致发散)
    subtasks = batch.planner_subtasks or decompose(batch.dispatch_result.executor_result.raw_output)
    if subtasks:
        try:
            est = estimate_tokens(subtasks, parent_task.description)
            from singularity.scheduler._types import _pending_sse_events
            _pending_sse_events.append({"kind": "token_estimate", "msg": (
                f"[{parent_task.id[:8]}] 方案: {est['task_count']}个子任务, "
                f"预估 ~{est['total_tokens']:,} tokens, "
                f"拆分: " + ", ".join(f"{k}×{v['tokens']:,}" for k,v in est['level_breakdown'].items())
            ), "ts": time.time(), "task_id": parent_task.id, "estimate": est})
        except Exception as _e:
            logging.getLogger(__name__).warning("token estimate failed: %s", _e)
        materialize_plan(parent_task.id, subtasks)



def _maybe_complete_parents(task_id: str) -> None:
    """task 完成后冒泡触发父聚合, 递归到根 (修复 重要 #4: 嵌套分解不冒泡)。"""
    changed = False
    for p in tracker.tasks_dir().glob("*.json"):
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


def materialize_plan(parent_id: str, subtasks: list[dict]) -> list[str]:
    """把子任务 dict 列表创建为真实 Task, 挂到 parent.children。

    - local_id → 真实 task_id 映射
    - 拓扑排序 (按 depends_on_local_id)
    - 环检测 → parent FAILED("循环依赖")
    - depth 上限检查 (>= _MAX_DEPTH 拒绝)
    - tracker.create(parent_id=parent_id) + set_children
    返回子 task_id 列表。
    """
    parent = tracker.read_task(parent_id)
    if parent is None:
        return []

    # depth 安全上限: 已达上限 → 拒绝自动分解，提示用户手工处理
    if parent.depth >= _MAX_DEPTH:
        tracker.transition(
            parent_id, TaskStatus.FAILED,
            error=f"分解深度达安全上限 {_MAX_DEPTH}，请手工拆分或放宽需求",
        )
        _pending_sse_events.append({
            "kind": "alert", "msg": f"[{parent_id[:8]}] 分解达深度上限 {_MAX_DEPTH}，需人工介入",
            "ts": time.time(), "task_id": parent_id,
        })
        return []

    # 拓扑排序 + 环检测
    order = _topo_sort(subtasks)
    if order is None:
        tracker.transition(parent_id, TaskStatus.FAILED, error="循环依赖, 子任务图有环")
        return []

    # local_id → 真实 task_id
    local_to_real: dict[int, str] = {}
    child_ids: list[str] = []
    try:
        for local_id in order:
            st = subtasks[local_id]
            # 依赖的 local_id → 真实 id (容错字符串类型)
            raw_deps = [int(d) if not isinstance(d, int) else d for d in st.get("depends_on_local_id", [])]
            real_deps = [local_to_real[d] for d in raw_deps if d in local_to_real]
            child = tracker.create(
                desc=st["desc"],
                priority=parent.priority,
                depends_on=real_deps,
                parent_id=parent_id,
            )
            tracker.transition(
                child.id, TaskStatus.PENDING,
                route_level=st.get("phase_hint", ""),
                route_locked=True,
            )
            local_to_real[local_id] = child.id
            child_ids.append(child.id)

        tracker.set_children(parent_id, child_ids)
        return child_ids
    except Exception as e:
        # 部分创建失败 → 父任务回滚到 FAILED，已创建的子任务保持 pending（orchestrator 会清理）
        tracker.transition(parent_id, TaskStatus.FAILED,
                          error=f"materialize_plan 部分失败: {e}"[:200])
        return []


def estimate_tokens(subtasks: list[dict], parent_desc: str = "") -> dict:
    """估算子任务的 token 消耗（**不含费用**）。

    返回前端可消费的格式:
      {total_tokens, task_count, per_task: [{desc, level, tokens}],
       level_breakdown: {level: {tokens}}, parent_tokens}
    """
    # 估算参数
    TOKENS_PER_CHAR = 0.6          # 中英混合平均
    OVERHEAD = {"any": 2000}       # 每任务固定开销 (两档后统一 any; 未知 level 走默认 2000)
    RESPONSE_MULTIPLIER = 2.0      # prompt + completion + retry buffer

    # 只估 token，**不估钱**。这里根本不知道子任务会落到哪个模型上，
    # 而各模型单价差几十倍 —— 任何"均价"都是编的（原来是 `total/1e6*0.5`，
    # 注释自认"混合均价 ~$0.5/M"）。真实的钱在 _token_budget 用实际单价算。
    total = 0
    per_task = []
    breakdown = {}
    for st in subtasks:
        desc = st.get("desc", "")
        level = st.get("phase_hint", "")
        chars = len(desc)
        tokens = int(chars * TOKENS_PER_CHAR + OVERHEAD.get(level, 2000))
        tokens = int(tokens * RESPONSE_MULTIPLIER)
        total += tokens
        per_task.append({"desc": desc[:80], "level": level, "tokens": tokens})
        if level not in breakdown:
            breakdown[level] = {"tokens": 0}
        breakdown[level]["tokens"] += tokens

    # 父任务 tokens (调度开销)
    parent_tokens = int(len(parent_desc) * TOKENS_PER_CHAR * RESPONSE_MULTIPLIER) if parent_desc else 0

    return {
        "total_tokens": total,
        "task_count": len(subtasks),
        "per_task": per_task,
        "level_breakdown": {k: {"tokens": v["tokens"]} for k, v in breakdown.items()},
        "parent_tokens": parent_tokens,
    }


def _topo_sort(subtasks: list[dict]) -> "Optional[list[int]]":
    """按 depends_on_local_id 拓扑排序。有环返回 None。"""
    n = len(subtasks)
    in_deg = [0] * n
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, st in enumerate(subtasks):
        for dep in st.get("depends_on_local_id", []):
            try: dep = int(dep)  # 容错: JSON 可能是字符串
            except (ValueError, TypeError): continue
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

