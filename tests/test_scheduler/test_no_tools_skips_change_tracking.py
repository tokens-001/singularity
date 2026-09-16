"""`no_tools` 的执行器**不该追改动** —— 它没有写文件的工具，追了只会报假警。

## 症状（2026-09-16 查清）

告警页前两名长期是：
    ×338  collect_changes          ← 假警
    ×133  lazy_spoke_import_failed

`collect_changes` 来自**评审 / QA** 那几路 dispatch —— 它们产出的是一段 JSON
（diff/代码是**内联喂进去的**），根本不需要工具，却按默认值去 `_track_changed_files`；
而它们没有基线 ⇒ 每调一次甩一句 `collect_changes:no_baseline_ref`。
**两个 key 合起来占全部告警的 47%**，把真事故盖住。

⚠️ **假告警比没有告警更坏**：它训练人忽略这个页面 —— 这正是本仓栽过的那条
（`lazy_spoke` 报了 43 次没人看）。

## 两处缺一不可

1. `openai_agent._track()`：`no_tools` ⇒ **不追**（这里钉的就是它）
2. `validator` 那四个评审/QA 路口**显式传 `no_tools=True`**（不传的话 ① 永远不生效）

⚠️ 能这么写的前提是 `_dispatch_exec._honors_no_tools` 那条已经落实（`a4f57d8`）：
**拦不住禁工具的执行器根本不会跑** ⇒ "跑到这里 = 确实没碰过磁盘"成立。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor  # noqa: E402


def _ex(**cfg):
    c = {"model": "m", "api_key_env": "K"}
    c.update(cfg)
    e = OpenAIAgentExecutor(c, "评审任务", "t_track", cwd=".")
    e._api_key = "k"
    return e


def test_no_tools时不追改动(monkeypatch):
    """**正题**：`no_tools` ⇒ `_track()` 不许去碰 `_track_changed_files`。"""
    e = _ex(no_tools=True)
    called = []
    monkeypatch.setattr(e, "_track_changed_files", lambda: called.append(1))
    e._track()
    assert called == [], "no_tools 还去追改动了 —— 它没有写工具，追出来的只会是假警"


def test_对照_有工具时照常追(monkeypatch):
    """**对照**：没开 `no_tools` 的照常追 —— 别把修法改宽成"一律不追"。"""
    e = _ex()
    called = []
    monkeypatch.setattr(e, "_track_changed_files", lambda: called.append(1))
    e._track()
    assert called == [1], "普通执行器不许停止追改动"


def test_接线_三个出口都走_track(monkeypatch):
    """**接线**：`_track_changed_files` 的三个调用点都得改成 `_track`。

    ⚠️ 只测 `_track()` 函数是**不够的** —— 出口那三处若还直接调
    `_track_changed_files()`，`no_tools` 这条守则一次都不会生效（典型"函数对、接线不通"）。
    """
    import re
    from singularity.scheduler.executors import openai_agent as oa
    src = Path(oa.__file__).read_text(encoding="utf-8")
    # `_track` 自己的函数体里那一句是正当的，先摘掉再数
    body = src[src.index("def _track(self):"):src.index("def _track_changed_files(self):")]
    rest = src.replace(body, "")
    direct = len(re.findall(r"self\._track_changed_files\(\)", rest))
    assert direct == 0, (
        f"还有 {direct} 个出口直接调 `_track_changed_files()` —— "
        f"`no_tools` 那条在这些出口上一次都不生效")
    assert len(re.findall(r"self\._track\(\)", rest)) >= 3, "出口没走 `_track`"
