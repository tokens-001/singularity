"""文档事实钉测试 —— 把「每轮外派/评审都要人肉重核一遍」的事实钉住。

## 为什么有它

2026-09-14 的评审-02b 花了大半篇篇幅**手工核**这些事实：默认并发是几、`FIXING` 删没删、
`run_phase` 认不认识 INTEGRATING。**它们都是一行能测的东西。**

而外派材料恰恰是因为**没核**，写出了一条假前提（"`run_phase` 不认识 INTEGRATING / DELIVERING"）
—— 代码 2026-09-11（`a41f590`）就修了，但 `docs/生产流现状.md:71` 没改、`project.py` 的
`set_phase` docstring 也没改，材料照抄了文档 ⇒ **三个地方互相抄一份过时的说法**。

⇒ **钉住了，下一轮的材料和评审就不用再核；漂了，这里先红。**
   文档也是状态，也会漂，漂了同样没人知道 —— 见 `docs/架构评审-02b-20260914.md` 第〇节。

## 往里加什么

文档或材料里每写下一个**可测的事实断言**，就来这儿加一条。
判据：**这个断言要是错了，会不会有人照着它做出错误决定？** 会 —— 就值得钉。

⚠️ **别钉"偏好"**（比如"不许引入数据库"）—— 会挡住合法的演进，测试会变成绊脚石。

## ⛔ 试过、**失败**的一条路（别再试）

我原本还写了「扫 `docs/*.md`，禁止出现『run_phase 不认识某阶段』」——
**立不住，删了。** 它分不清**"断言 X"和"引用 X 来反驳"**：
先扫出 `架构评审-*.md`（**正在反驳**这句）、`演化史.md`（"**以前**不认识"，过去时，是对的）；
缩到只扫 `docs/生产流现状.md` 后，又扫出**我自己刚写的那段纠正说明**（解释问题时必须引用它）。
**只要句子里出现过这些词，文本形状就判不了它是在断言还是在引用。**
—— 和 §64 那条"形状判语义，精确率和召回率会同时烂"是同一个坑。
⇒ **文档侧的钉法只有一条**：把文档里可测的断言**改写成对代码的断言**（本文件那三条就是）。

**顺带**：`docs/生产流现状.md` 的那两处过时说法已**手工改掉**
（外加 `docs/演化史.md:206` 是过去时、本来就对，不用动）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from singularity.scheduler import orchestrator, workflow
from singularity.scheduler.project import Phase

_REPO = Path(__file__).resolve().parents[2]
_DOCS = _REPO / "docs"


def _run_phase_分支提到的阶段() -> set[str]:
    """从 `workflow.run_phase` 源码里取出 if/elif 链上显式提到的 `Phase.X` 名字。

    只走**链**（`orelse` 里恰好一个 `If` 才算 elif），不深入分支体 ——
    分支体里也有 `Phase.X`（如 `set_phase(Phase.GATE3, ...)`），那是别的用途，不是派发。
    """
    tree = ast.parse(Path(workflow.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_phase")

    # ⚠️ 派发链裹在一个 `while True:` 里（每轮重读 project.phase），**不是**函数体第一层 ——
    # 别只翻 fn.body，会 StopIteration。
    def _提到_Phase(test: ast.expr) -> bool:
        return any(isinstance(s, ast.Attribute) and isinstance(s.value, ast.Name)
                   and s.value.id == "Phase" for s in ast.walk(test))

    node = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and _提到_Phase(n.test))
    names: set[str] = set()
    while node is not None:
        for sub in ast.walk(node.test):
            if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                    and sub.value.id == "Phase"):
                names.add(sub.attr)
        node = (node.orelse[0]
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)
                else None)
    return names


def test_run_phase_为每个_Phase_都写了分支():
    """`run_phase` 的派发链必须覆盖 `Phase` 枚举全集，否则会掉进 else 报「未知 phase」。"""
    missing = sorted(p.name for p in Phase if p.name not in _run_phase_分支提到的阶段())
    assert not missing, (
        f"run_phase 的 if/elif 链没有覆盖这些阶段：{missing} —— 它们会掉进 else 报「未知 phase」。\n"
        "⚠️ 2026-09-11 之前漏的正是 INTEGRATING / DELIVERING，而外派材料照抄了没更新的\n"
        "docs/生产流现状.md，把「已修」写成了「没修」⇒ 一整节论证白做。\n"
        "见 docs/架构评审-02b-20260914.md 第〇节。"
    )


def test_FIXING_阶段没有被加回来():
    """`FIXING` 2026-06-27（`d3c117e`）删掉：全仓无人给它赋值，状态根本不可达。"""
    assert not hasattr(Phase, "FIXING"), (
        "FIXING 又回来了。它 2026-06-27 被删是因为**全仓无人赋值**（状态不可达）；\n"
        "要加回来，先确认有地方会把它置上 —— 否则只是又造一个到不了的阶段。\n"
        "（外派材料 8.2 那张表至今还列着「run_phase 认识 FIXING」，与现实正好相反。）"
    )


def test_run_queue_默认并发仍是_1():
    """跑任务的并发默认是 1 —— 别再和「集成合并专用池」的 2 混起来。"""
    default = inspect.signature(orchestrator.run_queue).parameters["max_concurrent"].default
    assert default == 1, (
        f"run_queue 的默认并发变成 {default} 了（原来 1）。\n"
        "⚠️ 外派材料 8.1 写「实测用 2」，而 `max_workers=2` 是**集成合并专用池**的\n"
        "（`orchestrator._get_merge_executor`，thread_name_prefix=\"integrate\"）——\n"
        "这两个数被混过一次，还进了 §46 / §67 的算术。真要改，请把那些推理一起核。"
    )
