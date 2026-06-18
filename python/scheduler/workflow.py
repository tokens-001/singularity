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

你需要产出架构方案 + 任务分解清单 + 约束。输出严格 JSON:

{{
  "architecture": "主设计思路 + 模块划分 + 数据流",
  "tasks": [
    {{
      "id": "T1",
      "title": "任务标题",
      "description": "任务详细描述",
      "complexity": "low|medium|high",
      "depends_on": [],
      "acceptance": "验收标准",
      "estimated_files": ["涉及文件"]
    }}
  ],
  "constraints": [
    "不改 xx 接口",
    "保持现有测试通过",
    "不引入新的第三方依赖"
  ],
  "risks": ["风险1"],
  "test_strategy": "测试策略"
}}

注意:
- low→E, medium→E+, high→D
- depends_on 填任务 ID 列表
- 每个任务必须只改不相交的文件 (并行 merge 的前提)
- 你只出方案和清单，不写代码。"""


def _needs_research(project: ProjectState) -> bool:
    """判断是否需要调研阶段。"""
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
        return f"等待 Owner {phase.value} 确认"

    elif phase == Phase.PLANNING:
        return _run_planning(project, agents)

    elif phase == Phase.EXECUTING:
        return _run_execution(project, agents)

    elif phase == Phase.REVIEWING:
        return "等待 Supervisor 全项目审查 (P2)"

    elif phase == Phase.FIXING:
        return "等待执行修复循环 (P2)"

    elif phase == Phase.DONE:
        return "项目已完成"

    return f"未知 phase: {phase.value}"


def _run_research(project: ProjectState, agents: dict) -> str:
    """调 Researcher(E层) 搜集可借鉴方案。"""
    if _should_skip(project, "gate1"):
        project.phase = Phase.PLANNING
        save(project)
        return "调研已跳过 (Owner 设定)"

    prompt = _RESEARCHER_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
    )

    # 作为任务提交执行
    task = tracker.create(
        f"[调研] {project.name}: 搜集可借鉴架构方案",
        depth=1,
    )
    tracker.transition(task.id, TaskStatus.PENDING,
                       route_level="E", route_locked=True)
    project.task_ids.append(task.id)

    # 推进到 gate1
    project.phase = Phase.GATE1
    save(project)
    return f"调研任务 {task.id[:8]} 已入队，等待 Owner Gate1 确认"


def _run_planning(project: ProjectState, agents: dict) -> str:
    """调 Architect(D层) 出方案+任务清单。"""
    if _should_skip(project, "gate2"):
        return "规划已跳过 (Owner 设定)"

    research_json = json.dumps(project.research_report, ensure_ascii=False, indent=2) \
        if project.research_report else "无调研报告"

    prompt = _ARCHITECT_PREAMBLE.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
        research=research_json,
    )

    task = tracker.create(
        f"[架构] {project.name}: 出方案+任务清单",
        depth=1,
    )
    tracker.transition(task.id, TaskStatus.PENDING,
                       route_level="D", route_locked=True)
    project.task_ids.append(task.id)
    project.phase = Phase.GATE2
    save(project)
    return f"架构任务 {task.id[:8]} 已入队(D层)，等待 Owner Gate2 确认"


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
                           route_level=level, route_locked=True)
        project.task_ids.append(child.id)
        if not parent_id:
            parent_id = child.id
        created += 1

    project.constraints_checklist = constraints
    project.phase = Phase.GATE3  # 执行完成后进入 Gate3
    save(project)
    return f"已分发 {created} 个子任务到 E/E+/D 层"


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
