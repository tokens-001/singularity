"""task-api 流水线真机复现：复杂多文件任务会不会「反复测不收敛」。

背景（docs/执行超时与模型能力自动化.md 遗留）：task-api 那种「架构决策 + 多依赖」
的任务曾反复测不过、不收敛。单任务侧已修，但**流水线层面没验全** —— 需要真跑一条
project 流水线，烧多模型额度。

走真实路径，不做任何打桩：
  建项目(auto) → start_project_workflow → 后台调度循环推阶段 → 轮询到终态

判据（跑完才判定）：
  - 到达执行/审查/合并并收敛（不是卡在某个 phase 不动）
  - 没有撞「达到最大轮次」这类兜底
  - 看 .qidian/alerts.jsonl 里这一轮新增的告警

用法: .venv/bin/python tests/integration/pipeline_probe.py [超时秒 默认1800]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp_mod            # noqa: F401 破循环依赖
from singularity.scheduler import config, project as proj_mod, workflow as wf_mod
from singularity.scheduler import witness

DEADLINE_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 1800

# 「架构决策 + 多依赖」的最小复现：3 个文件、有依赖顺序、有跨文件契约
DESC = (
    "写一个命令行待办工具 todo，拆成三个文件："
    "① todo/store.py —— JSON 文件读写，提供 add/list/done/remove 四个函数，"
    "   数据落盘到 ~/.todo.json，读损坏文件不能崩；"
    "② todo/cli.py ——  argparse 子命令，调用 store，输出人类可读；"
    "③ todo/__init__.py —— 导出 store 的公共函数。"
    "cli 依赖 store 的接口，__init__ 依赖 store 的函数名 —— 三者的函数签名必须一致。"
    "全部用标准库，不引第三方依赖。"
)


def _alerts_since(ts: float) -> list[dict]:
    return [a for a in witness.read_alerts(limit=200) if a.get("ts", 0) >= ts]


def main() -> int:
    agents = disp_mod.load_agents()
    name = f"pipeline-probe-{int(time.time())}"
    p = proj_mod.create(name=name, description=DESC, scope="标准库 CLI 工具",
                        template="product_dev", budget=2.0)
    p.auto_mode = True
    proj_mod.save(p)
    print(f"项目 {p.id[:8]} 建好 | phase={p.phase.value} auto={p.auto_mode}")

    t0 = time.time()
    wf_mod.start_project_workflow(p, agents)
    print(f"已 start，轮询中（上限 {DEADLINE_SEC}s）…\n")

    last = ""
    while time.time() - t0 < DEADLINE_SEC:
        try:
            cur = proj_mod.load(p.id)
        except Exception as e:
            print(f"  读取项目失败: {e}")
            break
        if cur is None:
            print("  项目不见了（被删？）")
            break
        state = f"{cur.phase.value} tasks={len(cur.task_ids or [])}"
        if state != last:
            print(f"  [{time.time()-t0:6.0f}s] {state}")
            last = state
        if cur.phase.value in ("done", "failed", "rolled_back"):
            break
        time.sleep(10)

    cur = proj_mod.load(p.id)
    dt = time.time() - t0
    print(f"\n=== 结果（{dt:.0f}s）===")
    if cur is None:
        print("❌ 项目读取不到，无法判定")
        return 1
    print(f"终态 phase: {cur.phase.value}")
    from singularity.scheduler import tracker
    done = failed = 0
    for tid in (cur.task_ids or []):
        t = tracker.read_task(tid)
        st = t.status.value if t and hasattr(t.status, "value") else (t.status if t else "?")
        if st == "done":
            done += 1
        elif st in ("failed", "rolled_back"):
            failed += 1
        print(f"  {tid[:10]} {st}  {str(getattr(t, 'term_reason', '') or '')[:70]}")
    alerts = _alerts_since(t0)
    print(f"\n本轮新增告警 {len(alerts)} 条")
    for a in alerts[:12]:
        print(f"  · [{a.get('scope')}] {a.get('msg')}")

    stuck = cur.phase.value not in ("done",)
    print("\n" + ("❌ 没跑到终态（疑似卡住/不收敛）" if stuck
                  else f"✅ 流水线跑通（{done} 个任务完成，{failed} 个失败）"))
    return 1 if stuck else 0


if __name__ == "__main__":
    sys.exit(main())
