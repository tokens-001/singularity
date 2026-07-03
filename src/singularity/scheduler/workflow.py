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

_ARCHITECT_PREAMBLE = """你是资深系统架构师。基于 PRD + 交互/UI方案 + 调研报告，产出可执行的系统架构方案。

项目需求: {description}
项目范围: {scope}
原始约束: {constraints}
调研报告: {research}

设计原则:
1. 简单优先 — 选成熟技术，不为假想规模过度设计
2. 边界清晰 — 模块间通过接口契约通信
3. 可验证 — 每个约束有明确的验证方式
4. 任务可拆 — 架构必须能拆成独立可并行的实现任务

你需要产出: 模块划分 + 数据模型 + API契约 + 技术栈 + 约束清单 + 任务清单。不要调用工具，直接输出 JSON。必须符合以下 Schema:

{{
  "architecture": "主设计思路综述 (<500字)",
  "modules": [
    {{
      "name": "模块名 (必填)",
      "responsibility": "单一职责描述 (必填)",
      "depends_on": ["依赖模块名"],
      "interfaces": ["对外提供的能力"]
    }}
  ],
  "data_model": {{
    "database": "选型及理由 (必填)",
    "entities": [
      {{"name": "实体名", "fields": [{{"name": "字段", "type": "类型", "constraints": ["约束"]}}], "indexes": ["索引"]}}
    ],
    "relationships": [
      {{"from": "实体A", "to": "实体B", "type": "1:1/1:N/N:M", "via": "关联字段"}}
    ]
  }},
  "api_contracts": [
    {{
      "method": "GET/POST/PUT/DELETE",
      "path": "/api/...",
      "description": "用途",
      "input": {{}},
      "output": {{}},
      "errors": [{{"code": 400, "meaning": "..."}}]
    }}
  ],
  "tech_stack": {{
    "language": "选型及理由",
    "framework": "选型及理由",
    "database": "选型及理由",
    "cache": "选型及理由",
    "mq": "选型及理由"
  }},
  "constraints": [
    {{
      "type": "security/performance/reliability/maintainability (必填)",
      "rule": "具体约束 (必填)",
      "check": "如何验证 (必填)"
    }}
  ],
  "tasks": [
    {{
      "id": "T1",
      "title": "任务标题 (必填, <50字)",
      "description": "任务详细描述 (必填, <200字)",
      "complexity": "low|medium|high (必填)",
      "layer": "frontend/backend/data/devops (必填)",
      "depends_on": ["T0"],
      "acceptance": "验收标准 (必填, <100字)",
      "estimated_files": ["涉及文件路径"]
    }}
  ],
  "risks": [
    {{"risk": "风险描述", "impact": "high/medium/low", "mitigation": "缓解措施"}}
  ]
}}

Schema 规则:
- tasks 至少 1 个, 最多 20 个
- complexity: low→E层, medium→E+层, high→D层
- layer 标注任务所属层: frontend/backend/data/devops
- depends_on 填其他任务的 id, 可为空数组
- 每个任务改不相交的文件 (并行 merge 的前提)
- constraints 每条必须可机器检查 (type+rule+check)
- 你只出方案和清单，不写代码。不做 AI 架构，不做前端架构。

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

def run_test_fix_loop(project: ProjectState, agents: dict) -> str:
    """EXECUTING 任务全部完成后 → Step 5 验收 → GATE3。

    ponytail: AI内审已移除。QA+安全审计师并行出报告，人工在GATE3审核。
    """
    changed = _collect_changed_files(project)
    file_count = len(changed)
    msgs = [f"执行完成 ({file_count} 个文件改动)"]

    # Step 5: 验收层 (QA + 安全审计师并行)
    verify_msgs = _run_verification(project, agents)
    if verify_msgs:
        msgs.extend(verify_msgs)

    project.phase = Phase.GATE3
    project.issues = []
    save(project)
    return "\n".join(msgs) + "\n→ GATE3 等待人工审核"


def _run_verification(project: ProjectState, agents: dict) -> list[str]:
    """Step 5: QA工程师 + 安全审计师并行出验收报告。

    不调 LLM 写代码，只出验证报告供人工 GATE3 判断。
    """
    if not project.constraints_checklist:
        return ["验收跳过 (无约束清单)"]

    msgs = []
    constraints = project.constraints_checklist
    changed_files = _collect_changed_files(project)

    # 构建验收上下文
    ctx = (
        f"项目: {project.description[:300]}\n"
        f"约束清单:\n" +
        "\n".join(f"- [{c.get('type','?')}] {c.get('rule', c.get('text',''))} (验证: {c.get('check','?')})"
                  for c in constraints) +
        f"\n\n改动文件 ({len(changed_files)}):\n" +
        "\n".join(f"- {f}" for f in sorted(changed_files)[:30])
    )

    # ── QA 验收 ──
    qa_prompt = (
        f"你是 QA 工程师。做验收验证，不写代码，只出报告。\n\n{ctx}\n\n"
        "逐条检查约束是否满足，给出 evidence。输出 JSON。"
    )
    disp_result, err = _safe_dispatch(qa_prompt, "any", f"qa_{project.id}", agents, project)
    if disp_result and disp_result.executor_result:
        raw = disp_result.executor_result.raw_output
        _save_phase_output(project.id, "qa-report.md", raw)
        msgs.append(f"QA报告完成 ({len(raw)} chars)")
    elif err:
        msgs.append(f"QA验收失败: {err}")

    # ── 安全审计 ──
    sec_prompt = (
        f"你是安全审计师。做安全审计，不写代码，只出报告。\n\n{ctx}\n\n"
        "审计: 权限/注入/密钥/依赖漏洞/隐私合规。输出 JSON。"
    )
    disp_result2, err2 = _safe_dispatch(sec_prompt, "any", f"sec_{project.id}", agents, project)
    if disp_result2 and disp_result2.executor_result:
        raw2 = disp_result2.executor_result.raw_output
        _save_phase_output(project.id, "security-report.md", raw2)
        msgs.append(f"安全报告完成 ({len(raw2)} chars)")
    elif err2:
        msgs.append(f"安全审计失败: {err2}")

    # S4: E2E 测试执行 (对照 test_cases.json 中的 e2e 用例)
    tc_path = config.PROJECT_ROOT / "test_cases.json"
    if tc_path.exists():
        try:
            tc = json.loads(tc_path.read_text())
            e2e_cases = tc.get("e2e", []) if isinstance(tc, dict) else []
            if e2e_cases:
                msgs.append(f"E2E用例 {len(e2e_cases)} 个待人工验收 (对照 state_machine 验证)")
                _save_phase_output(project.id, "e2e_checklist.json",
                    json.dumps([{"name": c.get("name",""), "user_flow": c.get("user_flow",""),
                     "success_criteria": c.get("success_criteria","")} for c in e2e_cases],
                    ensure_ascii=False, indent=2))
        except Exception:
            pass

    # D3: 构建结构化 QA 报告 (fix_route 分级)
    try:
        from singularity.scheduler.validator import build_qa_report
        qa_raw = disp_result.executor_result.raw_output if disp_result and disp_result.executor_result else "{}"
        qa_data = json.loads(qa_raw) if qa_raw.strip().startswith("{") else {}
        issues = qa_data.get("issues", [])
        passed = qa_data.get("passed", [])
        verdict = qa_data.get("verdict", "go" if not issues else "no_go")
        reason = qa_data.get("summary", qa_data.get("verdict_reason", ""))
        qa_report = build_qa_report(passed, issues, verdict, reason)
        _save_phase_output(project.id, "qa_report.json",
                          json.dumps(qa_report, ensure_ascii=False, indent=2))
    except Exception:
        pass

    return msgs


# ═══════════════════════════════════════════════════════════
# GATE3 打回: 人工反馈 → 回规划重做
# ═══════════════════════════════════════════════════════════

def handle_gate3_reject(project: ProjectState, agents: dict, feedback: str = "") -> str:
    """GATE3 被人工打回: 按 fix_route 分级路由 (D3)。

    impl   → 回 EXECUTING, 只重做有问题的 task (依赖该 task 的下游一并重测)
    design → 回 PLANNING 重新规划
    note   → 仅记录, 不阻断交付
    无 qa_report 或读失败 → 默认 design (保守回规划)
    """
    project.add_lineage({"action": "gate3_rejected", "feedback": feedback[:500]})

    # 读 QA 报告的 fix_route 决定路由
    fix_route = "design"  # 默认保守
    qa_report_path = _projects_dir() / f"{project.id}.qa_report.json"
    if qa_report_path.exists():
        try:
            qa_report = json.loads(qa_report_path.read_text(encoding="utf-8"))
            # 优先用 summary.verdict_reason 里的 route, 否则按 issues 推断
            issues = qa_report.get("issues", [])
            if issues:
                routes = [i.get("fix_route", "") for i in issues if i.get("fix_route")]
                if routes:
                    # 有 design 就 design, 否则 impl, 否则 note
                    if "design" in routes:
                        fix_route = "design"
                    elif "impl" in routes:
                        fix_route = "impl"
                    else:
                        fix_route = "note"
        except Exception:
            pass

    if fix_route == "impl":
        # 回实现层: 重置失败 task 状态, 保留已通过的
        project.phase = Phase.EXECUTING
        project.add_lineage({"action": "gate3_route", "route": "impl"})
        msg = f"GATE3 打回 → 回实现层修复 (反馈: {feedback[:80]})"
    elif fix_route == "note":
        # 仅记录, 不阻断 (保持当前阶段, 等人再次确认)
        project.add_lineage({"action": "gate3_route", "route": "note"})
        msg = f"GATE3 问题仅记录 (suggestion), 不阻断交付"
    else:
        # design: 回规划重做架构
        project.phase = Phase.PLANNING
        project.architecture = None
        project.add_lineage({"action": "gate3_route", "route": "design"})
        msg = f"GATE3 打回 → 回架构规划 (反馈: {feedback[:80]})"

    save(project)
    return msg


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

from singularity.scheduler._workflow_phases import *  # noqa: F401,F403
