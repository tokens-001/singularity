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

_LOOP_POLL_SECS = 3  # 队列空时的轮询间隔


def main(argv: list) -> int:
    if not argv:
        print(
            "用法: python3 -m scheduler add|run|loop|rollback|apply|status|merge [参数]",
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
            except Exception:  # noqa: BLE001
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
