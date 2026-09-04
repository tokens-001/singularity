__all__ = ['_run_execution', '_run_planning', '_run_research', '_validate_architecture']

import json, os, time, logging

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler.project import ProjectState, Phase, save, _projects_dir
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler._io import try_parse_json
from singularity.scheduler import orchestrator

from singularity.scheduler.workflow import (
    _safe_dispatch, _needs_research, _should_skip, _collect_changed_files,
    _phase_output_path, _save_phase_output, _read_phase_output,
    _ARCHITECT_PREAMBLE, _RESEARCHER_PREAMBLE,
)

def _run_research(project: ProjectState, agents: dict) -> str:
    """调 Researcher(廉价层) 搜集可借鉴方案 → GATE1。"""
    if _should_skip(project, "gate1"):
        project.phase = Phase.GATE1
        save(project)
        return "调研已跳过"

    prompt = _RESEARCHER_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
    )

    # MAGMA 记忆上下文
    try:
        from . import pre_search as pre_mod
        from . import router as router_mod
        route = router_mod.route(project.description)
        pre = pre_mod.pre_search(project.description, route, use_hybrid=True)
        if pre.memory and pre.memory.narrative:
            items = pre.memory.narrative[:5]
            mem_ctx = "已知相关历史任务:\n" + "\n".join(
                f"- [{it.get('task_id','')[-8:]}] {it.get('description','')[:80]}"
                for it in items
            )
            prompt = f"[背景记忆]\n{mem_ctx}\n\n{prompt}"
    except Exception:
        pass

    task_id = f"research_{project.id}"
    disp_result, err = _safe_dispatch(prompt, "any", task_id, agents, project,
                                       project.agent_lineup)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    report = try_parse_json(raw)
    project.research_report = report
    # ponytail: 保存结构化调研报告供后续阶段复用
    _save_phase_output(project.id, "research.md", raw)
    project.add_lineage({"action": "research_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?"})
    project.phase = Phase.GATE1
    save(project)
    return f"调研完成: {len(report.get('competitive_analysis', {}).get('products', []))} 竞品, {len(report.get('frontier_theory', {}).get('papers', []))} 论文引用"


# ═══════════════════════════════════════════════════════════
# GATE2: 架构规划
# ═══════════════════════════════════════════════════════════

def _run_planning(project: ProjectState, agents: dict) -> str:
    """调 Architect(强力层) 出方案+任务清单 → GATE2。"""
    if _should_skip(project, "gate2"):
        project.phase = Phase.GATE2
        save(project)
        return "规划已跳过"

    # 阶段上下文: 优先从磁盘读 research.md
    research_md = _read_phase_output(project.id, "research.md")
    if research_md:
        research_context = research_md[:5000]  # 截断避免 token 浪费
    elif project.research_report:
        research_context = json.dumps(project.research_report, ensure_ascii=False, indent=2)
    else:
        research_context = "无调研报告"

    prompt = _ARCHITECT_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
        research=research_context,
    )

    task_id = f"architect_{project.id}"
    disp_result, err = _safe_dispatch(prompt, "any", task_id, agents, project,
                                       project.agent_lineup)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    arch = try_parse_json(raw, try_repair=True)
    if arch.get("parse_error"):
        retry_prompt = prompt + "\n\n[格式错误] 上一次输出不是合法JSON。请用 ```json ... ``` 包裹输出。"
        disp_result2, err2 = _safe_dispatch(retry_prompt, "any", task_id + "_r", agents,
                                             project, project.agent_lineup)
        raw2 = disp_result2.executor_result.raw_output if disp_result2 else ""
        if err2:
            raw2 += f'\n[LLM错误: {err2}]'
        if raw2:
            arch = try_parse_json(raw2, try_repair=True)
        disp_result = disp_result2  # lineage 用重试结果

    project.architecture = arch
    # ponytail: 保存阶段产出文件供后续阶段复用
    _save_phase_output(project.id, "architecture.md", raw)
    # Step 2: 多模型碰撞 → 保存各模型原始输出
    import json as _json
    fusion_meta_path = config.QIDIAN_DIR / ".last_fusion.json"
    if fusion_meta_path.exists():
        try:
            fm = _json.loads(fusion_meta_path.read_text())
            _save_phase_output(project.id, "fusion-models.md",
                "\n\n---\n".join(f"## 模型: {fm['models'][i]}\n\n{fm['outputs'][i][:3000]}" for i in range(len(fm['models']))))
            _save_phase_output(project.id, "fusion-meta.json",
                _json.dumps({"models": fm["models"], "count": fm["count"]}, ensure_ascii=False))
            fusion_meta_path.unlink()
        except Exception: pass
    traceability = arch.get("traceability", [])
    if traceability:
        _save_phase_output(project.id, "traceability.json",
                          json.dumps(traceability, ensure_ascii=False, indent=2))
    test_plan = arch.get("test_plan", {})
    if test_plan:
        _save_phase_output(project.id, "test-plan.md",
                          json.dumps(test_plan, ensure_ascii=False, indent=2))
    arch_issues = _validate_architecture(arch)
    blockers = [i for i in arch_issues if "缺少" in i or "无效" in i or "应为" in i]
    project.add_lineage({"action": "planning_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?",
                         "task_count": len(arch.get("tasks", [])),
                         "traceability_items": len(traceability),
                         "validation_issues": len(arch_issues),
                         "blockers": len(blockers)})

    # D4 拆解器: unified_architecture → 结构化可执行 task 列表
    try:
        from singularity.scheduler.execution_judge import decompose_architecture
        exec_tasks = decompose_architecture(arch)
        if exec_tasks:
            _save_phase_output(project.id, "executable_tasks.json",
                              json.dumps(exec_tasks, ensure_ascii=False, indent=2))
            project.add_lineage({"action": "tasks_decomposed",
                                "count": len(exec_tasks)})
    except Exception:
        pass

    project.phase = Phase.GATE2
    save(project)
    block_warn = f" (⚠阻塞: {'; '.join(blockers[:2])})" if blockers else ""
    return f"架构完成: {len(arch.get('tasks', []))} 个任务, {len(arch.get('constraints', []))} 条约束, {len(traceability)} 条追溯{block_warn}"


def _validate_architecture(arch: dict) -> list[str]:
    """校验架构产出完整性。"""
    issues = []
    for key in ["architecture", "modules", "data_model", "tech_stack", "tasks", "constraints"]:
        if not arch.get(key):
            issues.append(f"缺少必填字段: {key}")
    # 可选字段 (后续步骤逐步启用)
    for key in ["api_contracts", "risks"]:
        if key not in arch:
            issues.append(f"建议补充字段: {key}")
    tasks = arch.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) == 0:
        issues.append("tasks 为空或格式错误")
    else:
        for i, t in enumerate(tasks):
            tid = t.get("id", f"?")
            for f in ["id", "title", "description", "complexity", "layer", "acceptance"]:
                if not t.get(f):
                    issues.append(f"任务 {tid}: 缺少 {f}")
            if t.get("complexity") not in ("low", "medium", "high"):
                issues.append(f"任务 {tid}: complexity 无效")
            if not isinstance(t.get("estimated_files", []), list):
                issues.append(f"任务 {tid}: estimated_files 应为数组")
    constraints = arch.get("constraints", [])
    if isinstance(constraints, list):
        for i, c in enumerate(constraints):
            if isinstance(c, dict):
                if not c.get("rule"):
                    issues.append(f"约束 {i}: 缺少 rule")
            elif isinstance(c, str):
                issues.append(f"约束 {i}: 应为对象格式")
    modules = arch.get("modules", [])
    if isinstance(modules, list):
        for i, m in enumerate(modules):
            if isinstance(m, dict) and not m.get("name"):
                issues.append(f"模块 {i}: 缺少 name")
    return issues


# ═══════════════════════════════════════════════════════════
# EXECUTING: 拆任务 → tracker → orchestrator
# ═══════════════════════════════════════════════════════════

def _run_execution(project: ProjectState, agents: dict) -> str:
    """分发架构任务到 tracker。不调 LLM, 只创建任务。

    两档后: 实现层统一 route_level="any", 由 dispatcher 从全池选 agent。
    S8: 优先用拆解器(executable_tasks.json)产出, fallback 到 architecture.tasks。
    """
    if not project.architecture:
        return "无架构方案"
    constraints = project.architecture.get("constraints", [])

    # S8: 优先读拆解器产出 (含 context_snippet/acceptance), fallback 到架构原 tasks
    exec_tasks = None
    exec_tasks_path = _projects_dir() / f"{project.id}.executable_tasks.json"
    if exec_tasks_path.exists():
        try:
            exec_tasks = json.loads(exec_tasks_path.read_text(encoding="utf-8"))
        except Exception:
            exec_tasks = None
    if not exec_tasks:
        exec_tasks = project.architecture.get("tasks", [])
    if not exec_tasks:
        return "架构方案无任务清单"

    #  layer → role_key 映射 (两档后不再分 level, 统一 "any")
    LAYER_ROLE_MAP = {
        "frontend": "frontend_engineer",
        "backend": "backend_engineer",
        "data": "data_engineer",
        "devops": "devops_engineer",
    }

    created = 0
    id_map = {}  # 本地任务 id (T1..Tn) → tracker task_id
    for idx, tdef in enumerate(exec_tasks):
        # ── 按 layer 路由到对应角色 ──
        # 拆解器用 suggested_level 存 layer (backend/data/frontend/devops)
        layer = tdef.get("layer", "") or tdef.get("suggested_level", "")
        role_key = LAYER_ROLE_MAP.get(layer, "implementer")

        # 本地任务 id = T{idx+1} (拆解器不产 id 字段，depends_on_local_id 引用此 id)
        tid = tdef.get("id", "") or f"T{idx+1}"

        # 解析依赖 (拆解器用 depends_on_local_id 引用本地 id)
        arch_deps = tdef.get("depends_on", []) or tdef.get("depends_on_local_id", [])
        dep_ids = [id_map[d] for d in arch_deps if d in id_map]

        # 拆解器用 desc 存描述 (拆成 title + description)
        desc = tdef.get("description", "") or tdef.get("desc", "")
        title = tdef.get("title", "")
        if not title and ":" in desc:
            title, desc = desc.split(":", 1)
            title, desc = title.strip(), desc.strip()

        # 注入项目上下文 + 角色信息 + 拆解器上下文片段
        ctx_snippet = tdef.get("context_snippet", "")
        acceptance = tdef.get("acceptance", "") or tdef.get("acceptance_criteria", "")
        task_desc = (
            f"[{tid}] {title}\n"
            f"{desc}\n"
            f"验收标准: {acceptance or '代码可运行，功能完整'}\n"
            + (f"相关上下文:\n{ctx_snippet}\n" if ctx_snippet else "")
            + f"角色: {role_key}\n"
            f"项目背景: {project.description[:200]}\n"
            f"约束: {'; '.join([c.get('rule', c.get('text','')) for c in constraints[:3]]) if constraints else '无'}"
        )
        child = tracker.create(
            task_desc,
            depends_on=dep_ids,
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level="any", route_locked=True,
                           route_role=role_key,  # 绑定角色
                           project_id=project.id)
        project.task_ids.append(child.id)
        id_map[tid] = child.id
        created += 1

    project.fix_round = 0
    project.constraints_checklist = constraints
    project.phase = Phase.EXECUTING
    save(project)
    return f"已分发 {created} 个子任务 (按 layer 路由到对应工程师, 全池选模型)"


# ═══════════════════════════════════════════════════════════
# 内循环: D审查 → 修复 → 再审查 → ... → GATE3
# ═══════════════════════════════════════════════════════════

