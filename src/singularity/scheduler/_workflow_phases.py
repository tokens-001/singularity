__all__ = ['_run_execution', '_run_planning', '_run_research', '_validate_architecture']

import json, os, time, logging

from singularity.scheduler import tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler.project import ProjectState, Phase, save, _projects_dir
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler._io import try_parse_json
from singularity.scheduler import orchestrator
from singularity.scheduler.roles import get_phase_role

from singularity.scheduler.workflow import (
    _safe_dispatch, _needs_research, _should_skip, _collect_changed_files,
    _phase_output_path, _save_phase_output, _read_phase_output,
    _ARCHITECT_CONTEXT, _RESEARCHER_CONTEXT,
)

def _phase_selection(phase: str, project: ProjectState):
    """某阶段该用哪些模型 → ``(lineup, restrict_to_lineup)``。

    统一从这里取。项目级 lineup 和全局阶段配置的优先级在 `phase_models.selection`
    里，各阶段别自己判 —— 有一处漏掉限制开关，那处的"指定"就只生效一半。
    """
    from singularity.scheduler import phase_models
    return phase_models.selection(phase, project)


def _index_phase_memory(project: ProjectState, prefix: str, stage: str, raw: str) -> None:
    """把一个阶段的产出记进 MAGMA 记忆（"按阶段切，不按任务切"）。

    以前只有**任务**进记忆（`_exec.py`），阶段产出只落盘不索引 ——
    于是"上次调研/架构这步是怎么想的"永远查不到，只能查到"上次那个任务"。
    见 docs/经验分层-STAIR借鉴-20260912.md。

    force=True 是必须的：阶段条目的描述都是同一条项目描述，
    彼此的 Jaccard 极高，不去重的话会被上一阶段的条目直接挤掉、静默不落盘。

    记忆挂了不该阻塞阶段 → 兜住异常；但**必须留痕**。
    """
    try:
        from . import memory as mem_mod
        mem_mod.index_task(
            task_id=f"{prefix}_{project.id}",
            description=f"[{stage}] {project.description}",
            created_at=getattr(project, "created_at", None),
            stage=stage,
            trajectory=raw or "",
            force=True,
        )
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn("memory", f"index_phase:{stage}:{type(e).__name__}:{e}"[:120])


def _probe_url(url: str, timeout: float = 5.0) -> bool:
    """能拿到**任何** HTTP 响应就算通（404/405 也算）—— 只有连不上/超时才算断。"""
    import httpx
    try:
        httpx.head(url, timeout=timeout, follow_redirects=True)
        return True
    except Exception:
        return False


def preflight_external(agents: dict) -> list[str]:
    """跑阶段前探一次外部依赖，探不通就**明着说** —— 别挂在 except 后面装死。

    2026-09-12 立（防御模式 §57）：`sentence_transformers` 加载嵌入模型时会联网查
    huggingface.co 的 metadata，域名不通就退避重试**挂死**；而**挂起不是异常，
    `except` 拦不住**，整条调研阶段无声停摆，外面看只剩"启动了没反应"。

    这里**不阻断阶段**（一次网络抖动不该卡死整个项目），只把"哪根线断了"变成
    一条能查到的记录。真正的兜底在 `_memory_core._get_embed_model`（`local_files_only`）。
    """
    problems: list[str] = []

    # ① 嵌入模型：能不能在**不联网**的前提下加载出来
    try:
        from . import _memory_core as _mc
        if _mc._get_embed_model() is None:
            problems.append(
                "嵌入模型不可用（本地无缓存或加载失败）→ 记忆语义检索会退化成空")
    except Exception as e:
        problems.append(f"嵌入模型探测异常: {type(e).__name__}: {e}")

    # ② 模型 API：base_url 连得上吗（同一家多个别名只探一次）
    try:
        from . import dispatcher as disp_mod
        bases = {a.get("base_url", "") for a in disp_mod._all_agents_list(agents or {})}
        for base in sorted(b for b in bases if b):
            if not _probe_url(base):
                problems.append(f"模型 API 不可达: {base}")
    except Exception as e:
        problems.append(f"模型 API 探测异常: {type(e).__name__}: {e}")

    return problems


def _run_preflight(project: ProjectState, agents: dict, stage: str) -> list[str]:
    """探一遍外部依赖，把结果记进项目（issues + lineage），返回问题列表。"""
    try:
        problems = preflight_external(agents)
    except Exception as e:  # 探测本身不该炸掉阶段
        problems = [f"preflight 自身异常: {type(e).__name__}: {e}"]

    project.issues = [i for i in project.issues if i.get("type") != "preflight"]
    if problems:
        for p in problems:
            project.issues.append({"type": "preflight", "detail": f"[{stage}] {p}"})
        try:
            from singularity.scheduler import witness
            witness.warn("preflight", f"{stage}:{problems[0]}"[:160])
        except Exception:
            pass
    project.add_lineage({"action": "preflight", "stage": stage,
                         "problems": len(problems)})
    return problems


_MAX_UNCOVERED_LISTED = 10   # issues 里最多列几条未被覆盖的需求（有上限就写出来）


def _run_budget_gate(project: ProjectState, stage: str) -> str:
    """阶段开跑前查项目预算。**返回空串 = 放行**，非空 = 硬停原因。

    `token_budget_total` 的第一个真实消费点 —— 此前全仓只在 API/CLI **显示层**
    被读过（防御模式 §28 同族：声明了没兑现）。80% 只记告警不拦，100% 停下等人工。
    ⚠️ 花费是**下限**（没配单价的模型不计入）。

    ⚠️ 调用方**必须**像 `_should_skip` 那样把 phase 推到门再返回：
    `run_phase` 对 RESEARCHING/PLANNING 是 `continue` 死循环，只 return 不改 phase 会转不出来。
    """
    try:
        from singularity.scheduler._token_budget import project_budget_state
        level, spent, msg = project_budget_state(
            project.id, getattr(project, "token_budget_total", 0) or 0)
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn("budget", f"{type(e).__name__}:{e}"[:120])
        return ""          # 探不了就不拦 —— 预算探测不该变成新的卡点

    project.issues = [i for i in project.issues if i.get("type") != "budget"]
    if not msg:
        return ""
    project.issues.append({"type": "budget", "detail": f"[{stage}] {msg}"})
    try:
        from singularity.scheduler import witness
        witness.warn("budget", f"{stage}:{msg}"[:160])
    except Exception:
        pass
    project.add_lineage({"action": "budget", "stage": stage,
                         "level": level, "spent": spent})
    return msg if level == "stop" else ""


def _run_research(project: ProjectState, agents: dict) -> str:
    """调 Researcher(廉价层) 搜集可借鉴方案 → GATE1。"""
    if _should_skip(project, "gate1"):
        project.set_phase(Phase.GATE1, "调研已跳过 → GATE1")
        save(project)
        return "调研已跳过"

    _stop = _run_budget_gate(project, "researching")
    if _stop:
        # 走和 _should_skip 同一条路：**必须改 phase**，否则 run_phase 原地死循环
        project.set_phase(Phase.GATE1, f"预算硬停 → GATE1 等人工：{_stop[:60]}")
        save(project)
        return f"预算硬停（未调研）: {_stop}"

    _run_preflight(project, agents, "researching")

    # 角色定位/六维度清单/边界在 roles.toml [surveyor]（页面上可改）；
    # 这里只填动态上下文 + 输出契约
    from singularity.scheduler.roles import get_role
    role = get_role(get_phase_role(Phase.RESEARCHING) or "surveyor")
    role_prompt = role.get_full_prompt() if role else "你是项目调研员。"
    prompt = f"{role_prompt}\n\n" + _RESEARCHER_CONTEXT.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
    )

    # MAGMA 记忆上下文
    try:
        from . import pre_search as pre_mod
        from . import router as router_mod
        route = router_mod.route(project.description)
        # 只有"这事之前栽过"才值得花几倍 token 去读全文：
        # 项目有返工/失败记录 → 走 deep，把历史任务的实际产出也取回来。
        # 没栽过就只用标题（depth 1，与改动前行为一致）。
        _deep = bool(getattr(project, "fix_round", 0) or getattr(project, "review_failures", 0))
        pre = pre_mod.pre_search(project.description, route, use_hybrid=True, deep=_deep)
        if pre.memory and pre.memory.narrative:
            lines = []
            for it in pre.memory.narrative[:5]:
                line = f"- [{it.get('task_id','')[-8:]}] {it.get('description','')[:80]}"
                full = it.get("full_text")
                if full:
                    mark = "（已截断）" if it.get("full_text_truncated") else ""
                    line += f"\n  ↳ 上次实际产出{mark}：\n{full}"
                lines.append(line)
            mem_ctx = "已知相关历史任务:\n" + "\n".join(lines)
            prompt = f"[背景记忆]\n{mem_ctx}\n\n{prompt}"
    except Exception:
        pass

    task_id = f"research_{project.id}"
    lineup, restrict = _phase_selection("researching", project)
    # no_tools: 调研员的产出契约是一段 JSON 报告，不该碰磁盘。不禁的话它会当实现任务
    # 干（2026-09-11 实测在项目仓库里把整个项目写完，工具轮次耗尽 → 报告只剩一句
    # "(达到最大工具轮次, 已产出文件)"，GATE1 无物可审）。
    disp_result, err = _safe_dispatch(prompt, "any", task_id, agents, project,
                                       lineup, restrict, phase="researching",
                                       no_tools=True)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    report = try_parse_json(raw)
    project.research_report = report
    # ponytail: 保存结构化调研报告供后续阶段复用
    _save_phase_output(project.id, "research.md", raw)
    _index_phase_memory(project, "research", "researching", raw)
    project.add_lineage({"action": "research_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?"})
    project.set_phase(Phase.GATE1, "调研完成 → GATE1 等人工")
    save(project)
    return f"调研完成: {len(report.get('competitive_analysis', {}).get('products', []))} 竞品, {len(report.get('frontier_theory', {}).get('papers', []))} 论文引用"


# ═══════════════════════════════════════════════════════════
# GATE2: 架构规划
# ═══════════════════════════════════════════════════════════

def _run_planning(project: ProjectState, agents: dict) -> str:
    """调 Architect(强力层) 出方案+任务清单 → GATE2。"""
    if _should_skip(project, "gate2"):
        project.set_phase(Phase.GATE2, "规划已跳过 → GATE2")
        save(project)
        return "规划已跳过"

    _stop = _run_budget_gate(project, "planning")
    if _stop:
        project.set_phase(Phase.GATE2, f"预算硬停 → GATE2 等人工：{_stop[:60]}")
        save(project)
        return f"预算硬停（未规划）: {_stop}"

    _run_preflight(project, agents, "planning")

    # 阶段上下文: 优先从磁盘读 research.md
    research_md = _read_phase_output(project.id, "research.md")
    if research_md:
        research_context = research_md[:5000]  # 截断避免 token 浪费
    elif project.research_report:
        research_context = json.dumps(project.research_report, ensure_ascii=False, indent=2)
    else:
        research_context = "无调研报告"

    # 角色定位/设计原则/边界在 roles.toml [architect]（页面上可改）；
    # 这里只填动态上下文 + 输出 Schema（代码要按它解析）
    from singularity.scheduler.roles import get_role
    arch_role = get_role(get_phase_role(Phase.PLANNING) or "architect")
    arch_prompt = arch_role.get_full_prompt() if arch_role else "你是资深系统架构师。"
    prompt = f"{arch_prompt}\n\n" + _ARCHITECT_CONTEXT.format(
        description=project.description,
        scope=project.scope,
        constraints=project.raw_constraints,
        research=research_context,
    )

    task_id = f"architect_{project.id}"
    # 架构这一项 = 委员会席位。restrict 才限制得住：不限制的话 chain 还是全池，
    # 界面上配的"三家"会变成"池里所有模型各出一份初稿"。
    lineup, restrict = _phase_selection("planning", project)
    # no_tools: 架构阶段的产出同样是 JSON 方案。委员会那条路自带禁工具，但**单模型**
    # 兜底那条没有 —— 席位只有一家时它会带着 write_file 去改磁盘。
    disp_result, err = _safe_dispatch(prompt, "any", task_id, agents, project,
                                       lineup, restrict, phase="planning",
                                       no_tools=True)
    raw = disp_result.executor_result.raw_output if disp_result else ""
    if err:
        raw = f'{{"parse_error": true, "error": "{err}"}}'

    arch = try_parse_json(raw, try_repair=True)
    if arch.get("parse_error"):
        retry_prompt = prompt + "\n\n[格式错误] 上一次输出不是合法JSON。请用 ```json ... ``` 包裹输出。"
        disp_result2, err2 = _safe_dispatch(retry_prompt, "any", task_id + "_r", agents,
                                             project, lineup, restrict, phase="planning",
                                             no_tools=True)
        raw2 = disp_result2.executor_result.raw_output if disp_result2 else ""
        if err2:
            raw2 += f'\n[LLM错误: {err2}]'
        if raw2:
            arch = try_parse_json(raw2, try_repair=True)
        disp_result = disp_result2  # lineage 用重试结果

    project.architecture = arch
    # ponytail: 保存阶段产出文件供后续阶段复用
    _save_phase_output(project.id, "architecture.md", raw)
    # Step 2: 多模型碰撞 → 保存各模型原始输出。
    # 直接从本次 dispatch 的结果取（_dispatch_committee 挂在 executor_result 上）——
    # 曾走 QIDIAN_DIR/.last_fusion.json 这个全局单文件，并发下会串项目，见那边的注释。
    # 非委员会路径没有这个属性 → fm 为 None，跳过。
    # isinstance 不能省：getattr 的默认值只在**属性不存在**时生效，任何带该属性的
    # 对象（MagicMock 就会凭空生成一个）都会溜进来，后面 fm.get 一用就炸。
    import json as _json
    fm = getattr(getattr(disp_result, "executor_result", None), "fusion_meta", None)
    if isinstance(fm, dict) and fm:
        try:
            project.committee_fusion = {
                "models": fm.get("models", []),
                "count": fm.get("count", 0),
                "fused": fm.get("fused", ""),
                "outputs": [o[:3000] for o in fm.get("outputs", [])],
            }
            _save_phase_output(project.id, "fusion-models.md",
                "\n\n---\n".join(f"## 模型: {fm['models'][i]}\n\n{fm['outputs'][i][:3000]}" for i in range(len(fm['models']))))
            _save_phase_output(project.id, "fusion-meta.json",
                _json.dumps({"models": fm["models"], "count": fm["count"]}, ensure_ascii=False))
        except Exception as e:
            # 不能静默：落盘失败 → ProjectState 上和各阶段产出文件里都没有各模型产物，
            # 前端「融合」页空白、后续阶段看不到委员会的中间结果，且查不出为什么。
            from singularity.scheduler import witness
            witness.warn("workflow", f"save_committee_fusion:{type(e).__name__}:{e}"[:200])
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

    # 校验结果**落盘**。原来只进 lineage 的计数 + 一条返回文案（SSE 一闪而过）——
    # 于是"架构缺必填字段"这件事在项目状态里查不到、GATE2 面板上也看不见，
    # 放行后流到执行层才以"拆不出任务、项目无声卡住"的形式爆出来。
    #
    # **分两档，判据是"下一步还能不能干"，不是"字段全不全"**：
    #   致命的（拦）：tasks 缺失/为空 —— 拆不出任务，执行层必然卡死
    #   非致命（只记）：data_model / tech_stack / constraints 等 ——
    #     一个单文件 CLI 本来就没有 data_model，按"六字段齐全"拦会把好活挡在门外
    project.issues = [i for i in project.issues
                      if i.get("type") not in ("arch_invalid", "arch_warning")]
    fatal = [i for i in blockers if "tasks" in i]
    if blockers:
        kind = "arch_invalid" if fatal else "arch_warning"
        project.issues.append({"type": kind,
                               "detail": f"架构校验{'未通过' if fatal else '有缺项'}"
                                         f"（{len(blockers)} 项）：" + "；".join(blockers[:5])})
    if fatal:
        try:
            from singularity.scheduler import witness
            witness.warn("planning", f"arch_invalid:{project.id}:{fatal[0]}"[:200])
        except Exception:
            pass

    project.add_lineage({"action": "planning_complete",
                         "agent": disp_result.agent_cfg.get("model","?") if disp_result else "?",
                         "task_count": len(arch.get("tasks", [])),
                         "traceability_items": len(traceability),
                         "validation_issues": len(arch_issues),
                         "blockers": len(blockers)})
    # ── 「信任上限」那个数：约束里几条是**真能机器跑**的（argv 结构），几条是散文 ──
    # 分母是约束条数（架构师自己列的）→ 严格说这是"自洽率"，不是"对需求的覆盖率"；
    # 后者要拿需求侧 scope_clarification.core 当分母（docs/信任上限-可测化-20260912.md §二）。
    # 这一步先把**能测的那半**落下来：至少"架构师承诺了机器可查、实际一条跑不了"能被看见。
    try:
        from singularity.scheduler import _machine_checks as mchk
        _runnable, _total = mchk.coverage(arch.get("constraints") or [])
        project.add_lineage({"action": "check_coverage",
                             "runnable": _runnable, "total": _total})
        if _total and not _runnable:
            project.issues.append({
                "type": "check_not_machine_runnable",
                "detail": (f"架构产出 {_total} 条约束，**没有一条**是机器可跑的"
                           f"（全是散文）→ 信任上限这一轮等于 0，验收只能靠人读")})

        # ── 「信任上限」**真正的那个数**：需求侧覆盖率 ──
        # 分母取调研报告的 scope_clarification.core（**用户侧**条目），不是架构师
        # 自己列的约束。拿约束当分母的话，他少列一条分母就跟着缩、比例纹丝不动
        # —— 那是"自洽率"。拿需求当分母，**漏掉的需求才会以低分暴露**。
        _reqs = (((project.research_report or {}).get("scope_clarification") or {})
                 .get("core") or [])
        _rc = mchk.requirement_coverage(arch.get("constraints") or [], _reqs)
        project.add_lineage({"action": "requirement_coverage",
                             "total": _rc["total"], "covered": _rc["covered"],
                             "hard_covered": _rc["hard_covered"],
                             "uncovered": _rc["uncovered"][:_MAX_UNCOVERED_LISTED]})
        project.issues = [i for i in project.issues if i.get("type") != "requirement_uncovered"]
        if _rc["total"] and _rc["uncovered"]:
            _shown = _rc["uncovered"][:_MAX_UNCOVERED_LISTED]
            project.issues.append({
                "type": "requirement_uncovered",
                "detail": (f"**{len(_rc['uncovered'])}/{_rc['total']} 条需求没有任何约束覆盖**"
                           f"（索引 {_shown}）→ 这几条没人验。"
                           f"其中被可机器跑的约束覆盖的只有 {_rc['hard_covered']} 条")})
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn("planning", f"check_coverage:{type(e).__name__}:{e}"[:120])

    _index_phase_memory(project, "architect", "planning", raw)

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

    project.set_phase(Phase.GATE2, "架构完成 → GATE2 等人工")
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

    # 分发前先确保项目独立 git 仓库存在 (否则任务 dispatch 时 snap.take 会卡住)
    from singularity.scheduler.project import ensure_repo
    ensure_repo(project.id)

    created = 0
    # 重新规划时必须先清空旧任务 id。GATE3 以 design 打回 → 回 PLANNING → 重跑 GATE2/EXECUTING，
    # 这一轮只会 append 新 task；旧 id 留着的后果是——下次 impl 打回时
    # workflow.handle_gate3_reject 会遍历整个 task_ids，把**上一版架构已废弃的 DONE 任务**
    # 一起重置成 PENDING 重跑，_collect_changed_files 也会把它们的残留产物算进交付报告。
    # 上面 `if not exec_tasks: return` 已保证这里不会是空批次清空。
    project.task_ids = []
    id_map = {}  # 本地任务 id (T1..Tn) → tracker task_id
    for idx, tdef in enumerate(exec_tasks):
        # 实现层角色：默认 implementer（可在 .qidian/phases.json 改）
        role_key = get_phase_role(Phase.EXECUTING) or "implementer"

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
    project.set_phase(Phase.EXECUTING, "架构确认 → 建任务进执行")
    save(project)
    return f"已分发 {created} 个子任务 (按 layer 路由到对应工程师, 全池选模型)"


# ═══════════════════════════════════════════════════════════
# 内循环: D审查 → 修复 → 再审查 → ... → GATE3
# ═══════════════════════════════════════════════════════════

