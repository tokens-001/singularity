"""__main__.py — CLI 入口

子命令:
  scheduler add "任务"     入队即返回 (不等执行)
  scheduler run "任务"     入队 + 立即跑 (等价于 add + loop 一轮)
  scheduler loop           常驻循环: 取队→执行→取队→执行... (Ctrl+C 停)
  scheduler rollback ID    手动回滚到某 snapshot (审计 4.4)
  scheduler apply ID       手动 apply E+/Planner patch (审计 6.5)
  scheduler status         查看队列状态 + 各层负载
"""

from __future__ import annotations
import json
import sys
import time
import signal

from . import config
from . import dispatcher as disp_mod
from . import snapshot as snap_mod
from . import orchestrator
from . import tracker
from .tracker import TaskStatus
def _cmd_project_delete(project_id: str) -> int:
    from .project import load as load_proj
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1
    # 删除关联的任务文件
    from . import tracker as _tk
    for tid in proj.task_ids:
        p = _tk._path(tid)
        if p.exists():
            p.unlink()
    # 删除项目文件
    from .project import _path as _proj_path
    _proj_path(project_id).unlink()
    print(f"[project] 已删除: {proj.id[:8]} {proj.name}")
    return 0

from .project import Phase

_LOOP_POLL_SECS = 3  # 队列空时的轮询间隔


def main(argv: list) -> int:
    if not argv:
        print(
            "用法: python3 -m scheduler add|run|loop|rollback|apply|status|merge|memory [参数]",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]

    if cmd == "add" and len(argv) >= 2:
        return _cmd_add(" ".join(argv[1:]))
    if cmd == "run" and len(argv) >= 2:
        # 修复 #4: run 支持 --concurrent N
        rest, concurrent = _parse_concurrent(argv[1:])
        return _cmd_run(" ".join(rest), max_concurrent=concurrent)
    if cmd == "loop":
        # 修复 #4: loop 支持 --concurrent N
        rest, concurrent = _parse_concurrent(argv[1:])
        return _cmd_loop(max_concurrent=concurrent)
    if cmd == "rollback" and len(argv) >= 2:
        return _cmd_rollback(argv[1])
    if cmd == "apply" and len(argv) >= 2:
        return _cmd_apply(argv[1])
    if cmd == "status":
        return _cmd_status()
    if cmd == "merge" and len(argv) >= 2:
        # 修复 #10: merge list / merge resolve <id> --manual|--abort
        return _cmd_merge(argv[1:])
    if cmd == "memory":
        return _cmd_memory(argv[1:])
    if cmd == "project" and len(argv) >= 2:
        return _cmd_project(argv[1:])

    # 兼容旧用法: 直接跟任务文本
    rest, concurrent = _parse_concurrent(argv)
    task = " ".join(rest)
    return _cmd_run(task, max_concurrent=concurrent)


def _parse_concurrent(args: list) -> tuple:
    """从 argv 提取 --concurrent N (修复 #4)。返回 (剩余args, concurrent)。"""
    rest = []
    concurrent = 1
    i = 0
    while i < len(args):
        if args[i] == "--concurrent" and i + 1 < len(args):
            try:
                concurrent = int(args[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        rest.append(args[i])
        i += 1
    return rest, concurrent


def _cmd_status() -> int:
    from . import witness
    config.ensure_dirs()
    agents = disp_mod.load_agents()
    print(witness.status(agents))
    return 0


# ── 任务提交 ──────────────────────────────────────────────────────────
def _cmd_add(task: str) -> int:
    """入队即返回, 不等待执行。loop 模式会取走。"""
    config.ensure_dirs()
    t = tracker.create(task, priority=0)
    print(f"[tracker] 入队: {t.id}  →  \"{task[:60]}{'...' if len(task)>60 else ''}\"", file=sys.stderr)
    # 直接输出 task id 到 stdout, 方便脚本抓取
    print(t.id)
    return 0


def _cmd_run(task: str, max_concurrent: int = 1) -> int:
    """入队 + 立即跑队列 (单次, 跑完退出)。修复 #4: 支持 --concurrent。"""
    config.ensure_dirs()
    agents = disp_mod.load_agents()

    t = tracker.create(task, priority=0)
    print(f"[tracker] 入队: {t.id}", file=sys.stderr)

    exit_code, _ = _drain_queue(agents, max_concurrent=max_concurrent)
    return exit_code


def _cmd_loop(max_concurrent: int = 1) -> int:
    """常驻循环: 持续取队→执行, 队列空时轮询等待。Ctrl+C 优雅退出。修复 #4。"""
    config.ensure_dirs()
    agents = disp_mod.load_agents()

    # 启动时先恢复崩溃残留的 inflight 任务
    recovered = tracker.recover()
    if recovered:
        print(f"[loop] 恢复 {recovered} 个中断任务→PENDING", file=sys.stderr)

    running = True

    def _on_sigint(signum, frame):
        nonlocal running
        print("\n[loop] 收到 SIGINT, 等当前任务跑完退出...", file=sys.stderr)
        running = False

    signal.signal(signal.SIGINT, _on_sigint)

    print(f"[loop] 常驻循环启动 (concurrent={max_concurrent}), Ctrl+C 退出", file=sys.stderr)
    idle_ticks = 0
    while running:
        # 不只用 list_pending() (只扫 PENDING) 做 gate —— v3 路径下 BLOCKED
        # 任务等依赖满足后会由 ready_tasks() → ROUTED, 但 list_pending() 看不见。
        # 改为无条件调 _drain_queue, 让其内部 run_queue 的 while 循环自行判空。
        # 返回的 count==0 才表示本轮无活可干。
        exit_code, count = _drain_queue(agents, max_concurrent=max_concurrent)
        if count == 0:
            idle_ticks += 1
            if idle_ticks == 1:
                print(f"[loop] 队列空, 等待中 (每 {_LOOP_POLL_SECS}s 检查)...", file=sys.stderr)
            time.sleep(_LOOP_POLL_SECS)
        else:
            idle_ticks = 0

    print("[loop] 退出", file=sys.stderr)
    return 0


def _drain_queue(agents: dict, max_concurrent: int = 1) -> tuple[int, int]:
    """处理队列中所有就绪任务, 返回 (退出码, 处理数)。"""
    results = orchestrator.run_queue(agents, max_concurrent=max_concurrent)

    # MAGMA 慢通道: 每轮 drain 后运行一次启发式整合
    if results:
        try:
            added = orchestrator.consolidate_memory()
            if added:
                print(f"[memory] 慢通道整合: +{added} 条隐含因果边", file=sys.stderr)
        except Exception as e:
            try:
                from . import witness
                witness.heartbeat("main", f"warn:consolidate_mem:{e}"[:80])
            except Exception:
                pass
            pass

    exit_code = 0
    for tid, reason, validation in results:
        t = tracker._read(tid)
        level = t.route_level if t else "?"
        icon = "✅" if validation.action == "pass" else "❌"
        print(f"  {icon} [{tid[:8]}] level={level} {reason}", file=sys.stderr)
        if validation.action not in ("pass",):
            exit_code = 1
    return exit_code, len(results)


# ── 子命令 ────────────────────────────────────────────────────────────
def _cmd_rollback(snap_id: str) -> int:
    """手动回滚 (审计 4.4: 回滚失败时的人工恢复通道)。"""
    config.ensure_dirs()
    meta_path = config.SNAPSHOT_DIR / f"{snap_id}.json"
    if not meta_path.exists():
        print(f"快照不存在: {snap_id}", file=sys.stderr)
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    snap = snap_mod.Snapshot(
        id=meta["id"], method=meta["method"],
        ref=meta["ref"], created_at=meta["created_at"],
    )
    if snap_mod.rollback(snap):
        print(f"已回滚到 {snap_id} ({snap.method})")
        return 0
    print(f"回滚失败, snapshot_id={snap_id}, method={snap.method}", file=sys.stderr)
    return 1


def _cmd_apply(task_id: str) -> int:
    """手动 apply E+ / Planner patch (审计 6.5: 显式落盘动作)。"""
    config.ensure_dirs()
    # 先试精确 id, 再试 _plan 后缀 (planner 产出)
    candidates = [
        config.PATCH_DIR / f"{task_id}.md",
        config.PATCH_DIR / f"{task_id}_plan.md",
    ]
    patch_path = None
    for p in candidates:
        if p.exists():
            patch_path = p
            break
    if patch_path is None:
        print(f"patch 不存在: {task_id}.md / {task_id}_plan.md", file=sys.stderr)
        return 1
    content = patch_path.read_text(encoding="utf-8")
    print(f"=== patch 内容 ({patch_path.name}) ===")
    print(content)
    print("=== 确认后手动应用到对应文件; scheduler 不自动覆盖 (审计 6.5) ===")
    return 0


# ── merge 子命令 (修复 #10: parking 冲突的 CLI 入口) ──────────────────
def _cmd_merge(args: list) -> int:
    """merge list | merge resolve <id> --manual|--abort。

    MergeQueue 是 run_queue 期间的瞬态对象, CLI 调用时已不存在。
    故: list 扫 tracker 的 CONFLICT_HELD; resolve 直接改 task 状态。
      --abort: task → FAILED (放弃这次产出)
      --manual: 人已在主工作区手动解冲突并 commit → task → DONE
    """
    config.ensure_dirs()
    sub = args[0] if args else ""

    if sub == "list":
        # 扫所有 CONFLICT_HELD 任务
        held = []
        for p in tracker._tasks_dir().glob("*.json"):
            try:
                import json as _json
                task = tracker.Task.from_dict(_json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                try:
                    from . import witness
                    witness.heartbeat("main", f"warn:task_scan:{e}"[:80])
                except Exception:
                    pass
                continue
            if task.status == TaskStatus.CONFLICT_HELD:
                held.append(task)
        if not held:
            print("无 parking 冲突任务")
            return 0
        print(f"parking 冲突任务 ({len(held)}):")
        for t in held:
            print(f"  [{t.id[:8]}] {t.description[:50]}  error={t.error[:60]}")
        return 0

    if sub == "resolve" and len(args) >= 2:
        task_id = args[1]
        strategy = "manual"
        for a in args[2:]:
            if a in ("--manual", "--abort"):
                strategy = a[2:]
        task = tracker._read(task_id)
        if task is None:
            print(f"任务不存在: {task_id}", file=sys.stderr)
            return 1
        if task.status != TaskStatus.CONFLICT_HELD:
            print(f"任务 {task_id} 非 CONFLICT_HELD (当前 {task.status.value})", file=sys.stderr)
            return 1
        if strategy == "abort":
            tracker.transition(task_id, TaskStatus.FAILED, error="merge 冲突, 人工放弃")
            _release_pending_ref(task_id)
            print(f"[{task_id[:8]}] 已放弃 → FAILED")
        else:
            # manual: 验证产出 ref 确已合入 main (重要 #5)
            if not _is_merged_to_main(task_id):
                print(
                    f"[{task_id[:8]}] 产出分支 refs/qidian/pending/{task_id} 未合入 main, "
                    "请先手动 merge 再 resolve",
                    file=sys.stderr,
                )
                return 1
            tracker.transition(task_id, TaskStatus.DONE, error="")
            _release_pending_ref(task_id)
            print(f"[{task_id[:8]}] 人工解决 → DONE")
            # 触发父任务聚合
            from . import orchestrator as _o
            _o._maybe_complete_parents(task_id)
        return 0

    print("用法: scheduler merge list | merge resolve <id> --manual|--abort", file=sys.stderr)
    return 2


def _release_pending_ref(task_id: str) -> None:
    """清理 git 锚定 ref (orchestrator._anchor_ref 的配对)。"""
    import subprocess as _sp
    ref = f"refs/qidian/pending/{task_id}"
    _sp.run(
        ["git", "update-ref", "-d", ref],
        cwd=str(config.PROJECT_ROOT), capture_output=True,
    )


def _is_merged_to_main(task_id: str) -> bool:
    """验证产出 commit 确已合入 main (重要 #5)。"""
    import subprocess as _sp
    ref = f"refs/qidian/pending/{task_id}"
    r = _sp.run(
        ["git", "merge-base", "--is-ancestor", ref, "main"],
        cwd=str(config.PROJECT_ROOT), capture_output=True,
    )
    return r.returncode == 0


def _cmd_memory(argv: list) -> int:
    """scheduler memory stats|rebuild|query|latent|traverse [参数]"""
    from . import memory as mem_mod

    if not argv:
        print("用法: scheduler memory stats|rebuild|query|latent|traverse [参数]",
              file=sys.stderr)
        return 2

    sub = argv[0]
    if sub == "stats":
        s = mem_mod.stats()
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    if sub == "rebuild":
        config.ensure_dirs()
        n = mem_mod.rebuild_from_traces()
        print(f"[memory] 从 traces 重建: {n} 条任务已索引")
        return 0

    if sub == "latent":
        candidates = mem_mod.find_candidate_latent_edges()
        print(f"慢通道候选: {len(candidates)} 对")
        for c in candidates[:10]:
            print(f"  {c['task_a'][-8:]} ↔ {c['task_b'][-8:]} "
                  f"共享:{c['shared_files']} sim={c['semantic_sim']} gap={c['time_gap_hours']}h")
        return 0

    if sub == "chain" and len(argv) >= 2:
        task_id = argv[1]
        direction = "up"
        if "--down" in argv:
            direction = "down"
        elif "--both" in argv:
            direction = "both"
        chain = mem_mod.find_causal_chain(task_id, direction=direction)
        print(f"因果链 ({direction}): {len(chain)} 个关联任务")
        for c in chain:
            indent = "  " * c["depth"]
            print(f"{indent}{c['task_id'][-8:]} [{c['depth']}] {c['description'][:60]}")
        return 0

    if sub == "traverse" and len(argv) >= 2:
        rest, _ = _parse_concurrent(argv[1:])
        query_text = " ".join(rest) if rest else ""
        beam = 3
        hops = 3
        i = 0
        while i < len(argv):
            if argv[i] == "--beam" and i + 1 < len(argv):
                beam = int(argv[i+1]); i += 2; continue
            if argv[i] == "--hops" and i + 1 < len(argv):
                hops = int(argv[i+1]); i += 2; continue
            i += 1
        result = mem_mod.traverse(query_text, beam_width=beam, max_hops=hops)
        narrative = mem_mod.synthesize(result, query_text)
        print(json.dumps(narrative, ensure_ascii=False, indent=2))
        return 0

    if sub == "query" and len(argv) >= 2:
        rest, _ = _parse_concurrent(argv[1:])
        query_text = " ".join(rest) if rest else ""
        # 提取 --files
        files = None
        i = 0
        while i < len(argv):
            if argv[i] == "--files" and i + 1 < len(argv):
                files = [f.strip() for f in argv[i + 1].split(",")]
                i += 2
                continue
            i += 1

        result = mem_mod.query(query_text, files=files)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("用法: scheduler memory stats|rebuild|query|latent|traverse [参数]",
          file=sys.stderr)
    return 2


# ═══════════════════════════════════════════════════════════
# project 子命令
# ═══════════════════════════════════════════════════════════

def _cmd_project(argv: list) -> int:
    if not argv:
        print("用法: scheduler project create|list|show|advance|reject [参数]", file=sys.stderr)
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

    proj = create(name=name, template=template, budget=budget, auto_mode=auto_mode)
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
        Phase.RESEARCHING: "E",
        Phase.PLANNING: "D",
        Phase.REVIEWING: "D",
    }.get(phase, "-")


def _phase_cost_estimate(phase: Phase, proj) -> float:
    """估算 phase 的费用 ($)。返回 0 表示免费。"""
    rates = {
        Phase.RESEARCHING: 0.02,   # E层 DeepSeek/GLM 廉价
        Phase.PLANNING: 2.50,      # D层 Opus/GPT 架构
        Phase.REVIEWING: 1.00,     # D层审查(需推理能力出优化方案)
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
