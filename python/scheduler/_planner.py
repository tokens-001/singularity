"""内部模块 — Planner 分解 & D层委员会。

子任务分解物化 + D层多 agent 并行规划 + LLM/机械合成。
"""

from __future__ import annotations

import json
import os
import re as _re
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from typing import Optional

from ._types import RunContext, BatchOutput, _MAX_DEPTH
from ._exec import _run_with_retry, decompose
from . import config
from . import tracker
from . import dispatcher as disp_mod
from . import validator as val_mod
from .tracker import TaskStatus


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
            # ── subagent 事件: 启动 ──
            from .orchestrator import _pending_sse_events as _pe
            _pe.append({
                "kind": "subagent", "msg": f"委员会成员启动: {agent_cfg.get('model','?')}",
                "ts": time.time(), "task_id": task.id,
            })
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
                    _pe.append({
                        "kind": "subagent", "msg": f"委员会成员完成: {agent_cfg.get('model','?')}",
                        "ts": time.time(), "task_id": task.id,
                    })
                else:
                    _pe.append({
                        "kind": "subagent", "msg": f"委员会成员失败: {agent_cfg.get('model','?')}",
                        "ts": time.time(), "task_id": task.id,
                    })
            except Exception as e:
                plans.append({"model": agent_cfg.get("model", "?"), "error": str(e)})
                _pe.append({
                    "kind": "subagent", "msg": f"委员会成员异常: {agent_cfg.get('model','?')}",
                    "ts": time.time(), "task_id": task.id,
                })

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

        body = json.dumps({
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
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]

        # 拼接最终输出
        header = f"""# 委员会方案合成 (LLM)
任务: {task_desc[:200]}
参与: {', '.join(models)}

"""
        return header + content

    except Exception as e:
        try:
            import logging
            logging.getLogger("qidian").warning("llm_synthesize: %s", e)
        except Exception as e:
            witness.heartbeat('_planner', f'warn:{e}')
        return None


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

