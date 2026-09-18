"""workflow.py — 项目工作流执行引擎。

3门 + D审查内循环:
  TEMPLATE → RESEARCHING → GATE1(用户审调研) → PLANNING → GATE2(用户审架构)
  → EXECUTING(拆任务→orchestrator执行) → [内循环:D审查→修复] → GATE3(用户最终审核)
  → DONE
"""

from __future__ import annotations

import json
import re

# `Path` 只出现在 `_phase_output_path`/`_save_phase_output` 的**返回注解**里。
# 文件有 `from __future__ import annotations`，注解运行时不求值 —— 所以它一直没炸，
# 但名字确实不在本模块作用域里（2026-09-13 被星号 import 盲区补丁抓出来的）。
from pathlib import Path

from singularity.scheduler import config, tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler._io import try_parse_json
from singularity.scheduler.project import (
    Phase,
    ProjectState,
    _projects_dir,
    resolve_flow,
    save,
)
from singularity.scheduler.project import (
    effective_constraints as _effective_constraints,  # §60 容错读法
)
from singularity.scheduler.tracker import TaskStatus

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

⚠️ **不需要改动任何文件的任务，`title` 必须以 `[只读]` 开头**（例：`[只读] 独立验收：
在干净检出上跑测试、逐条核对需求`）。这类"只跑不改"的活是**合法**的，但验收端有一条
硬规则是"零文件改动 = 什么都没产出"—— 不带这个标记，它会被**判死**
（2026-09-15 真机：一个"只跑不改"的验收任务真跑了 pytest、逐条核对了需求，
照样 `QA:fail: [completeness] 无文件改动`）。**没有这个前缀就别指望它豁免。**

⚠️ `depends_on` **必须认真填**（填同数组内其它任务的 `id`）：
   · **测试任务必须依赖它要测的实现任务** —— 不排先后的话两者会**同时开跑**，
     写测试的等不来实现就会**自己把实现写了**（真机案例 1789300044340：
     结果实现任务超时失败、测试任务连实现一起交付 ⇒ 测试与实现出自同一轮，
     检查全过也证明不了符合需求）；
   · **两个任务会改同一批文件时必须排先后** —— 同时跑会产物互相看不见、争同一批文件；
   · **排不出先后的，说明它们真的互不相干**，那才留空数组。
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
- complexity: 必填(low|medium|high), **只作记录** —— 两档已合并成单档,
  所有任务**同池选人**, 填 low 不会给你派更便宜的模型。别为了选模型而调它
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


def _phase_history_dir(project_id: str) -> Path:
    """阶段产出的历史版本目录。

    ⚠️ 放**子目录**而不是同级 `projects/{project_id}.{filename}.2`：同级是**平铺命名空间**，
    `list_all()` 每轮 `glob("*.json")` 扫它，靠 `_OUTPUT_SUFFIXES` 那个 `endswith` 白名单
    把阶段产出挡在外面 —— 而**那个白名单本来就不全**：`.qa_report.json`（`app.py` 写）
    和 `.executable_tasks.json`（`_workflow_phases` 写）也落在这个目录里，两个都不在名单上，
    现在每轮被读进来、再靠 `"phase" not in data` 丢掉。再往里塞历史版本，就是把这个
    已经补得不全的名单扩成 N 份。
    子目录 `glob("*.json")` 天然看不见；而且 `project.delete()` 会连 `projects/<id>/`
    整棵树一起删，不留"查不到归属的孤儿"（那条注释就写在 `delete()` 里）。
    """
    return _projects_dir() / project_id / "history"


def _archive_phase_output(project_id: str, filename: str, old_text: str) -> Path:
    """把**上一版**阶段产出存进 history/，返回落盘路径。"""
    d = _phase_history_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    n = 1
    for f in d.glob(f"{filename}.*"):
        # 用 isdigit 挡，不用 try/except：这里不需要异常路径，而静默 except 棘轮
        # 会把它记成一笔（`except: continue` 正是那类"出了事没人知道"的形状）。
        tail = f.name.rsplit(".", 1)[-1]
        if tail.isdigit():
            n = max(n, int(tail) + 1)
    p = d / f"{filename}.{n}"
    p.write_text(old_text, encoding="utf-8")
    return p


def _save_phase_output(project_id: str, filename: str, content: str) -> Path:
    """保存阶段产出到文件。**覆盖前先把上一版归档进 history/**。

    🔴 为什么要（2026-09-17 用户当场想看对比）：这里原来是裸 `write_text`，
    而"打回重做"的**全部意义就是对比改进** —— 覆盖掉等于每次都在盲点。
    实测那次 `architecture.md` 和项目 json 里的 `architecture` 同时被换掉，
    打回前后两版只剩一版。同族的 `research.md` 一样（`research-raw` 那个入口
    只解决"这一版看得到全文"，**不解决跨版本**）。

    ⚠️ **只在内容真的变了才归档**：重跑同一阶段常常逐字相同，不比就存 = 每次重跑
    多一份一模一样的副本。
    ⚠️ 归档的是**上一版**、不是当前版 —— 当前版永远在主路径上（`_read_phase_output` 读它）。
    ⚠️ 归档的是模型吐的**原文**（`architecture` 那个 dict 就是从它 `try_parse_json` 出来的），
    比解析后的 dict 更忠实。
    ponytail: 版本号一路往上加、不设上限（一个项目重规划几次也就几份、每份几十 KB）。
    """
    p = _phase_output_path(project_id, filename)
    old = None
    try:
        if p.exists():
            old = p.read_text(encoding="utf-8")
    except OSError as e:
        # 旧版读不出来就不归档 —— 但**新内容照写**：这里不是"损坏=没有"要拦的场景，
        # 为归档失败把这一版也丢掉才是真丢东西。
        from singularity.scheduler import witness
        witness.warn("workflow", f"phase_out_unreadable:{filename}:{type(e).__name__}"[:140])
    if old is not None and old != content:
        try:
            _archive_phase_output(project_id, filename, old)
        except Exception as e:      # noqa: BLE001 —— 归档塌了不该连累主路径，但不能不出声
            from singularity.scheduler import witness
            witness.warn("workflow", f"phase_archive_failed:{filename}:{type(e).__name__}:{e}"[:140],
                         key="phase_archive_failed")
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


def _flag_unordered_architecture(project: ProjectState, arch) -> None:
    """架构"多个任务、零依赖"时的**两件必做事**：出声 + 进 issues。

    抽出来是为了能被测到 —— 只测 `_arch_tasks_are_unordered` 验的是"**判据对**"，
    验不到"**判据为真时真的有人记**"（同 `_flag_missing_qa_verdict` 那条理由）。
    """
    n = len((arch or {}).get("tasks") or [])
    try:
        from singularity.scheduler import witness
        witness.warn("workflow", f"arch_no_dependency:{project.id}"[:120],
                     key="arch_no_dependency")
    except Exception:
        pass
    project.issues.append({
        "type": "arch_no_dependency",
        "detail": (f"架构拆了 **{n} 个任务，却一条依赖都没排** —— 它们会被**同时派发**。"
                   "确实互不相干就忽略；否则**请打回重规划**。"
                   "真机案例（1789300044340）：测试任务与实现任务同时跑 ⇒ "
                   "写测试的等不来实现、**自己把实现写了**，而实现任务超时失败、零提交 ⇒ "
                   "「实现和测试出自同一个 agent」⇒ 机械检查全过也证明不了符合需求"),
    })


def _arch_tasks_are_unordered(arch) -> bool:
    """架构里 **≥2 个任务、却一条依赖都没排** —— 可疑形状，返回 True。

    **为什么要有这条**：2026-09-13 真机（项目 `1789300044340`）—— 架构拆出
    "实现 `txtstat.py`" 和 "编写 `test_txtstat.py`" 两个任务，`depends_on` **都是空数组**
    ⇒ 调度循环把它们**同时派出去**。写测试的那个等不来实现，**就自己写了一个实现**。
    结局：实现任务 900s 超时失败、零提交；写测试那个的提交里**同时带着实现和测试**
    （`git log --all -- txtstat.py` 只有它那一笔）。

    ⇒ 后果不是"慢"，是**验证失去意义**：测试和实现出自同一个 agent 的同一轮，
    机械检查 9/9 全过只说明"**它跟自己一致**"，证明不了"**它符合需求**"。
    顺带也是"两个任务改同一批文件"的温床（`orchestrator` 那段注释自己点名过）。

    ⚠️ **只判"一条都没有"，不判"排错了"** —— 后者需要语义，判据会变成猜。
    也**不阻断**：GATE2 本来就是人审门，把可疑形状摆上去比卡死项目有用
    （同 `_gate3_admission` 的规矩）。确实互不相干的多任务架构会误报，所以文案里
    明说了"互不相干就忽略"。
    """
    tasks = (arch or {}).get("tasks") if isinstance(arch, dict) else None
    if not isinstance(tasks, list) or len(tasks) < 2:
        return False
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if t.get("depends_on") or t.get("depends_on_local_id"):
            return False
    return True


_QA_VERDICT_MISSING = "未产出"


def _qa_verdict_from_raw(qa_raw: str) -> tuple[dict, str, str]:
    """从 QA 的**原始输出**里取出 `(qa_data, verdict, reason)`。

    ⚠️ **这条不变量是本函数存在的全部理由**：
    **"QA 没产出结论" 和 "QA 说没问题" 必须分得开，前者绝不能变 `"go"`。**

    原来那两行是这么写的（在调用点内联）：

        qa_data = json.loads(qa_raw) if qa_raw.strip().startswith("{") else {}
        verdict = qa_data.get("verdict", "go" if not issues else "no_go")

    QA 吐出非 JSON（模型把输出写成了工具调用）时 `qa_data` 是 `{}`、`issues` 也是 `[]`
    ⇒ **默认值正好落到 `"go"`** ⇒ 空结论被当成放行。

    2026-09-13 真机实测（项目 `1789300044340`）：`qa_report.json` =
    `{total_checks: 0, passed: 0, failed: 0, verdict: "go"}` —— **一条检查没跑、结论"放行"**；
    同轮 `qa-report.md` 里存的是**一段没解析的 `<tool_call>` 原文**
    （模型想跑 pytest，输出成了工具调用）。而 GATE3 的准入标记照样写了"验收跑过"。

    抽成纯函数是为了能单测（同 `_committee_allowed` 那条理由）——
    这个不变量只能靠**喂各种畸形输出**来验，走完整路径太重。

    ⚠️ **不改成硬拦**：GATE3 本来就是人审门，`_gate3_admission` 立的规矩是
    "把缺证据摆到台面上，比卡死项目有用"。所以这里给**三态**
    （`go` / `no_go` / `未产出`），由调用点负责让它出声、进 issues。
    """
    # ⚠️ 原来是 `json.loads(qa_raw) if str(qa_raw).strip().startswith("{") else {}`
    # —— **模型把 JSON 包在 ```json 代码块里时直接判"没解析出"**。
    # 2026-09-15 真机实测：QA 的报告是好的（7/7 约束 pass、8 个测试通过），
    # 就因为外面裹了代码块 ⇒ 判"未产出" ⇒ GATE3 的 QA 那一维**永远是瞎的**。
    # 换成同仓的 `validator._extract_json_obj`（取最外层 `{`…`}`，**本来就容忍代码块**）——
    # `qa_acceptance_review` 一直用的就是它，两边收敛到同一个提取器，别再各写一份。
    # 注：它解析不出时返回 None ⇒ 落到下面的"未产出"，**没有放松 fail-closed 那半边**。
    from singularity.scheduler.validator import _extract_json_obj
    qa_data = _extract_json_obj(str(qa_raw)) or {}
    if not isinstance(qa_data, dict):
        qa_data = {}

    verdict = str(qa_data.get("verdict") or "").strip()
    if verdict:
        return qa_data, verdict, qa_data.get("summary", qa_data.get("verdict_reason", ""))

    # 没给 verdict。**分两种，别混**：
    #   · 报了 issues ⇒ 至少知道"有问题" ⇒ 按 fail-closed 判不通过（这是原来就有的分支，保留）
    #   · 一条 issues 都没有 ⇒ **什么都不知道** ⇒ 这才是那个漏洞，必须叫"未产出"
    if qa_data.get("issues"):
        return qa_data, "no_go", "QA 没给 verdict，但报了问题 ⇒ 按不通过处理"
    return qa_data, _QA_VERDICT_MISSING, "QA 没有产出可解析的结论（输出不是 JSON / 缺 verdict 字段）"


def _flag_missing_qa_verdict(project: ProjectState) -> None:
    """QA 没产出结论时的**两件必做事**：出声 + 进 issues。

    单独抽出来是为了能被测到 —— 只测 `_qa_verdict_from_raw` 的话，
    验的是"**判据对**"，验不到"**判据为真时真的有人记**"（这两件事分开，
    2026-09-13 那天被咬过三次，见 `docs/防御模式.md` §65）。

    - **出声**：进 `alerts.jsonl`（带 key，能被 `alert_summary` 聚合）
    - **进 issues**：GATE3 的人审页读的就是 `project.issues`
    """
    from singularity.scheduler import witness
    witness.warn("workflow", f"qa_verdict_missing:{project.id}"[:120],
                 key="qa_verdict_missing")
    project.issues.append({
        "type": "qa_verdict_missing",
        "detail": "验收里 **QA 那一维没有产出结论** —— 本页的『通过』不覆盖 QA 维度",
    })


def _run_verification(project: ProjectState, agents: dict) -> list[str]:
    """Step 5: QA工程师 + 安全审计师并行出验收报告。

    不调 LLM 写代码，只出验证报告供人工 GATE3 判断。
    """
    # **带兜底地读** —— 见 `ProjectState.effective_constraints()`：真机上 `_run_execution`
    # 明明赋了值又 save 了，落到盘里却是空的（覆盖源未定位），而架构里那份是好的。
    # 直接读 `project.constraints_checklist` 就会在这儿早退 ⇒ 机械检查一条都跑不了。
    constraints = _effective_constraints(project)
    if not constraints:
        # ── 探针（临时，定案后删）：防御模式 §60 ──────────────────────
        # 走到这儿说明**架构里也真没有约束**（兜底都没捞着），不是被覆盖。
        # 写入点（_workflow_phases._run_execution）那条记了 on_disk 的值，两边对不上
        # 就说明是中途被别的副本覆盖了 —— 兜底会把那种情况捞走，所以这里记的是"真没有"。
        project.add_lineage({
            "action": "probe_constraints_checklist", "at": "read",
            "in_memory": 0,
            "arch_constraints": len((project.architecture or {}).get("constraints") or []),
        })
        save(project)
        # 记进 issues: 只返回字符串的话，这句话不落盘, GATE3 只能看到一个空 issues
        # 和一份不存在的 QA 报告, 谁也说不清验收为什么没跑。
        # 这条也是进 GATE3 的**入门票**之一（见 ProjectState._gate3_admission）：
        # 它代表"验收有过结论"，结论就是"没跑"。
        reason = "验收跳过: 架构没产出约束清单, QA/安全审计师都没跑"
        project.issues.append({"type": "verification_skipped", "detail": reason})
        return [reason]

    msgs = []
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
        runnable = [c for c in (constraints or [])
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
    # ⚠️ **提示词必须给出 schema**（2026-09-15 真机）：原来只有一句"输出 JSON"，
    # 没写字段名 —— 而下游 `_qa_verdict_from_raw` 要的是 `verdict` / `issues` / `passed`。
    # **契约根本没建立**：模型按自己的理解给了 `{"summary": "…", "constraints":[…]}`，
    # 于是报告写得再好，汇总也永远判"未产出"，GATE3 的 QA 那一维永远是瞎的。
    # 对照组：同仓 `validator.qa_acceptance_review` 的提示词**是给了完整 schema 的**。
    qa_prompt = (
        f"你是 QA 工程师。做验收验证，不写代码，只出报告。\n\n{ctx}\n\n"
        "逐条检查约束是否满足，给出 evidence。\n\n"
        "只输出一个 JSON 对象（**不要包在 markdown 代码块里**），字段固定为：\n"
        '{"verdict": "go 或 no_go",\n'
        ' "passed": [{"id": "约束标识", "desc": "约束内容", "evidence": "满足的证据"}],\n'
        ' "issues": [{"id": "约束标识", "severity": "critical|warning",\n'
        '             "detail": "差在哪", "suggested_fix": "怎么修",\n'
        '             "fix_route": "impl|design|note"}],\n'
        ' "summary": "一句话结论"}\n'
        "**一致性要求**：verdict 判 no_go 时 issues 里**必须至少有一条**，"
        "写清是哪条约束、差在哪 —— 判了 no_go 却给不出具体条目，下游只知道"
        "\"要修\"却不知道修什么，只会原样再来一遍。\n"
        # ⚠️ `fix_route` 这个字段**必须给**（2026-09-15 真机补上）：原来提示词里没有它，
        # 下游却拿它决定"人工打回时退到哪一层" —— 缺了就只能由 severity 去猜，
        # 而 severity 表达的是"这条验收过没过"，**不是**"要退到哪一层"。
        # 真机后果：一个"实现全对、只差补测试文件"的项目被判 design，
        # 打回时**清空架构重新规划**。字段补上之后，读的那一端才有依据。
        "`fix_route` 怎么填：**改代码/补文件就能修 → impl**；"
        "**非改架构或重新规划不可 → design**；只是提示不必修 → note。\n"
        "⚠️ **拿不准就填 `impl`** —— `design` 会让人工打回时**清空架构、从头重新规划**，"
        "是代价最大的一条路，别为保险起见全填它。"
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
    # ⚠️ `config.PROJECT_ROOT` 是**奇点自己的仓库**（= 上面 orchestrator 里
    # 已经修过的那处同一个错）：于是这一支永远走不到 —— 奇点根目录下压根没有
    # `test_cases.json` ⇒ `e2e_checklist.json` **从来没被写出来过**，
    # GATE3 上那份 E2E 清单永远空，而界面上跟"没有 E2E 用例"长得一样。
    # 和集成检查（orchestrator._run_delivery 上面的 `_Path(root)`）对齐：读项目仓库。
    from singularity.scheduler import project as _proj_mod
    tc_path = _proj_mod.repo_dir(project.id) / "test_cases.json"
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
    # ⚠️ 只有报告**真落盘**了才算数，见下面那段标记的说明。
    qa_saved = False
    try:
        from singularity.scheduler.validator import build_qa_report
        qa_raw = disp_result.executor_result.raw_output if disp_result and disp_result.executor_result else "{}"
        # ⚠️ 这里 2026-09-15 曾放一条**临时探针**（`qa_raw_probe:saved=N:parsed=M`），
        # 用来定位「喂进去的和落在盘上的不是同一份（差 217 字符）」。
        # **2026-09-16 真机响过一次，已按"定位完就删"删掉** —— 结论：
        # `saved == parsed`（1819 = 1819）且解析出了真结论 ⇒ **不是"中途被换"、
        # 也不是"跑了两趟"（只打了一行）**。那个谜没在这一轮复现（它的定性本来就是"间歇性"）。
        # 也就是说：**病灶不在这层**。要再查得从别处下手。
        qa_data, verdict, reason = _qa_verdict_from_raw(qa_raw)
        issues = qa_data.get("issues", [])
        passed = qa_data.get("passed", [])
        if verdict == _QA_VERDICT_MISSING:
            # 必须出声、必须进 issues —— 否则"QA 没产出"在人审页上跟"QA 说没问题"长得一样
            _flag_missing_qa_verdict(project)
        qa_report = build_qa_report(passed, issues, verdict, reason)
        _save_phase_output(project.id, "qa_report.json",
                          json.dumps(qa_report, ensure_ascii=False, indent=2))
        qa_saved = True
    except Exception as e:
        # 原来是裸 `pass` —— 报告没落盘、GATE3 却照样看到"验收跑过"。
        from singularity.scheduler import witness
        witness.warn("workflow", f"qa_report_save_failed:{type(e).__name__}:{e}"[:120])

    # 本轮有没有任务是在"上游失败、降级运行"下跑完的 —— 必须在人审页上看得见
    _flag_degraded_tasks(project)
    # 有没有任务越界改了**兄弟任务的产出文件** —— 同样必须在人审页上看得见
    _flag_file_overlap(project)

    # 验收入门票：走到这儿才算"验收真的跑过"。放在**最后**、而不是开头 ——
    # 中途抛异常时不该留下"跑过了"的假证据。GATE3 靠这个标记识别
    # "验收整段没跑就被推进来了"（见 ProjectState._gate3_admission）。
    # ⚠️ **撒标记要跟产出对账**（2026-09-14，D/E 两轮外派独立撞上同一条）：
    # 原先是无条件 append —— 上面那条 `except` 一吞，QA 报告根本没落盘，
    # 标记却照样撒 ⇒ `_gate3_admission` 看到的是"验收跑过"，进 GATE3 零告警。
    # 标记的语义是"验收有产出"，不是"代码走到这一行"。没产出就让 admission 去报缺证据。
    if qa_saved:
        project.issues.append({"type": "verification_ran", "detail": "QA + 安全审计已执行"})
    else:
        from singularity.scheduler import witness
        witness.warn("workflow", f"verification_ran_withheld:{project.id}"[:120])
    return msgs


# 描述里"像个文件名"的 token：带点 + 扩展名，且**扩展名以字母开头**。
# ⚠️ 扩展名必须字母开头 —— 不然 `0.55` 这种小数会被当成"文件名 .55"，
# 而架构描述里到处都是小数（置信度、阈值）。
_FILE_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z][A-Za-z0-9]{0,5}\b")


def _files_named_in(text: str) -> set[str]:
    """一段文本里点名的文件名（**只取 basename** —— 路径前缀不该影响判断）。"""
    return {m.group(0).rsplit("/", 1)[-1] for m in _FILE_TOKEN_RE.finditer(text or "")}


def _changed_files_of(tid: str) -> set[str]:
    """读这个任务的 trace，取它改过的文件（basename）。读不到就是空集。"""
    from . import neijinglu
    try:
        d = json.loads(neijinglu.config_trace_path(tid).read_text(encoding="utf-8"))
        return {str(f).rsplit("/", 1)[-1] for f in (d.get("changed_files") or [])}
    except Exception:
        return set()


def _flag_file_overlap(project: ProjectState) -> None:
    """任务改了**只有兄弟任务点名、自己没点名**的文件 → 进 issues + 出声。

    ⚠️ **为什么要有这条**（2026-09-13 轮 5 真机）：实现任务**顺手把测试也写了**
    （`changed_files = ['txtstat.py', 'test_txtstat.py']`），于是**写测试的那个任务空手**
    —— 零文件改动 → `QA:fail: [completeness] 无文件改动` → 项目 `all_tasks_failed`、卡在 GATE2。
    **门禁判得对，但人审页上看不出"它其实是被兄弟任务抢了活"。**

    判据只认**确定的那一种**（低误报）：
    - 这个文件**在兄弟任务的描述里被点名**（= 那本来是它的产出），**且**
    - **在本任务自己的描述里没被点名**（= 不是它自己的活）。
    两条都满足才算越界。**自己描述里点过的文件，改多少都不算** —— 那正是它的活。

    ⚠️ **不改变行为**（不改状态、不拦合并），只让它在人审页上**看得见** ——
    同 `_flag_degraded_tasks` 立的规矩。**提示词那条是"防"，这条是"报"** ——
    防不住的（模型不听）至少报得出来。

    ⚠️ **本函数里那几处 `except Exception` 有意静默**（读盘/读 trace/告警自己）：
    它们是"尽力而为"的，失败不该把**整段验收**带崩 —— 与兄弟 `_flag_degraded_tasks`
    同一取舍。所以它们进了 `silent_except` 守卫的基线（2026-09-13）。
    """
    try:
        from singularity.scheduler import tracker, witness
    except Exception:
        return
    tasks = []
    for tid in (project.task_ids or []):
        try:
            t = tracker.read_task(tid)
        except Exception:
            continue
        if t is not None:
            tasks.append((tid, t))
    mine = {tid: _files_named_in(getattr(t, "description", "")) for tid, t in tasks}
    overlaps = []
    for tid, t in tasks:
        changed = _changed_files_of(tid)
        if not changed:
            continue
        for other_tid, _ in tasks:
            if other_tid == tid:
                continue
            stolen = (changed & mine[other_tid]) - mine[tid]
            if stolen:
                overlaps.append((tid, other_tid, sorted(stolen)))
    if not overlaps:
        return

    try:
        witness.warn("workflow",
                     f"task_file_overlap:{project.id}:{len(overlaps)}"[:120],
                     key="task_file_overlap")
    except Exception:
        pass
    detail = "；".join(
        f"{tracker.short_id(a)} 改了本属 {tracker.short_id(b)} 的文件 {'、'.join(f)}"
        for a, b, f in overlaps)
    project.issues.append({
        "type": "task_file_overlap",
        "detail": (f"**任务越界改了别人的产出文件**（{detail}）—— "
                   "被抢活的那个任务会因为『零文件改动』被判失败，"
                   "而本页的『通过』看不出这件事。"),
    })


def _flag_degraded_tasks(project: ProjectState) -> None:
    """扫一遍本项目任务，把"**上游失败、降级运行**"的挑出来 → 进 issues + 出声。

    ⚠️ **为什么要有这条**（2026-09-13 真机 · 项目 `1789303369052`）：
    `tracker.ready_tasks` 有意**不级联失败** —— 上游挂了下游照跑，只在
    `task.error` 里留一句"上游依赖 X 已失败 (降级运行)"。**那个决定是合理的**
    （让返工循环修，别一挂全挂）。

    但它有个**没被兜住的副作用**：**下游可能把上游的活自己干了**。
    实测那一轮：实现任务 **900s 超时失败**，写测试的任务于是降级起跑 ——
    而**上一轮它就是这么自己把实现写了的**（`git log --all` 只有写测试那笔提交）。

    ⚠️ **而"这轮降级过"这件事，当时没有任何出口**：这句 error **只写在 task 字段里**，
    验收路径不读、`project.issues` 是空的、GATE3 人审页上**一个字都看不到** ——
    人看到的是"机械检查 9/9 全过"，不知道底下有个失败的实现任务。

    ⇒ **不改变行为**（不级联失败是设计），只让它在人审时**看得见** ——
    同 `_gate3_admission` 立的规矩：把缺证据摆到台面上，比卡死项目有用。
    """
    try:
        from singularity.scheduler import tracker, witness
    except Exception:
        return
    degraded = []
    for tid in (project.task_ids or []):
        try:
            t = tracker.read_task(tid)
        except Exception:
            continue
        if t is not None and "降级运行" in str(getattr(t, "error", "") or ""):
            degraded.append(tid)
    if not degraded:
        return
    try:
        witness.warn("workflow", f"degraded_dependency:{project.id}:{len(degraded)}"[:120],
                     key="degraded_dependency")
    except Exception:
        pass
    project.issues.append({
        "type": "degraded_dependency",
        "detail": (f"本轮有 **{len(degraded)} 个任务是在『上游失败』的前提下跑的**"
                   f"（{'、'.join(tracker.short_id(t) for t in degraded)}）—— "
                   "它们**拿不到上游的产物**，可能自己把上游的活干了。"
                   "本页的『通过』不覆盖这个前提"),
    })


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
        from singularity.scheduler._observer_definition import VERDICT_FIX_ROUTES
        from singularity.scheduler.project import get_project_dir
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
        msg = "GATE3 问题仅记录 (suggestion), 不阻断交付"
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
