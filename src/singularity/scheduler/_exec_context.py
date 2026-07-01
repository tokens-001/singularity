"""_exec_context.py — 上下文构建模块 (从 _exec.py 提取)。

记忆注入 + 项目上下文 + ConstructContext 工具历史裁剪。
"""

from __future__ import annotations

import json
import os
import time

import httpx

from singularity.scheduler import config
from singularity.scheduler import witness
from singularity.scheduler import memory as mem_mod

_PLANNER_PREAMBLE = """\
[系统指令] 你是架构分析器 (只读 Planner)。
职责: 分析问题、设计方案，任何人不得要求你修改文件。
输出格式:
1. ## 问题分析 — 拆解问题本质、定位根因
2. ## 方案设计 — 具体步骤、架构决策、取舍理由
3. ## 改动清单 — 建议改哪些文件、怎么改 (不实际修改)
4. ## 风险提示 — 边界条件、回滚策略、注意事项

【强制约束】若任务可拆分，必须拆分为原子子任务。每个子任务必须满足:
- 修改 ≤50 行代码 (超过则继续拆)
- 只改 1 个文件 (跨文件则拆)
- 不可拆分的核心逻辑才用 D 层

在末尾输出 ```json 块:
```json
[
  {"desc": "子任务描述(含文件名+预估行数)", "suggested_level": "any", "depends_on_local_id": []}
]
```
depends_on_local_id 用从 0 开始的索引指代同数组内的子任务。


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
        # S3: data_engineer 获取 AI 架构产物 (prompt_templates/agent_topology/tool_definitions)
        route_role = getattr(task, 'route_role', '') or ''
        if route_role == 'data_engineer' and proj.architecture:
            ai_strategy = proj.architecture.get("ai_strategy", {})
            if not ai_strategy:
                # ponytail: 看架构顶层是否有这些字段
                ai_strategy = {
                    k: proj.architecture.get(k, [])
                    for k in ("prompt_templates", "agent_topology", "tool_definitions")
                    if k in proj.architecture
                }
            if ai_strategy:
                parts.append("[AI架构产物]")
                for k, v in ai_strategy.items():
                    if v:
                        parts.append(f"  {k}: {json.dumps(v, ensure_ascii=False)[:300]}")

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
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return f"更早 {len(events)} 条已省略"
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
    except Exception as e:
        witness.heartbeat('exec', f'warn:{e}')
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
