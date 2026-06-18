"""workflow.py — 项目工作流执行引擎。

把 ProjectState 和 orchestrator 串联:
  - 读 project phase → 决定下一步动作
  - 调对应 agent (Researcher/Architect/Implementer/Supervisor)
  - 写回 project 状态
"""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from . import config
from . import tracker
from . import dispatcher as disp_mod
from . import orchestrator
from .project import ProjectState, Phase, save, load
from .tracker import TaskStatus


# ═══════════════════════════════════════════════════════════
# Phase → Action 映射
# ═══════════════════════════════════════════════════════════

_RESEARCHER_PREAMBLE = """你是项目调研员。基于项目需求，搜集可借鉴的架构/方案/理论。

项目需求: {description}
项目范围: {scope}
原始约束: {constraints}

请搜索并输出结构化调研报告（JSON）:
{{
  "references": [
    {{"name": "...", "source": "...", "core_idea": "...", "pros": [...], "cons": [...], "applicability": "high|medium|low"}}
  ],
  "comparison": "各方案对比",
  "recommendation": "推荐方案及理由",
  "pitfalls": ["注意的坑"]
}}

注意: 你只做调研，不写代码。输出必须严格 JSON。"""

_ARCHITECT_PREAMBLE = """你是系统架构师。基于需求和调研报告，设计方案。

项目需求: {description}
项目范围: {scope}
原始约束: {constraints}
调研报告: {research}

你需要产出架构方案 + 任务分解清单 + 约束。输出严格 JSON，必须符合以下 Schema:

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
  "risks": ["风险1"],
  "test_strategy": "如何测试整个方案 (必填, <200字)"
}}

Schema 规则:
- tasks 至少 1 个, 最多 20 个
- complexity: low→E层, medium→E+层, high→D层
- depends_on 填其他任务的 id (T1,T2...), 可为空数组
- 每个任务必须改不相交的文件 (并行 merge 的前提)
- constraints 每条必须可机器检查 (type+check 字段)
- 你只出方案和清单，不写代码。

输出时用 ```json ... ``` 包裹。"""

_REVIEWER_PREAMBLE = """你是系统审查员。只审查本次改动的文件和任务，不扫全项目。

架构方案: {architecture}
本次改动的任务: {task_ids}
改动范围: {changed_files}

只审查上述 changed_files 中的文件，不要扫描未改动的模块。输出严格 JSON:
{{
  "issues": [
    {{
      "id": "I1",
      "file": "文件路径",
      "line": null,
      "severity": "bug|perf|style|arch",
      "title": "问题标题",
      "description": "详细描述",
      "suggestion": "修复建议"
    }}
  ]
}}

扫描维度:
1. bug: 逻辑错误、空指针、类型不匹配、边界条件
2. 架构一致性: 是否偏离架构方案、模块职责是否清晰
3. 性能: N+1 查询、不必要的拷贝、内存泄漏风险
4. 代码风格: 命名规范、注释缺失、重复代码
5. 测试覆盖: 关键路径是否有测试、断言是否充分

注意: 如果没有发现问题，返回空 issues 数组。输出必须严格 JSON。"""


def _needs_research(project: ProjectState) -> bool:
    """判断是否需要调研阶段。bug_fix 和简单任务自动跳过。"""
    if project.template == "bug_fix":
        return False
    desc = project.description.lower()
    triggers = ["调研", "参考", "借鉴", "调研", "架构", "设计", "方案", "重构"]
    return any(t in desc for t in triggers)


def _should_skip(project, key: str) -> bool:
    return project.owner_confirm.get(key) == "skip"


def run_phase(project: ProjectState, agents: dict) -> str:
    """执行 project 当前 phase 对应的动作。返回日志信息。"""
    phase = project.phase

    if phase == Phase.TEMPLATE:
        return "等待 Owner 填写需求并确认"

    elif phase == Phase.RESEARCHING:
        return _run_research(project, agents)

    elif phase in (Phase.GATE1, Phase.GATE2, Phase.GATE3, Phase.GATE4):
        if project.auto_mode:
            project.confirm_gate(phase, "approved")
            save(project)
            return f"auto: {phase.value} → {project.phase.value}"
        return f"等待 Owner {phase.value} 确认"

    elif phase == Phase.PLANNING:
        return _run_planning(project, agents)

    elif phase == Phase.EXECUTING:
        return _run_execution(project, agents)

    elif phase == Phase.REVIEWING:
        return _run_review(project, agents)

    elif phase == Phase.FIXING:
        return _run_fixing(project, agents)

    elif phase == Phase.DONE:
        return "项目已完成"

    return f"未知 phase: {phase.value}"


def _run_research(project: ProjectState, agents: dict) -> str:
    """调 Researcher(E层) 同步搜集可借鉴方案 → 解析 JSON → 写入 project。

    与旧版不同: 不创建 tracker task (不丢产出), 直接 dispatch 等结果。
    """
    if _should_skip(project, "gate1"):
        project.phase = Phase.GATE1
        save(project)
        return "调研已跳过 (Owner 设定)"

    # 1. 构建 prompt
    prompt = _RESEARCHER_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
    )

    # 2. MAGMA 记忆上下文 (已有历史任务参考)
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
        pass  # 记忆挂了不阻塞调研

    # 3. 同步 dispatch 到 E 层
    task_id = f"research_{project.id}"
    try:
        disp_result = disp_mod.dispatch(
            prompt, "E", task_id, agents,
            project_lineup=project.agent_lineup,
        )
        raw = disp_result.executor_result.raw_output if disp_result else ""
    except Exception as e:
        raw = ""

    # 4. 解析 JSON 产出
    import re as _re
    report = None
    if raw:
        try:
            m = _re.search(r"```json\s*\n(.*?)\n```", raw, _re.DOTALL)
            if m:
                report = json.loads(m.group(1))
            else:
                m2 = _re.search(r"\{[\s\S]*\}", raw)
                if m2:
                    report = json.loads(m2.group())
        except (json.JSONDecodeError, Exception):
            pass
    if report is None:
        report = {"raw_output": raw[:5000], "parse_error": True}

    project.research_report = report
    project.add_lineage({"action": "research_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?"})

    # 5. 推进到 GATE1
    project.phase = Phase.GATE1
    save(project)
    return (
        f"调研完成: {len(report.get('references', []))} 条引用, "
        f"推荐: {report.get('recommendation', 'N/A')[:80]}"
    )


def _run_planning(project: ProjectState, agents: dict) -> str:
    """调 Architect(D层) 同步出方案+任务清单 → 解析 JSON → 写入 project。"""
    if _should_skip(project, "gate2"):
        project.phase = Phase.GATE2
        save(project)
        return "规划已跳过 (Owner 设定)"

    # 1. 构建 prompt (含调研报告)
    research_json = json.dumps(project.research_report, ensure_ascii=False, indent=2) \
        if project.research_report else "无调研报告"

    prompt = _ARCHITECT_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
        research=research_json,
    )

    # 2. 同步 dispatch 到 D 层
    task_id = f"architect_{project.id}"
    disp_result = None
    raw = ""
    try:
        disp_result = disp_mod.dispatch(
            prompt, "D", task_id, agents,
            project_lineup=project.agent_lineup,
        )
        raw = disp_result.executor_result.raw_output if disp_result else ""
    except Exception as e:
        raw = ""

    # 3. 解析 JSON 产出
    import re as _re
    arch = None
    if raw:
        try:
            m = _re.search(r"```json\s*\n(.*?)\n```", raw, _re.DOTALL)
            if m:
                arch = json.loads(m.group(1))
            else:
                m2 = _re.search(r"\{[\s\S]*\}", raw)
                if m2:
                    arch = json.loads(m2.group())
        except (json.JSONDecodeError, Exception):
            pass
    if arch is None:
        # 重试一次: 附加格式指令
        retry_prompt = prompt + "\n\n[格式错误] 上一次输出不是合法JSON。请用 ```json ... ``` 包裹输出，确保可以被 JSON.parse 解析。"
        raw2 = ""
        try:
            disp_result2 = disp_mod.dispatch(
                retry_prompt, "D", task_id + "_r", agents,
                project_lineup=project.agent_lineup,
            )
            raw2 = disp_result2.executor_result.raw_output if disp_result2 else ""
        except Exception:
            pass
        if raw2:
            import re as _re3
            try:
                m = _re3.search(r"```json\s*\n(.*?)\n```", raw2, _re3.DOTALL)
                if m:
                    arch = json.loads(m.group(1))
                else:
                    m2 = _re3.search(r"\{[\s\S]*\}", raw2)
                    if m2:
                        arch = json.loads(m2.group())
            except (json.JSONDecodeError, Exception):
                pass
    if arch is None:
        arch = {"raw_output": raw[:5000], "parse_error": True}

    project.architecture = arch
    # 校验架构完整性 — 阻塞级错误不许过
    arch_issues = _validate_architecture(arch)
    blockers = [i for i in arch_issues if "缺少" in i or "无效" in i or "应为" in i]
    project.add_lineage({"action": "planning_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?",
                         "task_count": len(arch.get("tasks", [])),
                         "validation_issues": len(arch_issues),
                         "blockers": len(blockers)})

    # 4. 推进到 GATE2 (有阻塞级问题就标记,但不过早中止——让 Owner 在 Gate 看到警告)
    project.phase = Phase.GATE2
    save(project)
    block_warn = f" (⚠阻塞: {'; '.join(blockers[:2])})" if blockers else ""
    warn = f" (校验: {'; '.join(arch_issues[:3])})" if arch_issues and not blockers else block_warn
    return (
        f"架构完成: {len(arch.get('tasks', []))} 个任务, "
        f"{len(arch.get('constraints', []))} 条约束"
        f"{warn}, "
        f"设计: {arch.get('architecture','N/A')[:80]}"
    )


def _validate_architecture(arch: dict) -> list[str]:
    """校验架构产出完整性。返回问题列表, 空=通过。"""
    issues = []
    # 必填顶层字段
    for key in ["architecture", "tasks", "constraints", "test_strategy"]:
        if not arch.get(key):
            issues.append(f"缺少必填字段: {key}")
    # tasks 校验
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
                issues.append(f"任务 {tid}: complexity 无效 ({t.get('complexity')})")
            if not isinstance(t.get("estimated_files", []), list):
                issues.append(f"任务 {tid}: estimated_files 应为数组")
    # constraints 校验
    constraints = arch.get("constraints", [])
    if isinstance(constraints, list):
        for i, c in enumerate(constraints):
            if isinstance(c, dict):
                if not c.get("text"):
                    issues.append(f"约束 {i}: 缺少 text")
                if c.get("type") not in ("api_surface", "test_green", "no_new_deps", "compat", "perf", "other"):
                    issues.append(f"约束 {i}: type 无效 ({c.get('type')})")
            elif isinstance(c, str):
                issues.append(f"约束 {i}: 应为对象格式 {{text,type,check}}, 不是纯字符串")
    # risks
    if "risks" not in arch:
        issues.append("缺少 risks 字段")
    return issues


def _run_execution(project: ProjectState, agents: dict) -> str:
    """分发 Architect 分解的子任务到 Implementer/Builder。"""
    if not project.architecture:
        return "无架构方案，无法分发执行"

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

    project.constraints_checklist = constraints
    project.phase = Phase.GATE3  # 执行完成后进入 Gate3
    save(project)
    return f"已分发 {created} 个子任务到 E/E+/D 层"


def _run_review(project: ProjectState, agents: dict) -> str:
    """调 Reviewer(D层) 同步增量审查 → 解析 JSON → 写入 project.issues。"""
    if _should_skip(project, "skip_gate3"):
        project.phase = Phase.GATE4
        save(project)
        return "审查已跳过 (Owner 设定 skip_gate3)"

    # 1. 收集本次改动的文件 (从 task traces)
    changed_files = set()
    for tid in project.task_ids:
        trace_path = config.TRACE_DIR / f"{tid}.json"
        if trace_path.exists():
            try:
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
                for f in trace.get("changed_files", []):
                    changed_files.add(f)
            except Exception as e:
                try:
                    from . import witness
                    witness.heartbeat("workflow", f"warn:trace_read:{e}"[:80])
                except Exception:
                    pass
    # 无改动文件 → 跳过审查, 不调LLM
    if not changed_files:
        project.issues = []
        project.add_lineage({"action": "review_skipped", "reason": "no_changed_files"})
        project.phase = Phase.GATE4
        save(project)
        return "审查跳过: 无改动文件"

    changed_str = ", ".join(sorted(changed_files))

    architecture_json = json.dumps(project.architecture, ensure_ascii=False, indent=2) \
        if project.architecture else "无架构方案"
    task_ids_str = ", ".join(project.task_ids) if project.task_ids else "无任务"

    # 2. 构建 prompt
    prompt = _REVIEWER_PREAMBLE.format(
        architecture=architecture_json,
        task_ids=task_ids_str,
        changed_files=changed_str,
    )

    # 3. 同步 dispatch 到 D 层 (审查需要真推理能力)
    task_id = f"review_{project.id}"
    disp_result = None
    raw = ""
    try:
        disp_result = disp_mod.dispatch(
            prompt, "D", task_id, agents,
            project_lineup=project.agent_lineup,
        )
        raw = disp_result.executor_result.raw_output if disp_result else ""
    except Exception:
        raw = ""

    # 4. 解析 JSON 产出
    import re as _re
    review = None
    if raw:
        try:
            m = _re.search(r"```json\s*\n(.*?)\n```", raw, _re.DOTALL)
            if m:
                review = json.loads(m.group(1))
            else:
                m2 = _re.search(r"\{[\s\S]*\}", raw)
                if m2:
                    review = json.loads(m2.group())
        except (json.JSONDecodeError, Exception):
            pass
    if review is None:
        review = {"raw_output": raw[:5000], "parse_error": True}

    issues = review.get("issues", [])
    project.issues = issues
    project.add_lineage({"action": "review_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?",
                         "issue_count": len(issues)})

    # 5. 推进到 GATE4
    project.phase = Phase.GATE4
    save(project)
    return (
        f"审查完成: 发现 {len(issues)} 个问题 "
        + (f"(bug={sum(1 for i in issues if i.get('severity')=='bug')})" if issues else "")
    )


def _run_fixing(project: ProjectState, agents: dict) -> str:
    """为 Reviewer 发现的每条 issue 创建修复任务，然后回到审查循环。"""
    if _should_skip(project, "skip_gate4"):
        project.phase = Phase.DONE
        save(project)
        return "修复已跳过 (Owner 设定 skip_gate4)"

    if not project.issues:
        project.phase = Phase.DONE
        save(project)
        return "无 issues，项目完成 ✅"

    # 检查迭代次数，防死循环
    fix_rounds = sum(1 for e in project.lineage if e.get("action") == "fixing_round")
    if fix_rounds >= 5:
        project.phase = Phase.DONE
        save(project)
        return f"修复已迭代 {fix_rounds} 轮，达上限，请人工接管"

    created = 0
    for issue in project.issues:
        severity = issue.get("severity", "style")
        level = "E+" if severity == "bug" else "E"

        child = tracker.create(
            f"[修复] {issue.get('id', '?')} {issue.get('title', '')}: {issue.get('description', '')}",
            depth=2,
        )
        tracker.transition(child.id, TaskStatus.PENDING,
                           route_level=level, route_locked=True,
                           project_id=project.id)
        project.task_ids.append(child.id)
        created += 1

    # 记录迭代
    project.add_lineage({"action": "fixing_round", "round": fix_rounds + 1,
                         "issues_fixed": len(project.issues)})
    # 回到审查阶段，验证修复效果
    project.phase = Phase.REVIEWING
    save(project)
    return f"已创建 {created} 个修复任务 (第{fix_rounds+1}轮)，回到审查验证"


def start_project_workflow(project: ProjectState, agents: dict) -> str:
    """项目工作流入口: 根据 phase 执行下一步。"""
    # 需求确认 → research
    if project.phase == Phase.TEMPLATE and project.description:
        if _needs_research(project):
            project.phase = Phase.RESEARCHING
        else:
            project.phase = Phase.PLANNING
        save(project)

    return run_phase(project, agents)
