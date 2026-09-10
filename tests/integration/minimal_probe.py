"""最小验证：那四条"复活"的子系统到底写不写数据。

背景：`route.level` 修复之前，`_task_runner.finalize` 里那几行每任务必抛
AttributeError 被 except 吞掉，所以下面四条**从没真正执行过**：
    · record_tokens      → .qidian/token_usage.json
    · learner.record     → .qidian/route_learner.json
    · archive_experience → .qidian/memory/events.json
    · update_attrs       → 记忆库里的任务属性

为什么不跑完整流水线：调研 + 架构那两步最贵，而且刚在智谱欠费上翻车。
这里**直接塞一份合法架构**、把项目置成 executing，只跑到「执行 + 收尾」——
那正是这四条触发的地方。判据也一样：不看终态，看产物。

用法: .venv/bin/python tests/integration/minimal_probe.py [超时秒 默认900]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp_mod           # noqa: F401 破循环依赖
from singularity.scheduler import config, orchestrator, project as proj_mod, workflow as wf_mod

DEADLINE = int(sys.argv[1]) if len(sys.argv) > 1 else 900

ARCH = {
    "architecture": "极简：一个 Python 文件 + 一个自测。",
    "modules": [{"name": "adder", "responsibility": "整数加法",
                 "depends_on": [], "interfaces": ["add(a, b) -> int"]}],
    "tasks": [{
        "id": "T1",
        "title": "实现 adder.py",
        "description": "创建 adder.py，实现 add(a, b) 返回 a+b，文件末尾加一段 __main__ 自测。",
        "complexity": "low", "layer": "impl", "depends_on": [],
        "acceptance": "python3 -c \"import adder; assert adder.add(1,2)==3\"",
    }],
    "risks": [], "data_model": {}, "api_contracts": [],
    "tech_stack": {"language": "python"}, "constraints": [], "test_cases": {},
}


def _snap() -> dict:
    """那四条路径的产物现状。"""
    out = {}
    for name in ("token_usage.json", "route_learner.json"):
        p = config.QIDIAN_DIR / name
        out[name] = p.stat().st_size if p.exists() else None
    ev = config.QIDIAN_DIR / "memory" / "events.json"
    out["memory/events.json"] = ev.stat().st_size if ev.exists() else None
    # 经验归档写的是**另一个**文件（第一次探针只看了 events.json，漏了它）
    ex = config.QIDIAN_DIR / "memory" / "experiences.json"
    out["memory/experiences.json"] = ex.stat().st_size if ex.exists() else None
    return out


def main() -> int:
    agents = disp_mod.load_agents()
    before = _snap()
    print("跑之前:", json.dumps(before, ensure_ascii=False))

    p = proj_mod.create(name=f"minimal-{int(time.time())}",
                        description="最小验证：创建 adder.py", scope="单文件",
                        template="product_dev", budget=1.0)
    p.auto_mode = True
    p.architecture = ARCH
    p.phase = proj_mod.Phase.EXECUTING      # 跳过调研/架构，直达执行
    proj_mod.save(p)
    print(f"项目 {p.id[:8]} 就位 | phase={p.phase.value} | 架构里 {len(ARCH['tasks'])} 个任务")

    wf_mod.start_project_workflow(p, agents)
    print("已派发，驱动队列中…\n")

    t0 = time.time()
    last = ""
    while time.time() - t0 < DEADLINE:
        try:
            orchestrator.run_queue(agents, max_concurrent=1)
        except Exception as e:
            print(f"  run_queue 抛异常: {type(e).__name__}: {e}")
            break
        cur = proj_mod.load(p.id)
        if cur is None:
            print("  项目不见了")
            break
        state = f"{cur.phase.value} tasks={len(cur.task_ids or [])}"
        if state != last:
            print(f"  [{time.time()-t0:5.0f}s] {state}")
            last = state
        if cur.phase.value in ("done", "failed", "rolled_back"):
            break
        if not cur.task_ids and time.time() - t0 > 60:
            print("  ✗ 一分钟后仍然 0 个任务 —— 架构没拆出任务")
            break
        time.sleep(3)

    cur = proj_mod.load(p.id)
    print(f"\n=== 结果（{time.time()-t0:.0f}s）===")
    print(f"终态 phase: {cur.phase.value if cur else '?'}")
    from singularity.scheduler import tracker
    for tid in (cur.task_ids or []) if cur else []:
        t = tracker.read_task(tid)
        st = t.status.value if t and hasattr(t.status, "value") else "?"
        print(f"  {tid[:10]} {st}  {str(getattr(t, 'term_reason', '') or '')[:60]}")

    after = _snap()
    print("\n=== 那四条路径的产物（跑前 → 跑后）===")
    ok = True
    for k in before:
        b, a = before[k], after[k]
        grew = (a is not None and (b is None or a > b))
        ok = ok and grew
        mark = "✅" if grew else "❌"
        print(f"  {mark} {k:<22} {b if b is not None else '不存在'} → {a if a is not None else '不存在'}")

    print("\n" + ("✅ 四条都写了数据" if ok else "❌ 有路径没写 —— 见上表"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
