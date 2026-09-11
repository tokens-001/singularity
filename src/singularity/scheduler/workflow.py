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
from singularity.scheduler.project import ProjectState, Phase, save, _projects_dir, resolve_flow
from singularity.scheduler.tracker import TaskStatus

from singularity.scheduler._io import try_parse_json


# ═══════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════

_RESEARCHER_CONTEXT = """项目需求: {description}
项目范围: {scope}
原始约束: {constraints}

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
}}"""

_ARCHITECT_CONTEXT = """项目需求: {description}
项目范围: {scope}
原始约束: {constraints}
调研报告: {research}

必须符合以下 Schema:

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
      "check": {{"argv": ["python3", "-m", "pytest", "-q"], "expect_exit": 0}},
      "covers": [0, 2]
    }}
  ]
}}

Schema 规则:
- 字段顺序就是输出顺序: tasks/risks 是下游拆任务唯一的依据, 先写它们 ——
  长输出万一被截断, 丢的必须是长尾而不是命根子
- tasks 至少 1 个, 最多 20 个
- complexity: low→廉价层, medium→中档层, high→强力层
- layer 标注任务所属层: frontend/backend/data/devops
- depends_on 填其他任务的 id, 可为空数组
- 每个任务改不相交的文件 (并行 merge 的前提)
- constraints 每条带 `covers`：这条约束覆盖 **调研报告里 `scope_clarification.core`
  的哪几条**（给 **0 起算的索引数组**，也可写原文）。**每一条 core 需求都必须被至少
  一条约束覆盖到** —— 覆盖不了的需求就是**没人验的需求**，会被算成缺口暴露出来。
  ⚠️ 别为了好看把 covers 乱填：填了就要真能验，填错等于伪造覆盖率。
- constraints 每条必须可机器检查 (type+rule+check)。`check` 两种写法，二选一：
  · **能机器跑**的 → `{{"argv": ["解释器或程序", "参数", ...], "expect_exit": 0}}`
    必须是**数组**（平台按数组直接 exec，**不过 shell**）。argv[0] 只允许:
    python3 / python / pytest / npm / node / git / ls / cat / wc / test。
    `python3` 只允许紧跟 `-m pytest`（不许 `-c`：那等于任意代码执行）。
  · **机器验不了**的（界面美观、命名风格之类）→ **如实写一段散文说明为什么验不了**。
    ⚠️ **不许编一条反正跑不通的命令来凑格式** —— 那比写散文更坏：
    平台会当真去跑，然后拿一个假的失败（或假的通过）当验收结论。

输出时用 ```json ... ``` 包裹。"""

# ponytail: AI内审已移除，人审在GATE1/GATE2/GATE3把关


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════

def _phase_cwd(project: ProjectState) -> str:
    """非执行阶段（调研/架构/QA/安全）的运行目录 = **项目自己的仓库**。

    这里以前不给 cwd，`dispatch` 的默认值是空串，执行器再兜底成
    `config.PROJECT_ROOT` —— **奇点仓库自己**。于是调研员带着写文件/跑命令的工具，
    在**主仓根目录**里干活：2026-09-11 实测仓库根冒出 `wc_lite.py` + `examples/`
    （同一形状此前已在委员会合成那条支路上踩到过一次，当时只修了那一处，
    见 `_dispatch_exec.py` 合成兜底的注释 —— 修症状没修根因）。

    非执行阶段**没有 worktree 隔离**，cwd 就是唯一那道边界，不能空。
    取不到项目目录时也**绝不退回 PROJECT_ROOT**：先自己 mkdir，宁可 cwd 是个空目录。
    """
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import witness
    try:
        return str(proj_mod.ensure_repo(project.id))   # mkdir + git init，幂等
    except Exception as e:
        witness.warn("workflow", f"phase_cwd_fallback:{type(e).__name__}:{e}"[:120])
        d = proj_mod.repo_dir(project.id)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return str(d)


def _safe_dispatch(prompt: str, level: str, task_id: str, agents: dict,
                   project: ProjectState, project_lineup=None,
                   restrict_to_lineup: bool = False, phase: str = "",
                   no_tools: bool = False,
                   allow_committee: bool | None = None) -> tuple:
    """调 disp_mod.dispatch 并记录错误到 project lineage。返回 (disp_result_or_None, error_str)。

    ``restrict_to_lineup`` 见 `dispatcher.pick_agent_fallback_chain`：「阶段 → 模型」
    配了名单时为 True，委员会席位才限制得住。

    ``phase`` 透传给技能解析（阶段级技能绑定）。不传 = 只用模型级绑定。

    ``cwd`` 必须给（见 `_phase_cwd`）：漏传 = 在奇点仓库自己里跑。

    ``no_tools`` 见 `dispatch` 的同名参数：产出是 JSON 的阶段（调研/架构）必须给，
    否则模型会当实现任务干、把工具轮次烧光，报告一句不剩。

    ``allow_committee`` 为 None 时**在这里**按项目重量推导（`resolve_flow`）。
    刻意放在函数内部而不是调用点：`_run_planning` 的调用点签名不动，
    `tests/test_scheduler/test_phase_dispatch_wiring.py` 的桩继续可用。
    传 True/False = 显式覆盖。
    """
    if allow_committee is None:
        allow_committee = resolve_flow(project).committee
    try:
        disp_result = disp_mod.dispatch(
            prompt, level, task_id, agents,
            cwd=_phase_cwd(project),
            project_lineup=project_lineup,
            restrict_to_lineup=restrict_to_lineup,
            phase=phase,
            no_tools=no_tools,
            project_id=project.id,
            allow_committee=allow_committee,
        )
        _record_phase_usage(project, task_id, level, disp_result)
        return disp_result, ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        project.add_lineage({"action": "llm_error", "level": level, "task_id": task_id, "error": err})
        return None, err


def _is_composite_model_name(name) -> bool:
    """是不是"聚合体合成名"（`fusion(a,b)` / `committee(a,b)`）—— 这类名字在计价表里
    查不到，记进账本只会让这一行永远算不出钱。"""
    s = str(name or "")
    return "(" in s and ")" in s and not s.startswith("http")


def _record_phase_usage(project: ProjectState, task_id: str, level: str,
                        disp_result) -> None:
    """把一次阶段调用的用量记到项目账上。

    这条路上原来**一次都不记**：`record_tokens` 全仓只有一个调用方
    （`_task_runner.py`，调度循环派任务那条），而调研 / 架构 / QA / 安全 都走
    本函数。后果是三重的 ——

    1. **项目花费恒为 0**（`project_cost` 按 project_id 查，查不到东西）；
    2. **预算无从比对**：`token_budget_total` 拦不住，根子在这儿而不只是"没接判断"；
    3. 那些调用挤进 `_unknown` 桶，成了用量页最大的一桶。

    2026-09-11 实测：三次完整调研（各 ~77 秒、上万字报告）在用量表上**贡献 0**，
    那一小时只有各一发 router 被记下（432×3）。

    记账失败不能把阶段带崩，但也**不许静默** —— 静默就等于又回到"账对不上还查不出"。
    """
    try:
        from singularity.scheduler import witness
        from singularity.scheduler._token_budget import record_tokens
        er = getattr(disp_result, "executor_result", None)

        # 委员会：**按成员逐个记**。记成一条、模型名用合成串 `fusion(a,b)` 的话，
        # 计价表里当然没有这个名字 → 费用按 None 跳过 → 最贵的架构阶段
        # 记了账却算不出钱（2026-09-11 探路轮实测：项目 cost 恒 $0.0000）。
        # per-member 的 token 本来就在手上（_dispatch_committee 里），以前直接扔了。
        usage = getattr(er, "member_usage", None)
        if isinstance(usage, list) and usage:
            for u in usage:
                record_tokens(project_id=project.id, project_name=project.name,
                              task_id=task_id, level=level,
                              model=str(u.get("model", "")),
                              tokens=int(u.get("tokens", 0) or 0),
                              elapsed_s=float(u.get("elapsed", 0.0) or 0.0))
            return

        tokens = int(getattr(er, "token_count", 0) or 0)
        if tokens <= 0:
            return
        model_name = (getattr(disp_result, "agent_cfg", None) or {}).get("model", "")
        if _is_composite_model_name(model_name):
            # 聚合体的合成名（`fusion(a,b)` / `committee(a,b)`）在计价表里查不到，
            # 记了也是白记（费用按 None 跳过）。成员那几行上面已经记过了，这条跳过。
            return
        record_tokens(project_id=project.id, project_name=project.name,
                      task_id=task_id, level=level, model=model_name,
                      tokens=tokens,
                      elapsed_s=float(getattr(er, "elapsed", 0.0) or 0.0))
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn("workflow", f"record_phase_usage:{type(e).__name__}:{e}"[:120])

def _needs_research(project: ProjectState) -> bool:
    """**转发到唯一判据** `resolve_flow`（防御模式 §5）。这里不再有自己的关键词表。

    保留这个名字是因为有两个调用点（`start_project_workflow` 的真决策、
    `_api_projects.project_cost` 的显示），转发一下两边就自动一致了。
    """
    return resolve_flow(project).research


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

        elif phase in (Phase.INTEGRATING, Phase.DELIVERING):
            # 非人门，和 EXECUTING 同类：在这儿交棒给调度循环
            # （`orchestrator._auto_trigger_test_fix` 推 integrating / delivering）。
            # 以前落到下面的 else，报"未知 phase" —— 而它明明是个正经阶段。
            # 消息里点明"循环没开就会停这儿"：那条路今天踩过（项目卡着等人，外面看着像坏了）。
            msgs.append(f"{phase.value} 由调度循环推进，无需人工操作"
                        f"（调度循环没开的话项目会停在这一步）")
            break

        elif phase in (Phase.GATE1, Phase.GATE2, Phase.GATE3):
            if project.auto_mode:
                before = project.phase
                project.confirm_gate(phase, "approved")
                save(project)
                if project.phase == before:
                    # 门没放行（phase 没动）——比如 GATE2 架构校验没过。
                    # **必须 break**：原来这里无条件 `continue`，
                    # 配上"不放行就原地不动"就变成死循环 —— auto_mode 下 CPU 烧到天荒地老，
                    # 而且外面看不出来（不报错、不退出，测试是**挂住**不是失败）。
                    msgs.append(f"auto: {phase.value} 未放行（校验未通过）→ 停下等人工")
                    break
                msgs.append(f"auto: {phase.value} → {project.phase.value}")
                continue
            msgs.append(f"等待 Owner {phase.value} 确认")
            break

        elif phase == Phase.REVIEWING:
            # 瞬时态：集成合并通过后由 orchestrator 置上，验收（QA+安全审计）跑几分钟，
            # 这里推进到 GATE3 交人工。**不是死代码** —— UI 靠它显示"审查中"。
            # （原来紧跟的 FIXING 分支已删：全仓无人赋值，状态不可达。）
            #
            # **别说"验收完成"**：REVIEWING 被两套驱动同时认识，但只有 orchestrator
            # 那条会跑验收。人手点"下一步"时验收多半还没跑完（异步线程还在跑）、
            # 甚至从没开始（线程炸了 / auto_mode 且调度循环没开）。真相由
            # set_phase 的入门票判定 —— 缺证据时 issues 里会多一条 gate3_no_evidence。
            _verified = project.has_verification_evidence()
            project.set_phase(Phase.GATE3,
                              "验收报告完毕 → 交人工" if _verified else "未验收 → 交人工")
            save(project)
            msgs.append("验收完成 → GATE3 等人工审核" if _verified else
                        "⚠ 未取得验收报告 → GATE3 等人工审核（该页结论不构成有效验收）")
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

    # 清空上一轮 issues 必须在验收**之前**。原来放在验收之后, 会把 _run_verification
    # 刚记进去的"验收跳过"一并擦掉 —— 于是 GATE3 面板永远看到 issues: []，
    # 那句"跳过"只活在一条一闪而过的聊天消息里。
    project.issues = []

    # Step 5: 验收层 (QA + 安全审计师并行)
    verify_msgs = _run_verification(project, agents)
    if verify_msgs:
        msgs.extend(verify_msgs)

    project.set_phase(Phase.GATE3, "执行完成 → 验收报告完毕, 等人工审")
    save(project)
    return "\n".join(msgs) + "\n→ GATE3 等待人工审核"


def _run_verification(project: ProjectState, agents: dict) -> list[str]:
    """Step 5: QA工程师 + 安全审计师并行出验收报告。

    不调 LLM 写代码，只出验证报告供人工 GATE3 判断。
    """
    if not project.constraints_checklist:
        # 记进 issues: 只返回字符串的话，这句话不落盘, GATE3 只能看到一个空 issues
        # 和一份不存在的 QA 报告, 谁也说不清验收为什么没跑。
        # 这条也是进 GATE3 的**入门票**之一（见 ProjectState._gate3_admission）：
        # 它代表"验收有过结论"，结论就是"没跑"。
        reason = "验收跳过: 架构没产出约束清单, QA/安全审计师都没跑"
        project.issues.append({"type": "verification_skipped", "detail": reason})
        return [reason]

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

    # ── ① 机械检查：约束里**能机器跑**的那几条，先跑（机器证据优先）──
    # 这是"信任上限 = 机械证据覆盖的验证面比例"的那个分子。
    # 运行前提：人在 **GATE2 批架构时就看过这些命令**（面板上明写"批准后会实际执行"）。
    # 安全护栏在 `_machine_checks`：argv 数组不过 shell / argv[0] 白名单 /
    # 解释器只许 -m pytest / cwd 锁项目仓 / 超时 / 环境洗掉 key 与代理。
    _MAX_MACHINE_CHECKS = 10      # 有上限就明说，别静默截断
    try:
        from singularity.scheduler import _machine_checks as mchk
        runnable = [c for c in (project.constraints_checklist or [])
                    if isinstance(c, dict) and mchk.validate_check(c.get("check"))[0]]
        if runnable:
            root = _phase_cwd(project)
            picked, dropped = runnable[:_MAX_MACHINE_CHECKS], runnable[_MAX_MACHINE_CHECKS:]
            results = []
            for c in picked:
                r = mchk.run_check(c.get("check"), root)
                results.append({"rule": c.get("rule", c.get("text", "")), **r})
            passed = sum(1 for r in results if r.get("passed"))
            note = f"机械检查 {passed}/{len(results)} 条通过"
            if dropped:
                note += f"（另有 {len(dropped)} 条超出上限 {_MAX_MACHINE_CHECKS}，本轮未跑）"
            project.issues = [i for i in project.issues if i.get("type") != "machine_checks"]
            project.issues.append({"type": "machine_checks", "detail": note})
            project.add_lineage({"action": "machine_checks", "ran": len(results),
                                 "passed": passed, "skipped": len(dropped)})
            _save_phase_output(project.id, "machine-checks.json",
                               json.dumps(results, ensure_ascii=False, indent=2))
            msgs.append(note)
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn("review", f"machine_checks:{type(e).__name__}:{e}"[:120])

    # ── ② QA 验收（LLM，兜机械查不了的残余面）──
    qa_prompt = (
        f"你是 QA 工程师。做验收验证，不写代码，只出报告。\n\n{ctx}\n\n"
        "逐条检查约束是否满足，给出 evidence。输出 JSON。"
    )
    disp_result, err = _safe_dispatch(qa_prompt, "any", f"qa_{project.id}", agents, project,
                                      phase="reviewing")
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
    disp_result2, err2 = _safe_dispatch(sec_prompt, "any", f"sec_{project.id}", agents, project,
                                        phase="reviewing")
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

    # 验收入门票：走到这儿才算"验收真的跑过"。放在**最后**、而不是开头 ——
    # 中途抛异常时不该留下"跑过了"的假证据。GATE3 靠这个标记识别
    # "验收整段没跑就被推进来了"（见 ProjectState._gate3_admission）。
    project.issues.append({"type": "verification_ran", "detail": "QA + 安全审计已执行"})
    return msgs


# ═══════════════════════════════════════════════════════════
# GATE3 打回: 人工反馈 → 回规划重做
# ═══════════════════════════════════════════════════════════

def _read_observer_rollup(project_id: str) -> str | None:
    """读观察者在 GATE3 产出的验收裁决（`fix_route_decision`）。

    落盘方是 `_observer_answer._persist_gate3_rollup`（**同一条路径**：
    `get_project_dir(id)/observer_rollup.json`）。没有 / 读不动 / 不合法 → None，
    调用方就完全走原逻辑 —— **fail-closed，绝不猜**。这个返回值会让项目回退到某一层，
    猜错的代价是清空架构重走 GATE2（那条转圈在下面注释里记着）。
    """
    try:
        from singularity.scheduler.project import get_project_dir
        from singularity.scheduler._observer_definition import VERDICT_FIX_ROUTES
        path = get_project_dir(project_id) / "observer_rollup.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        route = data.get("fix_route_decision")
        return route if route in VERDICT_FIX_ROUTES else None
    except Exception:
        return None


def handle_gate3_reject(project: ProjectState, agents: dict, feedback: str = "") -> str:
    """GATE3 被人工打回: 按 fix_route 分级路由 (D3)。

    impl   → 回 EXECUTING, 只重做有问题的 task (依赖该 task 的下游一并重测)
    design → 回 PLANNING 重新规划
    note   → 仅记录, 不阻断交付
    无 qa_report 或读失败 → 默认 design (保守回规划)
    """
    project.add_lineage({"action": "gate3_rejected", "feedback": feedback[:500]})

    # 读 QA 报告的 fix_route 决定路由
    fix_route = "design"  # 有报告但没标路由 → 保守回架构
    has_qa = False
    qa_report_path = _projects_dir() / f"{project.id}.qa_report.json"
    if qa_report_path.exists():
        try:
            qa_report = json.loads(qa_report_path.read_text(encoding="utf-8"))
            has_qa = True
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
            has_qa = False

    # 没有报告 ≠ 问题在架构。报告缺失恰恰是「架构没产出 constraints → 验收被整个跳过」
    # (见 workflow.py _run_verification 的空清单早退) 的症状 —— 这时猜 design 会: 清空架构
    # → 重做架构 → GATE2 又请你审架构 → 打回 → 转圈, 而架构其实什么都没改。
    # 无依据时回实现层: 代价最小, 且不动架构。
    if not has_qa:
        fix_route = "impl"
        no_qa_reason = "无 QA 报告(验收可能被跳过), 无法判断退回哪层 → 默认回实现层, 不动架构"
    else:
        no_qa_reason = ""

    # 观察者的 GATE3 汇总**优先** —— 它是"汇总裁定"（schema 的 description 就这么写的），
    # 比按 issue 逐条推断更接近设计意图。没有 / 不合法 → 上面那套原逻辑一个字不改。
    _rollup_route = _read_observer_rollup(project.id)
    if _rollup_route:
        fix_route = _rollup_route
        no_qa_reason = ""
        route_source = "observer_rollup"
    else:
        route_source = "qa_report" if has_qa else "default_no_qa"

    if fix_route == "impl":
        # 回实现层: 重置 DONE task 为 PENDING, 让实现层重新执行
        # (否则打回后所有 task 仍 DONE, 队列无活任务 → 空转直达 GATE3, 缺陷从未修复)
        project.set_phase(Phase.EXECUTING, f"GATE3 打回(impl): {feedback[:60]}")
        reset_count = 0
        for tid in list(project.task_ids):
            t = tracker.read_task(tid)
            if t is not None and t.status == TaskStatus.DONE:
                # force=True: DONE 是终态, GATE3 打回是唯一合法的 DONE→PENDING 重置
                tracker.transition(tid, TaskStatus.PENDING, force=True)
                reset_count += 1
        project.add_lineage({"action": "gate3_route", "route": "impl", "reset_tasks": reset_count,
                             "source": route_source,
                             **({"no_qa": True} if no_qa_reason else {})})
        msg = f"GATE3 打回 → 回实现层修复 (重置 {reset_count} 任务, 反馈: {feedback[:80]})"
        if no_qa_reason:
            msg += f" ⚠ {no_qa_reason}"
    elif fix_route == "note":
        # 仅记录, 不阻断 (保持当前阶段, 等人再次确认)
        project.add_lineage({"action": "gate3_route", "route": "note", "source": route_source})
        msg = f"GATE3 问题仅记录 (suggestion), 不阻断交付"
    else:
        # design: 回规划重做架构
        project.set_phase(Phase.PLANNING, f"GATE3 打回(design): {feedback[:60]}")
        project.architecture = None
        project.add_lineage({"action": "gate3_route", "route": "design", "source": route_source})
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

    # 流程重量判据（唯一入口 resolve_flow）：调研走不走，架构开不开委员会。
    # ⚠️ 记一条 lineage —— 防御模式 §44 要求"跳过"必须是**带理由的显式事实**，
    # 不能是"没发生"。进 lineage 而不是 issues：issues 在 _run_execution 开头
    # 会被整体清空（workflow.py 里那句 `project.issues = []`），放那儿活不到 GATE3。
    d = resolve_flow(project)
    if d.research:
        project.set_phase(Phase.RESEARCHING, f"立项: 需调研 ({d.reason})")
    else:
        project.set_phase(Phase.PLANNING, f"立项: 免调研 ({d.reason})")
    project.add_lineage({
        "action": "flow_weight", "weight": d.weight, "source": d.source,
        "research": d.research, "committee": d.committee, "reason": d.reason,
    })
    save(project)

    return run_phase(project, agents)

from singularity.scheduler._workflow_phases import *  # noqa: F401,F403
