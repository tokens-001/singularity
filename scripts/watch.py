#!/usr/bin/env python3
"""奇点跑轮的**终端看板** —— 一条命令看着一轮跑完。

用法：
    .venv/bin/python scripts/watch.py            # 自动挑最新的非终态项目
    .venv/bin/python scripts/watch.py <项目id>
    .venv/bin/python scripts/watch.py --once     # 打一屏就退出（不占终端）

🔴 **为什么要有它**（2026-09-28）：看一轮跑，我原来的做法是 `curl` + `grep` + 把结果
贴给人 —— 而那一夜栽了两次**沉默**：
  · `round-20260928i` 卡在 `conflict_held` **静默 47 分钟**，我的监视器只盯 `phase`，
    而卡住时阶段不变 ⇒ **沉默看起来和在干活一模一样**；
  · `round-20260928j` 一个任务**连试 3 次盲改**，盘上什么都没说。
⇒ 这块看板把「**多久没动**」摆到第一行（`idle`），不再让它藏在别处。

⚠️ **只读**：只用 GET + 读 `.qidian/` 下的文件，**不碰任何写路径**。
⚠️ **不用第三方库**（`rich`/`textual` 在这台机器上装不了，见仓里那条 pypi 不通的注）
—— 只用标准库 `curses`。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QIDIAN = ROOT / ".qidian"
BASE = os.environ.get("QIDIAN_URL", "http://127.0.0.1:5050")

# 任务状态分四栏，**"在等"和"在跑"必须分开报**（09-27 真机：顶栏写「14 个执行中」
# 而真正在跑的只有 2 个）。判据与 `tracker` 同一份来源，别在这儿另立一套。
_RUNNING = {"running", "dispatched", "routed", "validating"}
_DONE = {"done"}
_DEAD = {"failed", "rolled_back"}


def _get(path: str, timeout: float = 4.0):
    """本机也不走代理 —— 127.0.0.1 会被系统代理劫走（报 python-socks）。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(BASE + path, timeout=timeout) as r:
        return json.load(r)


def pick_project() -> str:
    """挑一个看：优先**非终态**的，多个取最新的（项目 id 是时间戳，越大越新）。"""
    try:
        rows = _get("/api/projects").get("projects", [])
    except Exception:
        return ""
    卡着的 = [p for p in rows if p.get("phase") not in ("done", None)]
    return max((p.get("id", "") for p in (卡着的 or rows)), default="")


def task_counts(pid: str) -> dict:
    """从**盘上**数任务 —— `/api/projects/<id>/tasks` 这个路由不存在，
    而任务文件本来就是状态的唯一出处（`tracker` 也是这么读的）。"""
    out = {"running": 0, "waiting": 0, "done": 0, "failed": 0, "total": 0}
    d = QIDIAN / "tasks"
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if t.get("project_id") != pid:
            continue
        out["total"] += 1
        st = t.get("status")
        if st in _RUNNING:
            out["running"] += 1
        elif st in _DONE:
            out["done"] += 1
        elif st in _DEAD:
            out["failed"] += 1
        else:
            out["waiting"] += 1
    return out


def recent_alerts(n: int = 6) -> list[str]:
    p = QIDIAN / "alerts.jsonl"
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - 60_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            a = json.loads(ln)
        except Exception:
            continue
        ts = time.strftime("%H:%M:%S", time.localtime(a.get("ts") or 0))
        out.append(f"{ts}  {a.get('key', '?')}: {str(a.get('msg', ''))[:80]}")
    return out


def collect(pid: str) -> dict:
    """取一屏要用的全部事实。**任何一项取不到都不许炸** —— 看板自己要能显示"取不到"。"""
    st: dict = {"pid": pid, "err": "", "phase": "?", "idle": None, "halted": False,
                "gate": "", "name": "", "cost": None, "loop": None,
                "tasks": {"running": 0, "waiting": 0, "done": 0, "failed": 0, "total": 0},
                "alerts": [], "issues": []}
    try:
        d = _get(f"/api/projects/{pid}")
        st["name"] = d.get("name", "")
        st["phase"] = d.get("phase", "?")
        st["idle"] = d.get("idle_seconds")
        st["halted"] = bool((d.get("halt") or {}).get("halted"))
        st["halt_reason"] = (d.get("halt") or {}).get("reason", "")
        st["gate"] = st["phase"] if str(st["phase"]).startswith("gate") else ""
        st["issues"] = [str(i.get("detail") or i)[:110] for i in (d.get("issues") or [])[:3]]
        st["flow"] = (d.get("flow_decision") or {}).get("weight", "")
    except Exception as e:
        st["err"] = f"后端连不上 / 项目读不到：{type(e).__name__}"
    # 后端读不到就从**盘上**补几样 —— 看板的价值有一半是"后端挂了时还能看"
    # （那正是最需要看的时候）。盘上没有才算真没有。
    if not st["name"]:
        try:
            pj = json.loads((QIDIAN / "projects" / f"{pid}.json").read_text(encoding="utf-8"))
            st["name"] = pj.get("name", "")
            st["phase"] = st["phase"] if st["phase"] != "?" else pj.get("phase", "?")
            st["halted"] = bool(pj.get("halted_reason")) or st["halted"]
            st["halt_reason"] = pj.get("halted_reason", "")
        except Exception:
            pass
    st["tasks"] = task_counts(pid)
    st["alerts"] = recent_alerts()
    try:
        st["loop"] = _get("/api/loop/status")
    except Exception:
        st["loop"] = None
    try:
        st["cost"] = _get(f"/api/projects/{pid}/cost").get("cost")
    except Exception:
        st["cost"] = None
    return st


def _fmt_idle(s) -> str:
    if not isinstance(s, (int, float)):
        return "—"
    if s < 60:
        return f"{int(s)} 秒"
    if s < 3600:
        return f"{int(s / 60)} 分钟"
    return f"{s / 3600:.1f} 小时"


def render(st: dict, width: int = 80) -> list[str]:
    """把一屏画成**纯文本行**（不碰 curses ⇒ 可测、可 `--once`、可重定向到文件）。

    ⚠️ 判据只在 `collect` 里取一次，这里**不做任何二次推断** ——
    "停没停"由 `halt_state` 说，"多久没动"由 `idle_seconds` 说，
    界面不自己从 `phase` 推（那是第二份判据）。
    """
    W = max(40, min(width, 200))
    line = "─" * W
    out = []
    name = st.get("name") or st.get("pid") or "（没有项目）"
    out.append(f"奇点 · {name}"[:W])
    out.append(line)

    # ── 第一行就是「多久没动」── 那一夜栽的就是它 ──
    # ⚠️ **不许把它读成"卡住了"**：任务正常跑满 900 秒期间不产生 lineage，
    #    这个数照样涨。所以有任务在跑时措辞是"无状态变化"，不是告警。
    t = st["tasks"]
    idle = st.get("idle")
    big = isinstance(idle, (int, float)) and idle >= 300
    动静 = f"idle {_fmt_idle(idle)}"
    if big and t["running"] == 0 and not st.get("halted") and not st.get("gate"):
        动静 += "  ⚠ 没有任何动作（一个任务都没在跑）"
    elif big:
        动静 += f"（{t['running']} 个任务在跑）"
    out.append(f"phase: {st['phase']:<16}{动静}"[:W])

    停 = "⛔ 停住（等人）" if st.get("halted") else ""
    if st.get("gate"):
        停 = f"🔴 GATE{str(st['gate']).replace('gate', '')} —— 该点批准了"
    loop = st.get("loop")
    循环 = ""
    if isinstance(loop, dict):
        循环 = f"循环 {'在跑' if loop.get('running') else '已停'}"
        if loop.get("stopped_by_human"):
            循环 += "（人停的）"
    if 停 or 循环:
        out.append(f"{停}   {循环}".strip()[:W])
    行 = (f"任务 {t['running']} 跑 · {t['waiting']} 等 · {t['done']} 完成 · "
          f"{t['failed']} 失败  （共 {t['total']}）")
    if isinstance(st.get("cost"), (int, float)):
        行 += f"   花费 ${st['cost']:.4f}"
    out.append(行[:W])

    if st.get("err"):
        out.append(line)
        out.append(f"⚠ {st['err']}"[:W])
        out.append("（先确认后端起没起：curl -s --noproxy '*' http://127.0.0.1:5050/api/status）"[:W])

    if st.get("issues"):
        out.append(line)
        out.append("项目问题"[:W])
        for s in st["issues"]:
            out.append(f"  ⚠ {s}"[:W])

    out.append(line)
    out.append("最近告警"[:W])
    if st.get("alerts"):
        for a in st["alerts"]:
            out.append(f"  {a}"[:W])
    else:
        out.append("  （没有）"[:W])
    return out


def _draw(stdscr, st: dict) -> None:
    import curses
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    for i, ln in enumerate(render(st, w - 1)):
        if i >= h - 2:
            break
        try:
            stdscr.addnstr(i, 0, ln, w - 1)
        except curses.error:
            pass
    try:
        stdscr.addnstr(h - 1, 0, "  [q] 退出 · [r] 立刻刷新 · 每 3 秒自动刷", w - 1)
    except curses.error:
        pass
    stdscr.refresh()


def main(argv: list) -> int:
    once = "--once" in argv
    args = [a for a in argv if not a.startswith("--")]
    pid = args[0] if args else pick_project()
    if not pid:
        print("挑不出项目 —— 盘上一个都没有，或者后端没起。")
        return 1
    if once:
        for ln in render(collect(pid), 100):
            print(ln)
        return 0

    import curses

    def _loop(stdscr):
        import curses as c
        c.curs_set(0)
        stdscr.nodelay(True)
        while True:
            _draw(stdscr, collect(pid))
            for _ in range(30):                      # 3 秒，拆成 100ms 一觉好收按键
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return
                if k == ord("r"):
                    break
                time.sleep(0.1)
    curses.wrapper(_loop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
