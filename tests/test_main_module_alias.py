"""`python -m singularity.web.app` 起的进程里，`import singularity.web.app`
必须命中**同一个对象** —— 不许诞生第二个副本。

来历（2026-09-19 夜真机抓到）：`-m` 把代码跑在 `__main__` 名下，`sys.modules` 里
**不留**包名那条记录 ⇒ 晚来的 `from singularity.web import app` **把这个文件又执行
一遍**，拿到第二个模块副本，它的模块级全局是另一套。
`_api_projects._loop_status` 正是这么读的 ⇒ 副本的 `_loop_running` 停在初始值
`False`（起循环那段在 `__main__` 守卫里，副本走不到），而 `/api/loop/status`
读真身回 `true` —— **同进程同一个变量，两条路两个答案**，于是过 GATE2/3 时
恒弹「调度循环没在跑，去界面上启动它」。**狼来了**：真坏掉时和正常时一模一样。

⚠️ **必须另起子进程**：这一整段就是"进程级模块表长什么样"，在测试进程里跑会把
真循环/观察者起在 pytest 里，还会把状态写进生产 `.qidian/`。
⚠️ **必须走 `runpy.run_module(alter_sys=True)`**：它复刻的正是 `-m` 那一点 ——
`sys.modules` 里没有包名那条、而 `__main__` 是它。换个写法（直接 import）
就把要测的东西测没了。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 子进程的引导：照抄"真启动命令"的模块语义，只是最后不 bind 端口。
_BOOTSTRAP = r'''
import json, os, runpy, sys
from pathlib import Path

# ① 状态目录整个挪走 —— 不许碰真 .qidian/（这段跑在 app 被 import 之前）
probe = Path(os.environ["QIDIAN_PROBE_DIR"])
import singularity.scheduler.config as c
c.QIDIAN_DIR = probe
for n in ("SNAPSHOT_DIR", "PATCH_DIR", "TRACE_DIR", "HOLD_DIR", "CANCEL_DIR",
          "PAUSE_DIR", "PARKED_DIR", "PARTIAL_USAGE_DIR"):
    if hasattr(c, n):
        setattr(c, n, probe / getattr(c, n).name)

# ② 两个 WS 端口不绑：要占 5051 / 8765，跟"模块身份"这条判据毫无关系
import singularity.scheduler.bridge as br
br.start_ws_server = lambda *a, **k: None
br.start_observer_server = lambda *a, **k: None

# ③ 起完就自检，不起服务
import flask
def _check(self=None, *a, **k):
    import singularity.web.app as w
    main = sys.modules["__main__"]
    print("SAME_OBJECT:", w is main)
    print("MAIN_LOOP_RUNNING:", getattr(main, "_loop_running", None))
    print("IMPORTED_LOOP_RUNNING:", getattr(w, "_loop_running", None))
    sys.stdout.flush()
    os._exit(0)
flask.Flask.run = _check

runpy.run_module("singularity.web.app", run_name="__main__", alter_sys=True)
'''


def _run_app(tmp_path) -> str:
    probe_state = tmp_path / "state"
    probe_state.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ,
               PYTHONPATH=str(REPO / "src"),
               QIDIAN_PROBE_DIR=str(probe_state),
               PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-c", _BOOTSTRAP],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=240)
    return p.stdout + p.stderr


def test_m_起的进程里_import_拿到的是同一个模块(tmp_path):
    """判据：`import` 到的是 `__main__` 本人，且两边 `_loop_running` 一致。

    变异验过：删掉 `app.py` 里 `sys.modules[__spec__.name] = …` 那行 →
    `SAME_OBJECT: False`、两个 `_loop_running` 一个 True 一个 False → 这条红。
    """
    out = _run_app(tmp_path)
    assert "SAME_OBJECT: True" in out, (
        "`-m` 起的进程里 import 拿到了**另一个副本** —— 它的模块级全局是另一套，"
        f"凡是靠它读状态的都会读到错的：\n{out[-2000:]}")

    main = re.search(r"MAIN_LOOP_RUNNING: (\w+)", out)
    imp = re.search(r"IMPORTED_LOOP_RUNNING: (\w+)", out)
    assert main and imp, f"自检没跑起来（看输出）：\n{out[-2000:]}"
    assert main.group(1) == "True", "前提就不对：这个进程里循环根本没起来"
    assert imp.group(1) == "True", (
        f"副本读到的 _loop_running 是 {imp.group(1)}，真身是 {main.group(1)} ——"
        " 过 GATE2/3 时就会恒弹「调度循环没在跑」")


def test_不许碰真的_qidian(tmp_path):
    """**命门**：这条测试会真起一次 web app，绝不能写到生产的 `.qidian/`。

    （本仓栽过：跑测试往真 `alerts.jsonl` 追加、在真目录里建 worktrees。）
    """
    alerts = REPO / ".qidian" / "alerts.jsonl"
    before = alerts.stat().st_mtime_ns if alerts.exists() else None
    _run_app(tmp_path)
    after = alerts.stat().st_mtime_ns if alerts.exists() else None
    assert before == after, "子进程往生产 alerts.jsonl 写了东西 —— 隔离没生效"
