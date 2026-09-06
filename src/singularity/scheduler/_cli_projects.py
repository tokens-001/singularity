__all__ = ['_cmd_project', '_cmd_project_advance', '_cmd_project_create', '_cmd_project_list', '_cmd_project_reject', '_cmd_project_show', '_phase_agent_level', '_phase_cost_estimate']

"""CLI sub-commands."""
import json, os, sys, time
from pathlib import Path
from singularity.scheduler import config, tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import orchestrator
from singularity.scheduler.tracker import TaskStatus

def _cmd_project(argv: list) -> int:
    if not argv:
        print("用法: scheduler project create|list|show|advance|reject|delete [参数]", file=sys.stderr)
        return 2
    sub = argv[0]
    args = argv[1:]

    if sub == "create" and len(args) >= 1:
        return _cmd_project_create(args)
    if sub == "list":
        return _cmd_project_list()
    if sub == "show" and len(args) >= 1:
        return _cmd_project_show(args[0])
    if sub == "advance" and len(args) >= 1:
        approve = "--approve" in args
        yes = "--yes" in args or "-y" in args
        return _cmd_project_advance(args[0], approve=approve, yes=yes)
    if sub == "reject" and len(args) >= 1:
        return _cmd_project_reject(args[0])
    if sub == "delete" and len(args) >= 1:
        return _cmd_project_delete(args[0])

    print(f"未知 project 子命令: {sub}", file=sys.stderr)
    return 2


def _cmd_project_create(args: list) -> int:
    from .project import create, TEMPLATES
    name = args[0]
    template = "product_dev"
    budget = 5.0
    auto_mode = False
    i = 1
    while i < len(args):
        if args[i] == "--template" and i + 1 < len(args):
            t = args[i + 1]
            if t in TEMPLATES:
                template = t
            else:
                print(f"未知模板: {t}, 可用: {list(TEMPLATES.keys())}", file=sys.stderr)
                return 1
            i += 2
        elif args[i] == "--budget" and i + 1 < len(args):
            try:
                budget = float(args[i + 1])
            except ValueError:
                print(f"无效预算: {args[i+1]}", file=sys.stderr)
                return 1
            i += 2
        elif args[i] in ("--auto",):
            auto_mode = True
            i += 1
        else:
            print(f"未知参数: {args[i]}", file=sys.stderr)
            return 1

    try:
        proj = create(name=name, template=template, budget=budget, auto_mode=auto_mode)
    except ValueError as e:
        print(f"创建失败: {e}", file=sys.stderr)
        return 1
    tmpl = TEMPLATES.get(template, {})
    print(f"[project] 创建: {proj.id[:8]}  {proj.name}")
    print(f"  template: {template} ({tmpl.get('name','')})")
    print(f"  phase: {proj.phase.value}")
    print(f"  auto: {auto_mode}")
    print(f"  budget: ${budget:.2f}")
    print(f"  id: {proj.id}")
    return 0


def _cmd_project_list() -> int:
    from .project import list_all
    projects = list_all()
    if not projects:
        print("无项目")
        return 0
    print(f"[project] 项目列表 ({len(projects)}):")
    print(f"  {'ID':<10} {'NAME':<20} {'PHASE':<14} {'TASKS':<6} {'UPDATED'}")
    for p in projects:
        ts = time.strftime("%m-%d %H:%M", time.localtime(p.updated_at)) if p.updated_at else "-"
        print(f"  {p.id[:8]:<10} {p.name[:20]:<20} {p.phase.value:<14} {len(p.task_ids):<6} {ts}")
    return 0


def _cmd_project_show(project_id: str) -> int:
    from .project import load as load_proj
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1
    print(f"[project] {proj.id[:8]}  {proj.name}")
    print(f"  phase: {proj.phase.value}")
    print(f"  template: {proj.template}")
    print(f"  auto: {proj.auto_mode}")
    print(f"  budget: ${proj.token_budget_total:.2f} / spent: ${proj.token_spent:.2f}")
    print(f"  description: {proj.description[:120]}")
    print(f"  scope: {proj.scope[:120]}")
    print(f"  constraints: {proj.raw_constraints}")
    print(f"  tasks: {len(proj.task_ids)} 个关联任务")
    if proj.research_report:
        rr = proj.research_report
        refs = rr.get("references", [])
        print(f"  调研: {len(refs)} 条引用, 推荐: {rr.get('recommendation','N/A')[:100]}")
        if refs:
            for ref in refs[:3]:
                print(f"    - {ref.get('name','?')}: {ref.get('core_idea','')[:80]}")
    if proj.architecture:
        arch = proj.architecture
        tasks = arch.get("tasks", [])
        cons = arch.get("constraints", [])
        print(f"  架构: {len(tasks)} 任务, {len(cons)} 约束")
        print(f"  设计: {arch.get('architecture','')[:120]}")
    if proj.issues:
        print(f"  issues: {len(proj.issues)} 个问题")
    if proj.agent_lineup:
        print(f"  lineup: {proj.agent_lineup}")
    print(f"  created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(proj.created_at))}")
    print(f"  updated: {time.strftime('%Y-%m-%d %H:%M', time.localtime(proj.updated_at))}")
    return 0


def _cmd_project_advance(project_id: str, approve: bool = False, yes: bool = False) -> int:
    from .project import load as load_proj, save as save_proj
    from .workflow import start_project_workflow, run_phase
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1

    phase = proj.phase
    if phase.value.startswith("gate"):
        if approve:
            proj.confirm_gate(phase, "approved")
            save_proj(proj)
            print(f"[project] {proj.id[:8]}  {phase.value} APPROVED → {proj.phase.value}")
            return 0
        else:
            print(f"[project] {proj.id[:8]}  当前在 {phase.value}，需 --approve 确认或 --reject 打回",
                  file=sys.stderr)
            return 1

    # ── 费用估算 & 确认 ──
    cost = _phase_cost_estimate(phase, proj)
    if cost > 0 and not yes:
        level = _phase_agent_level(phase)
        print(f"[project] {proj.id[:8]}  即将进入 {phase.value} 阶段")
        print(f"  调用: {level} 层 agent")
        print(f"  估算费用: ~${cost:.2f}  (累计已花费: ${proj.token_spent:.2f})")
        print(f"  预算剩余: ${proj.token_budget_total - proj.token_spent:.2f}")
        if proj.token_spent + cost > proj.token_budget_total:
            print(f"  ⚠ 预算将超支!", file=sys.stderr)
        print(f"\n  确认执行? 加上 --yes 跳过此提示")
        return 1

    agents = disp_mod.load_agents()
    if phase == Phase.TEMPLATE:
        msg = start_project_workflow(proj, agents)
        print(f"[project] {proj.id[:8]}  {msg}")
    else:
        msg = run_phase(proj, agents)
        print(f"[project] {proj.id[:8]}  {phase.value} → {proj.phase.value}")
        print(f"  {msg}")
    return 0


def _phase_agent_level(phase: Phase) -> str:
    """返回 phase 调用的 agent 层级。"""
    return {
        Phase.RESEARCHING: "any",
        Phase.PLANNING: "any",
        Phase.REVIEWING: "any",
    }.get(phase, "-")


def _phase_cost_estimate(phase: Phase, proj) -> float:
    """估算 phase 的费用 ($)。返回 0 表示免费。"""
    rates = {
        Phase.RESEARCHING: 0.02,   # 廉价层 DeepSeek/GLM 廉价
        Phase.PLANNING: 2.50,      # 强力层 Opus/GPT 架构
        Phase.REVIEWING: 1.00,     # 强力层审查(需推理能力出优化方案)
    }
    # 如果已有产出，跳过不重复收费
    if phase == Phase.RESEARCHING and proj.research_report:
        return 0
    if phase == Phase.PLANNING and proj.architecture:
        return 0
    if phase == Phase.REVIEWING and proj.issues:
        return 0
    return rates.get(phase, 0)


def _cmd_project_reject(project_id: str) -> int:
    from .project import load as load_proj, save as save_proj
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1
    phase = proj.phase
    if not phase.value.startswith("gate"):
        print(f"[project] {proj.id[:8]}  当前 {phase.value} 不是 gate 阶段，无需打回", file=sys.stderr)
        return 1
    proj.confirm_gate(phase, "rejected")
    save_proj(proj)
    print(f"[project] {proj.id[:8]}  {phase.value} REJECTED → {proj.phase.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
