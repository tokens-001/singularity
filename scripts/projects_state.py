#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**起后端之前先跑这个**：盘上每个项目"会不会一启动就开烧"。

为什么是脚本：`~/OPEN.md` 里那张「盘上项目现状」表是**手抄**的，2026-09-29 实测已经烂了
—— 表里列了 4 个项目、盘上有 12 个，而它读起来和"我核过了"一模一样。
（同族的教训在 `scripts/alert_map.py` 头上写着：手抄的表会烂，能重新生成就别手抄。）

判据**直接调 `project.halt_state`**，不在这儿重写一遍 —— 重写的那份迟早跟生产的那份漂。
两条「重启即开烧」的路：
  ① 僵尸任务被回收重派 —— 要有**非终态**任务
  ② 「任务清空 + phase 非终态 ⇒ 启动即重新拆解」—— 要 `task_ids` 为空**且没停**

跑法：`.venv/bin/python scripts/projects_state.py`（`--json` 给下游用）
退出码：**0** = 全部安全（或盘上没项目）；**1** = 有项目会开烧 ⇒ **别起后端，先处理**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from singularity.scheduler import project as proj_mod          # noqa: E402
from singularity.scheduler import tracker                      # noqa: E402

TERMINAL = {"done", "failed", "rolled_back", "cancelled"}


def _status_text(t) -> str:
    """任务状态 → 字符串。**必须走 `.value`**：`str(TaskStatus.DONE)` 是
    `'TaskStatus.DONE'`，直接拿去做集合判定会把**所有终态任务判成"非终态"** ——
    2026-09-29 第一版就是这么写的，当场把一个 `phase=done` 的项目报成"会开烧"。
    （读盘那条路没这个坑：JSON 里存的就是 `"done"`。）"""
    st = getattr(t, "status", "")
    return str(getattr(st, "value", st)).lower()


def _non_terminal(task_ids: list[str]) -> list[str]:
    """还没走到终态的任务 id（**读不到任务文件也算**：那正是"僵尸"的形状）。"""
    out = []
    for tid in task_ids or []:
        t = tracker.read_task(tid)
        if t is None or _status_text(t) not in TERMINAL:
            out.append(tid)
    return out


def snapshot() -> list[dict]:
    rows = []
    for p in proj_mod.list_all():
        halt = proj_mod.halt_state(p)
        alive = _non_terminal(p.task_ids)
        # 会开烧 = 既没停、又有活干（僵尸任务 / 空 task_ids 的非终态阶段）
        will_burn = (not halt["halted"]) and bool(alive or not p.task_ids)
        rows.append({
            "id": p.id, "name": p.name, "phase": str(getattr(p.phase, "value", p.phase)),
            "halted": halt["halted"], "halt_reason": halt["reason"],
            "failed_tasks": len(halt.get("failed_tasks") or []),
            "task_ids": len(p.task_ids or []), "non_terminal": alive,
            "will_burn": will_burn,
        })
    return rows


def main(argv: list[str]) -> int:
    rows = snapshot()
    if "--json" in argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 1 if any(r["will_burn"] for r in rows) else 0

    if not rows:
        print("盘上 0 个项目 —— 没有能烧的。")
        return 0
    print(f"{'project':>16} {'phase':<12} {'停?':<4} {'原因':<12} {'失败':>4} {'任务':>4} {'非终态':>6}  name")
    for r in rows:
        print(f"{r['id']:>16} {r['phase']:<12} {'停' if r['halted'] else '🟢动':<4} "
              f"{(r['halt_reason'] or '-'):<12} {r['failed_tasks']:>4} {r['task_ids']:>4} "
              f"{len(r['non_terminal']):>6}  {r['name']}")
    burn = [r for r in rows if r["will_burn"]]
    if burn:
        print(f"\n⛔ {len(burn)} 个项目会开烧 —— **别起后端，先处理**：")
        for r in burn:
            print(f"   {r['id']} {r['phase']} 非终态={len(r['non_terminal'])} task_ids={r['task_ids']}")
            print(f"     ⇒ `POST /api/projects/{r['id']}/stop`，或清掉那几个非终态任务")
        return 1
    print(f"\n✅ {len(rows)} 个项目全部安全（停着，或没有活干）—— 可以起后端")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
