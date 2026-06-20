"""内部模块 — 核心执行引擎。

纯执行: dispatch + validate + trace。worker 线程安全，不写 tracker。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from pathlib import Path

from ._types import RunContext, BatchOutput, _SnapProxy
from ._worktree import (
    _maybe_create_worktree, _cleanup_wt, _lock_wt, _unlock_wt,
    _anchor_ref, _build_merge_request,
)
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
from ._git_worktree import (
    Worktree, create as wt_create, cleanup as wt_cleanup,
    merge_back as wt_merge_back, commit_wt, changed_files_between,
)
from .tracker import TaskStatus


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

def _inject_memory(description: str, pyramid_level: int = 1, token_budget: int = 80) -> str:
    """MAGMA 记忆注入: 金字塔分层展开 (Omni-SimpleMem 原文三级机制)。

    pyramid_level: 1=摘要(~10tokens) 2=完整描述(需sim≥0.3门控) 3=原始文件(需sim≥0.5,受token_budget限制)
    token_budget: Level 2/3 总计最大 token 数 (按 ~1.3 chars/token 估算)
    """
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
        char_used = 0
        budget_chars = token_budget * 1.3  # ~1.3 chars/token
        sim_threshold_l2 = 0.3   # Level 2 门控: 相似度低于此值只给摘要
        sim_threshold_l3 = 0.5   # Level 3 门控: 需更高相似度才展开原始文件

        for item in items[:3]:
            score = item.get("score", 0)
            if score < 0.01:
                continue
            # Level 1: 始终给紧凑摘要 (原文 ~10 tokens/条)
            desc = item.get("description", "")[:30]
            lines.append(f"- {desc}")
            count += 1

            # Level 2: 相似度 ≥0.3 时展开完整描述
            if pyramid_level >= 2 and score >= sim_threshold_l2:
                similarity = item.get("similarity", "")
                tag = f" [相似度 {similarity}]" if similarity else ""
                full_desc = item.get("description", "")[:120]
                files = item.get("files", []) or item.get("attrs", {}).get("files", [])
                if files:
                    full_desc += f" | 涉及: {', '.join(files[:3])}"
                extra = f"  详情: {full_desc}{tag}"
                if char_used + len(extra) < budget_chars:
                    lines.append(extra)
                    char_used += len(extra)

            # Level 3: 相似度 ≥0.5 且预算充裕时展开原始文件
            if pyramid_level >= 3 and score >= sim_threshold_l3:
                files = item.get("files", []) or item.get("attrs", {}).get("files", [])
                for fp in files[:3]:
                    file_line = f"    └─ {fp}"
                    if char_used + len(file_line) < budget_chars:
                        lines.append(file_line)
                        char_used += len(file_line)

        if count == 0:
            return ""
        lines.append("参考以上历史任务的改动方案。\n")
        return "\n".join(lines)
    except Exception as e:
        try: witness.heartbeat("memory", f"warn:inject_memory:{e}")
        except Exception: pass
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
        # 上游 Agent 交接记录 (最近 3 条)
        handoffs = getattr(proj, 'handoffs', []) or []
        if handoffs:
            recent = handoffs[-3:]
            parts.append("[上游交接]")
            for h in recent:
                agent = h.get("agent_model", "unknown")
                phase = h.get("phase", "")
                conclusion = h.get("conclusion", "")[:120]
                deliverable = h.get("deliverable", "")[:120]
                next_agent = h.get("next_agent", "")
                parts.append(
                    f"  [{phase}] {agent}: {conclusion}"
                )
                if deliverable:
                    parts.append(f"    产出: {deliverable}")
                if next_agent:
                    parts.append(f"    建议下一Agent: {next_agent}")
        return "\n".join(parts) if len(parts) > 1 else ""
    except Exception:
        return ""

_CONSTRUCT_WINDOW = 5  # Microsoft ConstructContext: 保留最近 N 对工具调用

def _summarize_events(events: list) -> str:
    """用 cheap-model 生成工具事件摘要 (ConstructContext C4 方案)。

    ponytail: 调 deepseek-chat 生成一行摘要。失败回退到纯计数。
    """
    if not events:
        return ""
    # 构建简短的事件列表
    event_lines = []
    for ev in events[:20]:  # 最多20条，够了
        tool = ev.get("tool", ev.get("name", "?"))
        status = ev.get("status", "?")
        event_lines.append(f"{tool}:{status}")
    brief = ", ".join(event_lines)
    prompt = f"将以下工具调用历史总结为一句话（中文，不超过50字），说明做了什么操作和结果：\n{brief}"

    try:
        import os, json
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return f"更早 {len(events)} 条已省略"
        import httpx
        resp = httpx.Client(timeout=httpx.Timeout(10.0)).post(
            "https://api.deepseek.com/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 80, "temperature": 0.1},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        if resp.status_code == 200:
            body = resp.json()
            summary = body["choices"][0]["message"]["content"].strip()
            return f"[摘要] {summary}（更早 {len(events)} 条已省略）"
    except Exception:
        pass
    return f"更早 {len(events)} 条已省略"


def _construct_context(all_tool_events: list, turn: int) -> str:
    """ConstructContext 算法 (Microsoft 2026): 保留最近 N 条工具事件，更早的 LLM 摘要。

    论文数据: 最近5次+摘要(C4) 完成率 91.6% vs 全量(C2) 71%，token 省 63%。
    """
    if not all_tool_events or turn <= 1:
        return ""
    total = len(all_tool_events)
    if total <= _CONSTRUCT_WINDOW:
        recent = all_tool_events
        omitted = []
        old_count = 0
    else:
        recent = all_tool_events[-_CONSTRUCT_WINDOW:]
        omitted = all_tool_events[:-_CONSTRUCT_WINDOW]
        old_count = len(omitted)
    lines = ["[历史工具调用]"]
    if old_count > 0:
        summary = _summarize_events(omitted)
        lines.append(summary)
    for ev in recent:
        tool = ev.get("tool", ev.get("name", "?"))
        status = ev.get("status", "")
        elapsed = ev.get("elapsed", "")
        detail = f"  {tool}: {status}"
        if elapsed:
            detail += f" ({elapsed:.1f}s)" if isinstance(elapsed, (int, float)) else f" ({elapsed})"
        lines.append(detail[:120])
    lines.append("")
    return "\n".join(lines)


def _build_effective_task(task, turn: int, feedback: str, is_planner: bool,
                          tool_events: list = None) -> str:
    """拼接最终 prompt: 记忆注入 + planner preamble + 项目上下文。

    顺序 (与原 run() 内一致):
      turn==1 且无 feedback → 前置 MAGMA 记忆
      is_planner → 前置 planner preamble
      末尾前置 项目上下文 (proj_ctx + 分隔线)
    """
    effective_task = task.description
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


def _run_fusion(task, level: str, agents: dict, ctx: RunContext = None) -> BatchOutput:
    """Fusion: 2模型并行 → 合成裁判融合。

    两种模式:
      - plan模式 (ctx=None): 不改文件，只出方案 (Self-Fusion)
      - file模式 (ctx提供): 各自worktree内执行+改文件 → merge

    论文: model-fusion Self-Fusion 模式 — 2×便宜 ≈ 1×贵，成本 ~1/3。
    """
    from .execution_judge import run_parallel_models, fuse_outputs
    from . import validator as val_mod
    all_tool_events = []

    # ── file模式: worktree 并行执行 ──
    if ctx is not None:
        return _run_fusion_with_files(task, level, agents, ctx)

    # ── plan模式: 迭代融合 (round1→交叉审查→round2→分类) ──
    try:
        from .execution_judge import classify_finding, build_cross_model_prompt
        outputs = run_parallel_models(task.description, level)
        if len(outputs) < 2:
            return BatchOutput(
                ok=False, task_id=task.id,
                term_reason="fusion_insufficient_models",
                validation=val_mod.ValidationReport(
                    verdict="阻断", action="abort",
                    unverified=[f"并行模型不足: {len(outputs)}/2"],
                ),
                tool_events=all_tool_events, turn_count=0,
            )

        # Round 1: 初始合成
        fused = fuse_outputs(task.description, outputs[0], outputs[1])

        # Round 2: 交叉审查 — 各模型审查对方产出 + 合成结果
        cross_a = build_cross_model_prompt(outputs[1])
        cross_b = build_cross_model_prompt(outputs[0])
        review_a = _call_single_model(task.description + "\n\n" + cross_a, "deepseek-chat")
        review_b = _call_single_model(task.description + "\n\n" + cross_b, "glm-5-turbo")

        if review_a and review_b:
            # 第二轮合成 — 纳入交叉审查结果
            fused = fuse_outputs(
                task.description,
                f"{fused}\n\n[交叉审查A]\n{review_a}",
                f"[交叉审查B]\n{review_b}",
            )

        # 发现分类
        findings = _extract_findings(fused)
        classified = []
        for f_text in findings[:5]:  # 最多5条
            f_class = classify_finding(f_text)
            classified.append(f"{f_class}: {f_text[:80]}")

        # 人工卡点检查
        human_confirm = _needs_human_confirm(task.description, fused)

        unverified = [f"Fusion 2轮迭代: 2模型并行+交叉审查+合成"]
        if classified:
            unverified.append(f"发现分类: {'; '.join(classified[:3])}")
        if human_confirm:
            unverified.append("⚠️ 人工卡点: 建议人工确认后再执行")

        return BatchOutput(
            ok=True, task_id=task.id,
            term_reason="fusion_complete_2round",
            validation=val_mod.ValidationReport(
                verdict="通过", action="pass" if not human_confirm else "abort",
                unverified=unverified,
            ),
            dispatch_result=disp_mod.DispatchResult(
                level=level, agent_cfg={},
                executor_result=type('obj', (object,), {
                    'success': True, 'raw_output': fused,
                    'changed_files': [], 'tool_events': all_tool_events,
                    'elapsed': 0, 'token_count': 0, 'error': '',
                })(),
                attempts=1,
            ),
            tool_events=all_tool_events, turn_count=2,
        )
    except Exception as e:
        return BatchOutput(
            ok=False, task_id=task.id,
            term_reason=f"fusion_error: {e}",
            validation=val_mod.ValidationReport(
                verdict="阻断", action="abort",
                unverified=[f"Fusion 执行异常: {e}"],
            ),
            tool_events=all_tool_events, turn_count=0,
        )


def _call_single_model(task_desc: str, model: str) -> str:
    """调单个 cheap model（用于交叉审查）。ponytail: 直接 HTTP，不经过 dispatcher。"""
    import os
    api_map = {
        "deepseek-chat": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
        "glm-5-turbo": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
    }
    env_var, base_url = api_map.get(model, ("", ""))
    api_key = os.environ.get(env_var, "")
    if not api_key:
        return ""
    try:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(30)) as client:
            r = client.post(
                base_url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": task_desc}],
                      "max_tokens": 800, "temperature": 0.3},
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return ""


def _extract_findings(text: str) -> list[str]:
    """从合成文本中提取独立发现条目。
    ponytail: 按行分割，取以 - / • / 数字 开头的行。
    """
    findings = []
    for line in text.split("\n"):
        line = line.strip()
        if line and (line.startswith("- ") or line.startswith("• ") or
                     (line[0].isdigit() and ". " in line[:4])):
            findings.append(line.lstrip("- •1234567890. "))
    return findings[:10]


def _needs_human_confirm(task_desc: str, fused: str) -> bool:
    """判断是否需要人工卡点: 涉及安全/架构/破坏性变更。
    ponytail: 关键词检测。需要时上 LLM 判断。
    """
    safety_keywords = ["安全", "权限", "认证", "auth", "密钥", "密码", "token",
                       "SQL注入", "XSS", "注入", "shell", "sudo", "删除数据库"]
    arch_keywords = ["架构", "重构", "数据库迁移", "API破坏", "接口变更", "schema"]
    combined = f"{task_desc} {fused[:500]}".lower()
    for kw in safety_keywords:
        if kw.lower() in combined:
            return True
    for kw in arch_keywords:
        if kw.lower() in combined:
            return True
    return False


def _run_fusion_with_files(task, level: str, agents: dict, ctx: RunContext) -> BatchOutput:
    """Fusion file模式: 2模型在独立worktree并行执行+改文件，merge冲突时合成裁判裁决。

    ponytail: 2路并行。需要时扩展到N路。
    """
    from .execution_judge import fuse_outputs
    from . import validator as val_mod
    import concurrent.futures

    # 取前2个可用agent
    level_agents = agents.get(level, agents.get("E", []))
    if len(level_agents) < 2:
        # 不够2个 → fallback到plan模式
        return _run_fusion(task, level, agents, ctx=None)

    agent_a, agent_b = level_agents[0], level_agents[1]
    all_tool_events = []

    def _run_one(agent_cfg):
        """在一个worktree里跑完整 executor 流水线。"""
        single_agents = {level: [agent_cfg]}
        # 使用基类的 run() — 它内部处理 worktree/dispatch/validate
        return _run_with_retry(task, ctx, single_agents)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_run_one, agent_a)
        fut_b = pool.submit(_run_one, agent_b)
        done = concurrent.futures.wait([fut_a, fut_b], timeout=600, return_when='ALL_COMPLETED')

    batch_a = fut_a.result() if fut_a.done() else None
    batch_b = fut_b.result() if fut_b.done() else None

    if not batch_a or not batch_b:
        ok_batch = batch_a or batch_b
        if ok_batch:
            return ok_batch
        return BatchOutput(ok=False, task_id=task.id, term_reason="fusion_both_timeout",
                          validation=val_mod.ValidationReport(verdict="阻断", action="abort",
                              unverified=["Fusion 两路均超时"]))

    # 收集结果
    out_a = batch_a.dispatch_result.executor_result.raw_output if batch_a.dispatch_result else ""
    out_b = batch_b.dispatch_result.executor_result.raw_output if batch_b.dispatch_result else ""
    files_a = batch_a.dispatch_result.executor_result.changed_files if batch_a.dispatch_result else []
    files_b = batch_b.dispatch_result.executor_result.changed_files if batch_b.dispatch_result else []

    if batch_a.tool_events:
        all_tool_events.extend(batch_a.tool_events)
    if batch_b.tool_events:
        all_tool_events.extend(batch_b.tool_events)

    # 检查文件冲突
    conflict_files = set(files_a) & set(files_b)
    all_files = list(set(files_a) | set(files_b))

    if conflict_files:
        # 有冲突 → 合成裁判裁决
        fused = fuse_outputs(task.description, out_a, out_b)
        unverified = [f"Fusion file模式: 2模型({agent_a.get('model','')}+{agent_b.get('model','')})并行",
                      f"冲突文件: {', '.join(conflict_files)}，已合成裁决"]
    else:
        fused = f"{out_a}\n\n---\n[模型B: {agent_b.get('model','')}]\n{out_b}"
        unverified = [f"Fusion file模式: 2模型并行，无文件冲突",
                      f"文件: {', '.join(all_files)}"]

    # 合并 merge requests (如果有)
    merge_req = batch_a.merge_request  # ponytail: 取A的，冲突时合成器已裁决

    return BatchOutput(
        ok=batch_a.ok or batch_b.ok, task_id=task.id,
        term_reason="fusion_file_complete",
        validation=val_mod.ValidationReport(verdict="通过", action="pass", unverified=unverified),
        dispatch_result=disp_mod.DispatchResult(
            level=level, agent_cfg={},
            executor_result=type('obj', (object,), {
                'success': True, 'raw_output': fused,
                'changed_files': all_files, 'tool_events': all_tool_events,
                'elapsed': 0, 'token_count': 0, 'error': '',
            })(),
            attempts=1,
        ),
        merge_request=merge_req,
        tool_events=all_tool_events, turn_count=max(
            getattr(batch_a, 'turn_count', 1), getattr(batch_b, 'turn_count', 1)),
    )


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
    total_failures = getattr(task, 'retry_count', 0)
    force_premium = total_failures >= 2  # 2次失败 → 跳过便宜模型直上 premium
    fallback_chain = disp_mod.pick_agent_fallback_chain(agents, level, fallback_levels=["E+","E"])
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
                                                        tool_events=all_tool_events)

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
                    _, level, turn, disp_result, all_tool_events, reason = pm_signal
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
            "suggested_level": str(item.get("suggested_level", "E")),
            "depends_on_local_id": list(item.get("depends_on_local_id", [])),
        })
    return subtasks


