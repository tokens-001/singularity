"""workflow.py — 项目工作流执行引擎。

3门 + D审查内循环:
  TEMPLATE → RESEARCHING → GATE1(用户审调研) → PLANNING → GATE2(用户审架构)
  → EXECUTING(拆任务→orchestrator执行) → [内循环:D审查→修复] → GATE3(用户最终审核)
  → DONE
"""

from __future__ import annotations
import json

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler.project import ProjectState, Phase, save, _projects_dir
from singularity.scheduler.tracker import TaskStatus

from singularity.scheduler._io import try_parse_json


# ═══════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════

_RESEARCHER_PREAMBLE = """你是项目调研员。基于项目需求，完成六维度调研报告。

项目需求: {description}
项目范围: {scope}
原始约束: {constraints}

必须覆盖以下六个维度，缺一不可：

1. **竞品分析**: 同类产品拆解、核心技术方案对比、可借鉴的技术点、竞品短板和本项目差异化机会
2. **前沿理论**: 项目相关的学术/工业前沿进展、关键技术的成熟度评估、前沿理论在本项目中的可行性论证
3. **用户研究**: 目标用户的痛点、交互需求和使用习惯、竞品无法满足的需求
4. **需求澄清**: 核心功能/次要功能/不做功能的边界明确、用户故事的优先级排序
5. **技术预研(PoC)**: 关键技术的原型验证结论、证明"能做"的证据（不是"理论上能做"）、可运行的 demo 或实验数据
6. **约束识别**: 性能约束(响应时间/QPS)、安全约束(加密/权限/攻击面)、合规约束(隐私/行业规范)、兼容性约束(向后兼容/平台)

输出格式 (JSON，```json 包裹):
{{
  "competitive_analysis": {{"products": [...], "comparison": "...", "differentiation": "..."}},
  "frontier_theory": {{"papers": [...], "maturity": "...", "feasibility": "..."}},
  "user_research": {{"pain_points": [...], "needs": [...], "unmet_needs": "..."}},
  "scope_clarification": {{"core": [...], "secondary": [...], "out_of_scope": [...], "priorities": [...]}},
  "technical_poc": {{"pocs": [{{"name": "...", "result": "...", "evidence": "..."}}], "conclusion": "..."}},
  "constraints": {{"performance": [...], "security": [...], "compliance": [...], "compatibility": [...]}},
  "recommendation": "综合推荐方案及理由",
  "pitfalls": ["注意的坑和风险"]
}}

你只做调研，不写代码。输出必须完整覆盖六个维度。"""

_ARCHITECT_PREAMBLE = """你是系统架构师。基于需求和调研报告，设计方案。

项目需求: {description}
项目范围: {scope}
原始约束: {constraints}
调研报告: {research}

你需要产出四个部分：架构方案 + 任务分解清单 + 需求追溯表 + 测试方案。不要调用工具，直接输出 JSON。必须符合以下 Schema:

{{
  "architecture": "主设计思路 + 模块划分 + 数据流 (必填, <500字)",
  "tasks": [
    {{
      "id": "T1",
      "title": "任务标题 (必填, <50字)",
      "description": "任务详细描述 (必填, <200字)",
      "complexity": "low|medium|high (必填)",
      "depends_on": ["T0"],
      "acceptance": "验收标准: 完成后怎么验证? (必填, <100字)",
      "estimated_files": ["涉及文件路径"]
    }}
  ],
  "constraints": [
    {{
      "text": "不改 xx 接口",
      "type": "api_surface|test_green|no_new_deps|compat|perf|other",
      "check": "如何验证: grep/diff/test命令"
    }}
  ],
  "traceability": [
    {{
      "requirement": "立项需求描述 (必填)",
      "test_method": "如何验证该需求已实现 (必填)",
      "owner": "E|E+|D (必填)",
      "acceptance_criteria": "通过标准 (必填)",
      "covered_by_tasks": ["T1", "T2"]
    }}
  ],
  "test_plan": {{
    "project_type": "web_app|cli|library|mobile|script",
    "industry_tests": ["单元测试", "集成测试", "E2E"],
    "test_cases": [
      {{"name": "测试用例名", "what": "测什么", "how": "怎么测", "expected": "预期结果"}}
    ],
    "coverage_target": "80%"
  }},
  "risks": ["风险1"]
}}

Schema 规则:
- tasks 至少 1 个, 最多 20 个
- complexity: low→E层, medium→E+层, high→D层
- depends_on 填其他任务的 id (T1,T2...), 可为空数组
- 每个任务必须改不相交的文件 (并行 merge 的前提)
- constraints 每条必须可机器检查 (type+check 字段)
- **traceability 必须逐条映射立项需求**，每条需求对应一个测试方法和通过标准
- test_plan.project_type 决定行业测试标准
- 你只出方案和清单，不写代码。

输出时用 ```json ... ``` 包裹。"""

_REVIEWER_PREAMBLE = """你是代码审查员。只审查本次改动的代码质量，不审架构方向（架构已由 Owner 确认）。

本次改动的任务: {task_ids}
改动范围: {changed_files}

只审查上述 changed_files 中的文件。重点找 bug 和测试问题。不要调用工具，直接输出 JSON:
{{
  "issues": [
    {{
      "id": "I1",
      "file": "文件路径",
      "severity": "bug|test_gap|style",
      "title": "问题标题",
      "description": "详细描述",
      "suggestion": "修复建议"
    }}
  ],
  "summary": "一句话总结",
  "tests_pass": true
}}

扫描维度:
1. bug: 逻辑错误、空指针、类型不匹配、边界条件
2. test_gap: 验收标准未覆盖、缺少边界测试、mock 不合理
3. style: 死代码、命名混乱、注释缺失

如果 tests_pass 未知则填 null。输出必须严格 JSON。"""

_FIXER_PREAMBLE = """你是系统架构师(D层)。Owner 审核交付物后发现不足，这是完整的错误报告，请出修复方案。

═══════════════════════════════════════
【项目需求】
{description}

【架构方案】
{architecture}

【执行结果】
{execution_report}

【Owner 反馈】
{feedback}

【D层审查 issue 清单】
{current_issues}
═══════════════════════════════════════

请基于以上完整信息输出修复方案（JSON）:
{{
  "diagnosis": "根因分析: 结合执行结果+审查+反馈, 定位根因 (<200字)",
  "fix_tasks": [
    {{
      "id": "F1",
      "title": "修复任务标题 (<50字)",
      "description": "详细描述 (<200字)",
      "complexity": "low|medium|high",
      "acceptance": "验收标准: 必须可机器检查 (<100字)",
      "estimated_files": ["涉及文件"]
    }}
  ],
  "constraints_update": ["新增或修改的约束, 保持与架构方案一致的 type+check 格式"]
}}

规则:
- fix_tasks 最多 5 个
- 优先最小改动，不要推翻重来
- 每个任务验收标准必须可机器检查
- 如果执行结果中某个任务已 FAILED, 优先修复它
输出时用 ```json ... ``` 包裹。"""

# ponytail: 返工不设硬上限，用户判定。收敛检测(降不降) + 用户 GATE3 审批控制终点。


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════

def _safe_dispatch(prompt: str, level: str, task_id: str, agents: dict,
                   project: ProjectState, project_lineup=None) -> tuple:
    """调 disp_mod.dispatch 并记录错误到 project lineage。返回 (disp_result_or_None, error_str)。"""
    try:
        disp_result = disp_mod.dispatch(
            prompt, level, task_id, agents,
            project_lineup=project_lineup,
        )
        return disp_result, ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        project.add_lineage({"action": "llm_error", "level": level, "task_id": task_id, "error": err})
        return None, err

def _needs_research(project: ProjectState) -> bool:
    if project.template == "bug_fix":
        return False
    desc = project.description.lower()
    triggers = ["调研", "参考", "借鉴", "调研", "架构", "设计", "方案", "重构"]
    return any(t in desc for t in triggers)


def _should_skip(project, key: str) -> bool:
    return project.owner_confirm.get(key) == "skip"


def _collect_changed_files(project: ProjectState) -> set[str]:
    """从 task traces 收集改动文件。"""
    changed = set()
    for tid in project.task_ids:
        trace_path = config.TRACE_DIR / f"{tid}.json"
        if trace_path.exists():
            try:
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
                for f in trace.get("changed_files", []):
                    changed.add(f)
            except Exception:
                pass
    return changed


# ═══════════════════════════════════════════════════════════
# Phase 执行
# ═══════════════════════════════════════════════════════════

def run_phase(project: ProjectState, agents: dict) -> str:
    """执行当前 phase。auto_mode 循环推进直到等待 Owner 或完成。"""
    msgs = []
    while True:
        phase = project.phase

        if phase == Phase.TEMPLATE:
            msgs.append("等待 Owner 填写需求并确认")
            break

        elif phase == Phase.RESEARCHING:
            msgs.append(_run_research(project, agents))
            continue

        elif phase == Phase.PLANNING:
            msgs.append(_run_planning(project, agents))
            continue

        elif phase == Phase.EXECUTING:
            msgs.append(_run_execution(project, agents))
            break  # 任务分发后等 orchestrator 跑完

        elif phase in (Phase.GATE1, Phase.GATE2, Phase.GATE3):
            if project.auto_mode:
                project.confirm_gate(phase, "approved")
                save(project)
                msgs.append(f"auto: {phase.value} → {project.phase.value}")
                continue
            msgs.append(f"等待 Owner {phase.value} 确认")
            break

        elif phase == Phase.REVIEWING:
            # 内部: D审查 → 结果驱动内循环
            msgs.append(_run_internal_review(project, agents))
            # _run_internal_review 会把 phase 改为 FIXING/GATE3
            continue

        elif phase == Phase.FIXING:
            # 内部: 创建修复任务 → 等 orchestrator 执行
            msgs.append(_run_internal_fixing(project, agents))
            break  # 等 orchestrator 跑完修复任务 → app.py loop 推进

        elif phase == Phase.DONE:
            msgs.append("项目已完成")
            break

        else:
            msgs.append(f"未知 phase: {phase.value}")
            break
    return "; ".join(msgs)


# ═══════════════════════════════════════════════════════════
# 阶段上下文传递: 读写阶段产出文件
# ═══════════════════════════════════════════════════════════

def _phase_output_path(project_id: str, filename: str) -> Path:
    """项目阶段产出文件路径。存于 .qidian/projects/ 目录。"""
    return _projects_dir() / f"{project_id}.{filename}"


def _save_phase_output(project_id: str, filename: str, content: str) -> Path:
    """保存阶段产出到文件。"""
    p = _phase_output_path(project_id, filename)
    p.write_text(content, encoding="utf-8")
    return p


def _read_phase_output(project_id: str, filename: str) -> str | None:
    """读取阶段产出文件。不存在返回 None。"""
    p = _phase_output_path(project_id, filename)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


# ═══════════════════════════════════════════════════════════
# GATE1: 调研
# ═══════════════════════════════════════════════════════════

def _run_research(project: ProjectState, agents: dict) -> str:
    """调 Researcher(E层) 搜集可借鉴方案 → GATE1。"""
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
    disp_result, err = _safe_dispatch(prompt, "E", task_id, agents, project,
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
    """调 Architect(D层) 出方案+任务清单 → GATE2。"""
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
    disp_result, err = _safe_dispatch(prompt, "D", task_id, agents, project,
                                       project.agent_lineup)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    arch = try_parse_json(raw, try_repair=True)
    if arch.get("parse_error"):
        retry_prompt = prompt + "\n\n[格式错误] 上一次输出不是合法JSON。请用 ```json ... ``` 包裹输出。"
        disp_result2, err2 = _safe_dispatch(retry_prompt, "D", task_id + "_r", agents,
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

    project.phase = Phase.GATE2
    save(project)
    block_warn = f" (⚠阻塞: {'; '.join(blockers[:2])})" if blockers else ""
    return f"架构完成: {len(arch.get('tasks', []))} 个任务, {len(arch.get('constraints', []))} 条约束, {len(traceability)} 条追溯{block_warn}"


def _validate_architecture(arch: dict) -> list[str]:
    """校验架构产出完整性。"""
    issues = []
    for key in ["architecture", "tasks", "constraints", "traceability", "test_plan"]:
        if not arch.get(key):
            issues.append(f"缺少必填字段: {key}")
    tasks = arch.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) == 0:
        issues.append("tasks 为空或格式错误")
    else:
        for i, t in enumerate(tasks):
            tid = t.get("id", f"?")
            for f in ["id", "title", "description", "complexity", "acceptance"]:
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
                if not c.get("text"):
                    issues.append(f"约束 {i}: 缺少 text")
                if c.get("type") not in ("api_surface", "test_green", "no_new_deps", "compat", "perf", "other"):
                    issues.append(f"约束 {i}: type 无效")
            elif isinstance(c, str):
                issues.append(f"约束 {i}: 应为对象格式")
    if "risks" not in arch:
        issues.append("缺少 risks 字段")
    return issues


# ═══════════════════════════════════════════════════════════
# EXECUTING: 拆任务 → tracker → orchestrator
# ═══════════════════════════════════════════════════════════

def _run_execution(project: ProjectState, agents: dict) -> str:
    """分发架构任务到 tracker。不调 LLM, 只创建任务。"""
    if not project.architecture:
        return "无架构方案"
    tasks = project.architecture.get("tasks", [])
    constraints = project.architecture.get("constraints", [])
    if not tasks:
        return "架构方案无任务清单"

    created = 0
    parent_id = ""
    for tdef in tasks:
        level_map = {"low": "E", "medium": "E+", "high": "D"}
        level = level_map.get(tdef.get("complexity", "low"), "E")

        child = tracker.create(
            f"[{tdef.get('id', '?')}] {tdef.get('title', '')}: {tdef.get('description', '')}",
            depends_on=[parent_id] if parent_id else [],
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level=level, route_locked=True,
                           project_id=project.id)
        project.task_ids.append(child.id)
        if not parent_id:
            parent_id = child.id
        created += 1

    project.fix_round = 0
    project.constraints_checklist = constraints
    project.phase = Phase.EXECUTING
    save(project)
    return f"已分发 {created} 个子任务"


# ═══════════════════════════════════════════════════════════
# 内循环: D审查 → 修复 → 再审查 → ... → GATE3
# ═══════════════════════════════════════════════════════════

def run_test_fix_loop(project: ProjectState, agents: dict) -> str:
    """EXECUTING 任务全部完成后调用。内循环推进直到 GATE3。"""
    msgs = []

    # 收集改动文件
    changed = _collect_changed_files(project)
    if not changed:
        project.phase = Phase.GATE3
        project.issues = []
        save(project)
        return "无改动文件 → GATE3"

    msgs.append(f"内循环开始: {len(changed)} 个文件")
    prev_bug_count = None

    while True:
        # D层审查
        issues = _do_review(project, agents, changed)
        bugs = [i for i in issues if i.get("severity") == "bug"]
        project.issues = issues
        project.add_lineage({"action": "review_round", "round": project.fix_round + 1,
                             "issues": len(issues), "bugs": len(bugs)})

        if not bugs:
            # 需求符合性检查 (production-flow.md: 测试第一层)
            from .supervisor import check_requirement_conformance
            req_check = check_requirement_conformance(
                project.id,
                agent_output="",  # 项目级检查，用磁盘文件
                changed_files=list(changed),
            )
            if not req_check.passed:
                project.add_lineage({"action": "requirement_check_failed",
                                     "evidence": req_check.evidence})
                project.phase = Phase.GATE3  # 仍有未达标 → 交用户裁决
                save(project)
                msgs.append(f"审查通过 (0 bug), 但需求符合性: {req_check.reason} → GATE3 (用户裁决)")
                return "; ".join(msgs)

            project.phase = Phase.GATE3
            save(project)
            msgs.append(f"审查通过 (0 bug, {len(issues)} 个建议) → GATE3")
            return "; ".join(msgs)

        # ── 收敛检测: bug 数不降反升 → 升 D 重出方案 ──
        if prev_bug_count is not None and len(bugs) >= prev_bug_count:
            project.phase = Phase.PLANNING  # 打回重规划
            project.architecture = None
            save(project)
            msgs.append(f"收敛失败: bug数 {prev_bug_count}→{len(bugs)} 未收敛 → 升D重出方案")
            return "; ".join(msgs)
        prev_bug_count = len(bugs)

        # 有 bug → 创建修复任务，继续循环
        project.fix_round += 1
        _create_fix_tasks(project, bugs)
        msgs.append(f"第{project.fix_round}轮: {len(bugs)} 个 bug → {len(bugs)} 个修复任务 → 等待执行")
        break  # 等 orchestrator 跑完修复任务, app.py loop 会再调

    return "; ".join(msgs)


def _do_review(project: ProjectState, agents: dict, changed_files: set[str]) -> list[dict]:
    """调 D层审查代码。返回 issues 列表。"""
    changed_str = ", ".join(sorted(changed_files))
    task_ids_str = ", ".join(project.task_ids[-10:]) if project.task_ids else "无"

    prompt = _REVIEWER_PREAMBLE.format(
        task_ids=task_ids_str,
        changed_files=changed_str,
    )

    task_id = f"review_{project.id}_r{project.fix_round}"
    disp_result, err = _safe_dispatch(prompt, "D", task_id, agents, project,
                                       project.agent_lineup)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    review = try_parse_json(raw)
    return review.get("issues", [])


def _create_fix_tasks(project: ProjectState, bugs: list[dict]):
    """为 bug 级 issue 创建修复任务。"""
    for issue in bugs:
        child = tracker.create(
            f"[修复] {issue.get('id', '?')} {issue.get('title', '')}: {issue.get('description', '')}",
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level="E+", route_locked=True,
                           project_id=project.id)
        project.task_ids.append(child.id)
    project.phase = Phase.FIXING
    save(project)


# ═══════════════════════════════════════════════════════════
# 内部: REVIEWING / FIXING (非用户门, 内循环用)
# ═══════════════════════════════════════════════════════════

def _run_internal_review(project: ProjectState, agents: dict) -> str:
    """内循环审查入口: 收集文件 → D审查 → 决定下一步。"""
    changed = _collect_changed_files(project)
    if not changed:
        project.phase = Phase.GATE3
        save(project)
        return "无改动 → GATE3"

    issues = _do_review(project, agents, changed)
    bugs = [i for i in issues if i.get("severity") == "bug"]
    project.issues = issues
    project.fix_round += 1

    if not bugs:
        # 需求符合性检查
        from .supervisor import check_requirement_conformance
        changed = _collect_changed_files(project)
        req_check = check_requirement_conformance(
            project.id, agent_output="", changed_files=list(changed),
        )
        project.phase = Phase.GATE3
        save(project)
        req_msg = f"; 需求符合性: {req_check.reason}" if not req_check.passed else ""
        return f"审查通过 (0 bug, {len(issues)} 个建议) → GATE3{req_msg}"

    _create_fix_tasks(project, bugs)
    return f"第{project.fix_round}轮审查: {len(bugs)} bug → 修复任务已创建"


def _run_internal_fixing(project: ProjectState, agents: dict) -> str:
    """内部修复: 任务已创建, 等 orchestrator 执行完 → app.py loop 推进到 REVIEWING。"""
    project.phase = Phase.REVIEWING  # 标记: 修复任务完成后回到审查
    save(project)
    return f"等待修复任务执行 → 回到审查"


# ═══════════════════════════════════════════════════════════
# GATE3 打回: D出方案 → 修复 → 测试 → 回 GATE3
# ═══════════════════════════════════════════════════════════

def _build_execution_report(project: ProjectState) -> str:
    """收集所有任务执行状态 + 改动文件 → 结构化报告。"""
    lines = []
    for tid in project.task_ids:
        t = tracker.read_task(tid)
        if t is None:
            continue
        status_icon = {"done": "✅", "failed": "❌", "rolled_back": "↩️", "running": "⏳",
                       "pending": "⏸️"}.get(t.status.value if hasattr(t.status, 'value') else str(t.status), "❓")
        lines.append(f"  {status_icon} [{tid[-8:]}] {t.description[:80]}")
        if t.status == TaskStatus.FAILED and hasattr(t, 'error') and t.error:
            lines.append(f"     错误: {str(t.error)[:200]}")
    # 改动文件
    changed = _collect_changed_files(project)
    if changed:
        lines.append(f"\n改动文件 ({len(changed)}):")
        for f in sorted(changed):
            lines.append(f"  - {f}")
    # 测试策略
    if project.architecture and project.architecture.get("test_strategy"):
        lines.append(f"\n测试策略: {project.architecture['test_strategy'][:200]}")
    return "\n".join(lines) if lines else "无执行记录"


def handle_gate3_reject(project: ProjectState, agents: dict, feedback: str = "") -> str:
    """GATE3 被用户打回: D出修复方案 → 创建任务 → 等执行 → 内循环 → 回 GATE3。"""
    architecture_json = json.dumps(project.architecture, ensure_ascii=False, indent=2) \
        if project.architecture else "无架构方案"
    issues_json = json.dumps(project.issues, ensure_ascii=False, indent=2) \
        if project.issues else "无"
    exec_report = _build_execution_report(project)

    prompt = _FIXER_PREAMBLE.format(
        description=project.description,
        architecture=architecture_json,
        execution_report=exec_report,
        feedback=feedback or "请审查交付物并修复问题",
        current_issues=issues_json,
    )

    task_id = f"fixer_{project.id}_g3"
    disp_result, err = _safe_dispatch(prompt, "D", task_id, agents, project,
                                       project.agent_lineup)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    fix_plan = try_parse_json(raw, try_repair=True)
    fix_tasks = fix_plan.get("fix_tasks", [])

    project.add_lineage({"action": "gate3_rejected", "diagnosis": fix_plan.get("diagnosis", ""),
                         "fix_tasks": len(fix_tasks)})

    if not fix_tasks:
        # D 没出任务: 直接回到架构规划
        project.phase = Phase.PLANNING
        save(project)
        return "D层未产出修复任务 → 回到架构规划"

    # 创建修复任务
    project.fix_round = 0
    for ft in fix_tasks:
        level_map = {"low": "E", "medium": "E+", "high": "D"}
        level = level_map.get(ft.get("complexity", "medium"), "E+")
        child = tracker.create(
            f"[G3修复] {ft.get('id', '?')} {ft.get('title', '')}: {ft.get('description', '')}",
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level=level, route_locked=True,
                           project_id=project.id)
        project.task_ids.append(child.id)

    project.phase = Phase.REVIEWING  # 修复任务完成后 → 内循环审查 → GATE3
    save(project)
    return f"GATE3 打回: D出{len(fix_tasks)}个修复任务 → 等执行 → 内循环 → 回GATE3"


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

def start_project_workflow(project: ProjectState, agents: dict) -> str:
    """项目工作流入口。"""
    if project.phase != Phase.TEMPLATE:
        return run_phase(project, agents)

    if not project.description:
        return "请先填写需求描述再启动工作流"

    if _needs_research(project):
        project.phase = Phase.RESEARCHING
    else:
        project.phase = Phase.PLANNING
    save(project)

    return run_phase(project, agents)
