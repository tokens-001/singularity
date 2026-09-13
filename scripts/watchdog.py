#!/usr/bin/env python3
"""进程外看门狗 —— 盯「整个后端装死」（2026-09-13）。

## 它为什么必须住在进程外

我们所有的仪器（告警 / 聚合视图 / 孤儿探测 / 各种判据）**全部住在被观测的进程里**。
§57 教过：挂起不是异常、`except` 拦不住；§65 的现场：循环空转到 `sleep(3)`、
任务孤儿挂着。**进程内的仪器看不见进程自己挂死 —— 那台仪器也在里面。**

一个独立进程、一分钟看一眼，零依赖（只用标准库）。

⚠️ **但它不是"零运行时代码改动"** —— 这句原来的说法是错的（2026-09-13 更正）。
零改动只能拿到"进程还在 **+ HTTP 应**"，**判不出"活着但不动了"**：
`/health` 报的 `_loop_running` 是个**布尔标志位**，循环卡住它照样是 `true`。
所以要真判得了这一类，循环里必须多那 **3 行心跳落盘**（`app.py::_write_loop_tick`）。
**"只加一个外部脚本就够了"这个印象会让人把心跳删掉、看门狗就退化成"进程死了没"这一半。**

## 它判什么（三类，别越界）

| 判据 | 含义 |
|---|---|
| `process_down` | HTTP 不通 → 进程没了 / 端口没在听 / 整个卡死到不回请求 |
| `loop_stalled` | HTTP 通、但 `loop_tick.json` 太旧，**且循环自称在跑** → 活着但不动了 |
| `ok` | 两者都正常 |

⚠️ **它判不了的（写清楚，免得"看门狗绿"给人虚假安全感）**：
- **单条线程挂起**（§57 那种）：进程活着、HTTP 照应、循环照 tick —— 全绿。
  那类归**进程内**的判据管（卡死相位 / 孤儿探测）。
- **"活着但在撒谎"**（判据本身不成立）：它只看两个活性信号，不看语义。
- **任务级超时**：那是 orchestrator 的 900s 收割的事，不是"装死"。

## 两个必须处理的假阳性（不处理的话这台看门狗活不过一天）

1. **长任务期间 tick 天然是旧的。** 循环体里 `orchestrator.run_queue()` 是**阻塞**的，
   一个任务能占满 900s。⇒ 阈值必须盖过它，默认 **25 分钟**
   （900s 收割 + 收尾/校验/合并的余量）。
2. **用户会主动停循环。** 那种情况下 tick 当然不更新 —— 但那不是故障。
   ⇒ 先读 `/health` 的 `loop_running`，**只有它为 true 时才判 tick**。

再加一条防疲劳：**只在状态"变化"时通知**（连续 `--fail-threshold` 次才升级为故障），
恢复时补一条"已恢复"。每次都喊的看门狗会被关掉，等于没有。

## 怎么装（默认**不自动装** —— 装定时任务是改你的机器）

    # 看一眼现在什么状态
    .venv/bin/python scripts/watchdog.py --once

    # 装成 launchd 每分钟跑一次（可回退：launchctl unload -w …）
    sh scripts/install-watchdog.sh

    # 卸掉
    sh scripts/install-watchdog.sh uninstall

## 自查

    .venv/bin/python scripts/watchdog.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TICK = ROOT / ".qidian" / "loop_tick.json"

# 长任务能把循环占满 900s（orchestrator 的收割线），留足余量。
DEFAULT_TICK_MAX_AGE = 25 * 60


def classify(http_ok: bool, loop_running: bool | None,
             tick_age: float | None, tick_max_age: float) -> str:
    """纯函数，好测。**判据只有这一处**，别在别处再长一份。"""
    if not http_ok:
        return "process_down"
    if loop_running is not True:
        # 循环是停的（用户主动停 / 还没启动）—— tick 不更新是**正常的**。
        return "loop_stopped"
    if tick_age is None:
        return "loop_stalled"          # 自称在跑却从没落过 tick
    if tick_age > tick_max_age:
        return "loop_stalled"
    return "ok"


def probe(url: str, timeout: float) -> tuple[bool, bool | None]:
    """→（HTTP 通不通, 循环自称在不在跑）。第二条拿不到时是 None。"""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            if r.status != 200:
                return False, None
            body = json.loads(r.read().decode("utf-8", "replace"))
        return True, bool(body.get("loop_running"))
    except Exception:
        return False, None


def tick_age(path: Path = TICK) -> float | None:
    """tick 文件的年龄（秒）；没有/读坏了 → None（= 从没落过）。"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return time.time() - float(d["ts"])
    except Exception:
        return None


def read_pid(path: Path = TICK) -> int | None:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("pid"))
    except Exception:
        return None


def notify(title: str, msg: str) -> None:
    """macOS 桌面通知。**失败不影响退出码** —— 通知发不出去不该让看门狗自己算故障。"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def _load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_once(args) -> str:
    ok, loop_running = probe(args.url, args.timeout)
    age = tick_age(args.tick)
    state = classify(ok, loop_running, age, args.tick_max_age)

    prev = _load_state(args.state)
    # ⚠️ `loop_stopped` **不算失败**（用户主动停循环是正常操作，可能停一整晚）。
    # 把它算进去的话，计数会一路涨到阈值以上 ⇒ 之后第一次**真**故障会跳过防抖、
    # 一跑就喊。防抖要防的是"抖动"，不是"停了很久"。
    fault = state in ("process_down", "loop_stalled")
    fails = (prev.get("fails", 0) + 1) if fault else 0
    # 只在**状态变化**时通知（连续 fail-threshold 次才算数；恢复时补一条）
    announced = prev.get("announced")
    now_announced = state if fails >= args.fail_threshold else None

    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {state:14s} "
            f"http={'ok' if ok else 'FAIL'} loop_running={loop_running} "
            f"tick_age={('%.0fs' % age) if age is not None else 'n/a'} fails={fails}")
    print(line, flush=True)
    try:
        with open(args.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

    if now_announced != announced:
        if now_announced == "process_down":
            notify("奇点后端没了", "看门狗：HTTP 不通（进程挂了 / 端口没了）。")
        elif now_announced == "loop_stalled":
            notify("奇点调度循环卡住",
                   f"看门狗：进程活着但循环 {int(age or -1)}s 没动过。")
        elif now_announced is None and announced:
            notify("奇点已恢复", f"看门狗：从 {announced} 恢复正常。")

    try:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(
            {"fails": fails, "announced": now_announced, "state": state,
             "ts": time.time(), "pid": read_pid(args.tick)}), encoding="utf-8")
    except Exception:
        pass
    return state


def selftest() -> int:
    """判据的自我检查 —— 这台看门狗的真假全靠 classify 这一个函数。"""
    cases = [
        # (http_ok, loop_running, tick_age, 期望)
        (False, None, None, "process_down"),
        (False, True, 0.0, "process_down"),          # HTTP 挂了优先于一切
        (True, False, 99999.0, "loop_stopped"),      # 循环是停的 → tick 旧是正常的
        (True, None, 99999.0, "loop_stopped"),       # 拿不到 loop_running 不当故障
        (True, True, None, "loop_stalled"),          # 自称在跑却从没落过 tick
        (True, True, 10.0, "ok"),
        (True, True, DEFAULT_TICK_MAX_AGE + 1, "loop_stalled"),
        (True, True, DEFAULT_TICK_MAX_AGE - 1, "ok"),
    ]
    bad = [(c, classify(*c[:3], DEFAULT_TICK_MAX_AGE)) for c in cases
           if classify(*c[:3], DEFAULT_TICK_MAX_AGE) != c[3]]
    for c, got in bad:
        print(f"  ❌ {c} → {got}")
    print(f"{'✅ 全过' if not bad else f'❌ {len(bad)} 条不过'}  （{len(cases)} 条）")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="奇点进程外看门狗")
    ap.add_argument("--once", action="store_true", help="跑一次就退出（默认行为）")
    ap.add_argument("--selftest", action="store_true", help="只跑判据自检")
    ap.add_argument("--url", default=os.environ.get("QIDIAN_URL", "http://127.0.0.1:5050"))
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--tick", type=Path, default=TICK)
    ap.add_argument("--tick-max-age", type=float, default=DEFAULT_TICK_MAX_AGE)
    ap.add_argument("--fail-threshold", type=int, default=2,
                    help="连续几次才升级为故障并通知（防单次抖动）")
    ap.add_argument("--state", type=Path, default=ROOT / ".qidian" / "watchdog_state.json")
    ap.add_argument("--log", default=str(ROOT / ".qidian" / "watchdog.log"))
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    state = run_once(args)
    return 0 if state in ("ok", "loop_stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
