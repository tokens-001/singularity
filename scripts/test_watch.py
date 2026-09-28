#!/usr/bin/env python3
"""`scripts/watch.py` 的自测 —— 只测**纯渲染**那一层，不联后端。

为什么只测渲染：看板的取数要起后端，而起后端 = 循环 + 观察者一起起 = **立刻开烧**
（仓里那条止血规矩）。所以把"画什么"和"取什么"拆开，画的那半在这里用构造的
状态覆盖，取的那半只保证**取不到也不炸**。

🔴 要害都在**第一行那个「多久没动」**上（2026-09-28 定）：
  · 一个任务都没在跑 + 很久没动 ⇒ **要出告警**（这就是 `round-i` 静默 47 分钟那个洞）
  · 有任务在跑 + 很久没动 ⇒ **不许报警**（任务正常跑 900 秒期间不产生 lineage，
    这个数照样涨 —— 报了就是假红，而假红比不报坏：人学会无视它之后真停住那次也没人看）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watch import render  # noqa: E402


def 状态(**kw) -> dict:
    st = {"pid": "p1", "name": "验收-日志统计-x", "err": "", "phase": "executing",
          "idle": 0, "halted": False, "gate": "", "cost": 0.0123, "loop": {"running": True},
          "tasks": {"running": 0, "waiting": 0, "done": 0, "failed": 0, "total": 0},
          "alerts": [], "issues": []}
    st.update(kw)
    return st


def 文本(st) -> str:
    return "\n".join(render(st, 100))


def test_没任务在跑又很久没动_要出告警():
    t = 文本(状态(idle=2820, tasks={"running": 0, "waiting": 12, "done": 0, "failed": 0, "total": 12}))
    assert "没有任何动作" in t, f"静默停住时看板一个字都不说：\n{t}"


def test_有任务在跑又很久没动_不许报警():
    """反方向。任务正常跑 900 秒期间不产生 lineage，idle 照样涨。"""
    t = 文本(状态(idle=2820, tasks={"running": 1, "waiting": 11, "done": 0, "failed": 0, "total": 12}))
    assert "没有任何动作" not in t, f"任务在跑却报『没有任何动作』—— 假红：\n{t}"
    assert "个任务在跑" in t, "该说的是『无状态变化（N 个任务在跑）』"


def test_停住和门都要摆出来():
    assert "停住（等人）" in 文本(状态(halted=True, idle=900))
    t = 文本(状态(gate="gate2", phase="gate2"))
    assert "该点批准了" in t and "GATE2" in t, f"门没摆出来：\n{t}"


def test_等和在跑必须分成两个数():
    """09-27 真机：顶栏写「14 个执行中」而真正在跑的只有 2 个。"""
    t = 文本(状态(tasks={"running": 2, "waiting": 12, "done": 0, "failed": 0, "total": 14}))
    assert "2 跑" in t and "12 等" in t, f"把『在等』报成了『在跑』：\n{t}"


def test_后端连不上时要说话不许炸():
    """取不到就说取不到 —— 空屏和"项目很安静"长得一模一样。"""
    t = 文本(状态(err="后端连不上 / 项目读不到：URLError", phase="?", idle=None))
    assert "连不上" in t, f"取不到却不说：\n{t}"


def test_idle_短的时候不报():
    t = 文本(状态(idle=30, tasks={"running": 0, "waiting": 1, "done": 7, "failed": 0, "total": 8}))
    assert "没有任何动作" not in t, "刚启动就报警 —— 那是噪声"


def test_取数取不到也不炸():
    """`collect` 打不通后端时**必须回一个能渲染的状态**，不是抛异常。

    这里直接把 BASE 指到一个必然打不通的端口 —— 不依赖外部环境起没起。
    """
    import watch
    old = watch.BASE
    watch.BASE = "http://127.0.0.1:59999"
    try:
        st = watch.collect("不存在的项目")
    finally:
        watch.BASE = old
    assert st["err"], "打不通却不说 —— 看板会显示成『项目很安静』"
    render(st, 100)          # 渲染不许炸


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"✅ {name}")
        except Exception:
            fails += 1
            print(f"❌ {name}")
            traceback.print_exc()
    raise SystemExit(1 if fails else 0)
