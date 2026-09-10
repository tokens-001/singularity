__all__ = ['_cmd_project', '_cmd_project_advance', '_cmd_project_create', '_cmd_project_delete', '_cmd_project_list', '_cmd_project_reject', '_cmd_project_show', '_phase_agent_level', '_phase_will_run']

"""CLI sub-commands."""
import json, os, sys, time
from pathlib import Path
from singularity.scheduler import config, tracker
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import orchestrator
from singularity.scheduler.project import Phase
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
    _spent, _unpriced = _project_today_cost(proj.id)
    print(f"  budget: ${proj.token_budget_total:.2f} / 今日已花: ${_spent:.4f}"
          + ("  (有模型未配置单价)" if _unpriced else ""))
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

    # ── 执行前确认 ──
    # 这里原来显示 `估算费用: ~$2.50` —— 来自一张写死的价目表，是编的。
    # 现在只报**真实**的今日已花费；未来要花多少不预测（各模型单价差几十倍，
    # 又不知道这次会落到哪个模型上，任何预估都是猜）。
    if _phase_will_run(phase, proj) and not yes:
        level = _phase_agent_level(phase)
        spent, has_unpriced = _project_today_cost(proj.id)
        print(f"[project] {proj.id[:8]}  即将进入 {phase.value} 阶段")
        print(f"  调用: {level} 层 agent")
        print(f"  本项目今日已花费: ${spent:.4f}"
              + ("  （有模型未配置单价，实际更高）" if has_unpriced else ""))
        print(f"  项目预算: ${proj.token_budget_total:.2f}")
        if spent > proj.token_budget_total:
            # 只陈述已发生的事实，不做"将超支"的预测（那是没有依据的）
            print(f"  ⚠ 今日花费已超过项目预算", file=sys.stderr)
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


def _phase_will_run(phase: Phase, proj) -> bool:
    """该 phase 这次是否真的会调 agent。

    取代原先的 `_phase_cost_estimate(phase, proj) > 0` —— 那张写死的价目表
    （调研 $0.02 / 架构 $2.50 / 审查 $1.00）是编的，但它顺带充当了"这个阶段要不要干活"
    的判断。这里保留判断、去掉假金额：调 agent 的层级不是 "-" 且产出尚不存在。
    """
    if phase == Phase.RESEARCHING and proj.research_report:
        return False   # 已有产出，不重复跑
    if phase == Phase.PLANNING and proj.architecture:
        return False
    if phase == Phase.REVIEWING and proj.issues:
        return False
    return _phase_agent_level(phase) != "-"


def _project_today_cost(project_id: str) -> tuple[float, bool]:
    """该项目**今日**的真实花费，以及是否存在未配置单价的模型。

    注意是"今日"不是"累计"：`proj.token_spent` 那个字段全仓无人赋值、恒为 0，
    真正的累计需要新的持久化，属另一个改动。这里只报有据可查的那个数。
    """
    from ._token_budget import get_usage_stats
    try:
        stats = get_usage_stats()
    except Exception:
        return 0.0, False
    cost = next((r.get("cost", 0.0) for r in stats.get("by_project", [])
                 if r.get("project_id") == project_id), 0.0)
    return cost, bool(stats.get("unpriced_models"))


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


def _cmd_project_delete(project_id: str) -> int:
    from .project import load as load_proj, delete as delete_proj
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1
    # 删关联任务及全部残留 (复用 task_delete: trace/worktree/snapshot/pending ref)
    from ._api_tasks import task_delete
    for tid in list(proj.task_ids):
        task_delete(tid)
    delete_proj(project_id)
    print(f"[project] 已删除: {proj.id[:8]} {proj.name}")
    return 0
