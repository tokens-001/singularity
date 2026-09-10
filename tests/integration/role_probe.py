"""A/B：`route_role` 注入的角色提示词，到底改变产物吗？

背景：`route_role` 这个字段**从上线到 2026-09-11 一直是死的** —— 写入侧被
`hasattr` 静默丢弃、读取侧永远拿到 ""，所以角色提示词从没注入过。今晚修好后
它第一次真正生效：首轮会把 `roles.toml` 里那个角色的 system_prompt 拼在任务前面。

**行为变了，但没人看过产物差异。** 这个脚本就是去补那一眼。

设计：
  PROBE_ROLE=implementer（默认）→ 带角色提示词
  PROBE_ROLE=                → 不带（route_role 为空）
  同一个任务、同一批模型、只切这一个变量 → 产物拿来 diff / 判读

任务特意选得**有余地**（有 UI 输出、有错误处理、有落盘），这样 implementer
提示词里那几条「视觉效果 / 错误处理 / 可运行性」才有发挥空间 —— 太简单的任务
（比如"写个 add 函数"）两种跑法必然一样，测了等于没测。

用法:
  PROBE_ROLE=implementer .venv/bin/python tests/integration/role_probe.py
  PROBE_ROLE=            .venv/bin/python tests/integration/role_probe.py
"""
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp_mod           # noqa: F401 破循环依赖
from singularity.scheduler import orchestrator, project as proj_mod, tracker

ROLE = os.environ.get("PROBE_ROLE", "implementer")
DEADLINE = int(os.environ.get("PROBE_DEADLINE", "600"))

DESC = (
    "写一个命令行待办工具 todo.py（单文件，只用标准库）：\n"
    "① 支持 add / list / done / remove 四个子命令；\n"
    "② 数据存到 ~/.todo.json，**读损坏的文件不能崩**（给出可读提示）；\n"
    "③ list 的输出要人类可读（带序号 / 勾选状态）；\n"
    "④ 文件末尾加一段 __main__ 自测。"
)


def main() -> int:
    agents = disp_mod.load_agents()
    tag = ROLE or "(空)"
    p = proj_mod.create(name=f"role-probe-{tag}-{int(time.time())}",
                        description=DESC, scope="单文件 CLI", template="product_dev",
                        budget=1.0)
    p.auto_mode = True
    p.phase = proj_mod.Phase.EXECUTING
    proj_mod.save(p)
    proj_mod.ensure_repo(p.id)          # 任务 dispatch 时 snap.take 需要

    # 直接建任务（不走 _run_execution）—— 这样 route_role 完全可控
    t = tracker.create(DESC, project_id=p.id)
    tracker.transition(t.id, tracker.TaskStatus.PENDING, route_locked=True,
                       route_type="default", route_gate=False,
                       route_level="any", route_role=ROLE)
    p.task_ids.append(t.id)
    proj_mod.save(p)

    print(f"route_role = {tag!r} | 任务 {t.id[:10]} | 项目 {p.id[:8]}")
    t0 = time.time()
    while time.time() - t0 < DEADLINE:
        try:
            orchestrator.run_queue(agents, max_concurrent=1)
        except Exception as e:
            print(f"  run_queue 抛异常: {type(e).__name__}: {e}")
            break
        cur = proj_mod.load(p.id)
        if cur is None:
            print("  项目不见了"); break
        if cur.phase.value in ("done", "failed", "rolled_back"):
            break
        time.sleep(3)

    task = tracker.read_task(t.id)
    st = task.status.value if task and hasattr(task.status, "value") else "?"
    print(f"终态: {st} | 耗时 {time.time()-t0:.0f}s")

    repo = proj_mod.repo_dir(p.id)
    files = sorted(f for f in repo.rglob("*")
                   if f.is_file() and ".git" not in f.parts)
    print(f"\n产物（{repo}）：")
    for f in files:
        body = f.read_text(encoding="utf-8", errors="replace")
        h = hashlib.sha256(body.encode()).hexdigest()[:12]
        print(f"  {f.relative_to(repo)}  {len(body)} 字  sha={h}")
    out = repo / "todo.py"
    if out.exists():
        Path(f"/tmp/role_{tag}.py").write_text(out.read_text(encoding="utf-8"),
                                               encoding="utf-8")
        print(f"  → 存到 /tmp/role_{tag}.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
