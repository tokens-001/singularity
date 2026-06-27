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

# ponytail: AI内审已移除，人审在GATE1/GATE2/GATE3把关


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
    # product_dev / agent_dev / refactor 默认需要调研
    if project.template in ("product_dev", "agent_dev", "refactor"):
        return True
    desc = project.description.lower()
    triggers = ["调研", "参考", "借鉴", "架构", "设计", "方案", "重构"]
    return any(t in desc for t in triggers)


def _reassess_complexity(tdef: dict, arch_level: str) -> str:
    """精准升层: 架构师可能低估复杂度，用关键词二次判断。

    只升不降。E+ 变 D 的条件 (满足任一):
    1. 需要读现有代码+写新代码 (跨文件集成)
    2. 涉及协议/异步/服务端基础设施
    3. 架构师已标 high
    """
    if arch_level == "D":
        return "D"

    title = tdef.get("title", "") + " " + tdef.get("description", "")
    title_lower = title.lower()

    # D 层触发词: 需要理解现有系统 + 写基础设施代码
    d_triggers = [
        # 集成/挂载
        ("集成", "挂载"), ("bridge", "入口"), ("接入", "现有"),
        # 协议/基础设施
        ("websocket", "server"), ("async", "asyncio"), ("服务端", "server"),
        ("event", "loop"), ("事件循环",),
        # 跨文件复杂操作
        ("安全执行", "代理"), ("白名单", "executor"),
        # 架构师已标的 (trust the architect)
    ]

    for trigger_group in d_triggers:
        if all(t in title_lower for t in trigger_group):
            return "D"

    # 单文件实现、纯功能模块 → 保持原层
    return arch_level

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
            # ponytail: AI内审已移除，直接交GATE3等人审
            project.phase = Phase.GATE3
            save(project)
            msgs.append("AI内审已移除 → GATE3 等人工审核")
            continue

        elif phase == Phase.FIXING:
            # ponytail: AI修复已移除，交GATE3等人审
            project.phase = Phase.GATE3
            save(project)
            msgs.append("AI自动修复已移除 → GATE3 等人工审核")
            continue

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
    id_map = {}  # architecture task_id → tracker task_id
    for tdef in tasks:
        level_map = {"low": "E", "medium": "E+", "high": "D"}
        architecture_level = level_map.get(tdef.get("complexity", "low"), "E")

        # ── 精准升层: 检测架构师低估的复杂任务 ──
        level = _reassess_complexity(tdef, architecture_level)

        # 解析真正的依赖关系 (架构中定义的 depends_on)
        arch_deps = tdef.get("depends_on", [])
        dep_ids = [id_map[d] for d in arch_deps if d in id_map]

        # 注入项目上下文，让模型知道要做什么
        task_desc = (
            f"项目背景: {project.description[:300]}\n"
            f"项目范围: {project.scope[:200]}\n"
            f"你的任务: [{tdef.get('id', '?')}] {tdef.get('title', '')}\n"
            f"具体要求: {tdef.get('description', '')}\n"
            f"验收标准: {tdef.get('acceptance', '代码可运行，功能完整')}\n"
            f"约束: {'; '.join([c.get('text','') for c in constraints[:3]]) if constraints else '无'}"
        )
        child = tracker.create(
            task_desc,
            depends_on=dep_ids,
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level=level, route_locked=True,
                           project_id=project.id)
        project.task_ids.append(child.id)
        id_map[tdef.get("id", "")] = child.id
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
    """EXECUTING 任务全部完成后 → 直接交 GATE3 等人审。

    ponytail: AI内审已移除。人工在GATE3审核交付物。
    """
    changed = _collect_changed_files(project)
    project.phase = Phase.GATE3
    project.issues = []
    save(project)
    file_count = len(changed)
    return f"执行完成 ({file_count} 个文件改动) → GATE3 等待人工审核"


# ═══════════════════════════════════════════════════════════
# GATE3 打回: 人工反馈 → 回规划重做
# ═══════════════════════════════════════════════════════════

def handle_gate3_reject(project: ProjectState, agents: dict, feedback: str = "") -> str:
    """GATE3 被人工打回: 记录反馈 → 回到架构规划重做。

    ponytail: 不再调D层自动修复。人工给出反馈后，回到PLANNING重新走流程。
    """
    project.add_lineage({"action": "gate3_rejected", "feedback": feedback[:500]})
    project.phase = Phase.PLANNING
    project.architecture = None
    save(project)
    return f"GATE3 打回 (反馈: {feedback[:100]}) → 回到架构规划"


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
