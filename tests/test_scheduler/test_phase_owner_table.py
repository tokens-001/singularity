"""「哪个阶段归谁推」是**数据**，而且钉在代码上（2026-09-19）。

09-18 那条记的是：这条约定"只活在 `_api_projects.py` 的一句注释里"。
09-19 立了 `project.py` 顶部的权威表，但**它还是注释**，而 `_api_projects`
里手抄了一份 `_LOOP_OWNED_PHASES` 元组 —— 同一件事两个定义（§60 的形状）。
现在：表是数据（`PHASE_OWNER`）、副本删掉改成现算、**代码钉在表上**。

为什么值得有守卫：那段注释自己写着 ——「改任何一档的归属前，先问：换掉之后，
还有人推它吗？**没人推 = 项目无声停住**」。而加档位 / 改归属时没人提醒：
`web/app.py` 那"第三套"推进逻辑就是这么长出来的，它把项目从 EXECUTING 直接
送进 GATE3，**跳过了 INTEGRATING 那道门**。

变异验证（各能掐红）：
  · 给 `_advance_project` 加一档（表没改）→ 第 3 条红；
  · 从 `PHASE_OWNER` 里去掉一档（代码还在认）→ 第 1、3 条红；
  · 新增一个 `Phase` 成员不声明归属 → 第 1 条红。
"""
import inspect
import re

from singularity.scheduler import orchestrator as orch
from singularity.scheduler.project import (
    OWNER_NONE,
    OWNER_RUN_PHASE,
    OWNER_SCHEDULER_LOOP,
    PHASE_OWNER,
    Phase,
)


def test_每一档都声明了归属():
    """加新档位时必须先回答"谁推它" —— 没声明就红，别让它悄悄落在没人推的地方。"""
    missing = sorted(p.value for p in Phase if p not in PHASE_OWNER)
    assert not missing, f"这些档位没声明归属（新加的？）：{missing}"


def test_表里没有已删掉的档位():
    assert set(PHASE_OWNER) == set(Phase)


def test_归属只有三种():
    assert set(PHASE_OWNER.values()) == {OWNER_SCHEDULER_LOOP, OWNER_RUN_PHASE, OWNER_NONE}


def test_调度循环实际认的档_和表一致():
    """**把表钉在代码上** —— 这条才是重点。

    只测"表自己长得对"等于没测：表对不对，取决于**代码认不认它**。
    这里从 `_advance_project` 的源码里把 `proj.phase.value == "…"` 抠出来，
    跟表里声明归循环的那几档对比，**两个方向都要一致**：
      · 代码多认一档（表没写）⇒ 红；
      · 表里写归循环、代码不认 ⇒ 红（那一档就是"没人推"）。
    """
    src = inspect.getsource(orch._advance_project)
    handled = set(re.findall(r'proj\.phase\.value == "(\w+)"', src))
    declared = {p.value for p, o in PHASE_OWNER.items() if o == OWNER_SCHEDULER_LOOP}

    assert handled == declared, (
        f"表说 {sorted(declared)}，`_advance_project` 实际认 {sorted(handled)} —— "
        f"有一边忘了改（没人推的那一档 = 项目无声停住）")
