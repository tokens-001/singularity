"""QA 判**中间态**（retry/escalate/block）⇒ 重排**必须有上限**。

来历（2026-10-01 `round-20261001`，那天最贵的一件东西）：
`_task_runner.finalize` 那条 `elif qa_verdict != "pass"` 分支**只把 `retry_count`
+1、没有任何人读它** —— 两处上限（兄弟函数 `requeue_if_never_dispatched`、
`else` 里的"重试耗尽→自动拆分"）**都长在别的路上**，这条路一个出口都碰不到。

真机形状：T4 判 `escalate` ⇒ 转 PENDING ⇒ 再跑 ⇒ 又 `escalate` …… **20 次派发、
186 万 token**，`retry_count` 涨到 **4（已 > `max_retries=3`）仍然被重派**，
一直到手工杀后端才停。对照 T3：判的是 `fail`，走另一支，**当场就 FAILED 停住了**。

⚠️ 最刺眼的是那段注释自己的原话："`retry_count` 必须在这里 +1 …… 于是
'重试耗尽 → 自动拆分再提交'那条分支永远进不去" —— 可那条分支在 `else` 里：
**生产者把值写上了，消费者长在另一条路上。**

这组用例**真驱动 `finalize`**（不是读源码）—— 判据是"终态是什么"，
而终态只有真跑那条分支才看得出来。
"""
from types import SimpleNamespace as NS

import pytest

from singularity.scheduler import _task_runner as tr
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


def _mk_task(rc=0, cap=3):
    t = tracker.create("[T1] 实现某模块: 创建 x.py", depth=0)
    tracker.transition(t.id, TaskStatus.PENDING, retry_count=rc, max_retries=cap)
    return tracker.read_task(t.id)


def _batch(verdict, issues=("质量门禁**未执行**：不是通过",)):
    """够 `finalize` 走完的最小假 batch（qa_verdict 已设 ⇒ 不会去跑真的 supervise）。"""
    er = NS(changed_files=["x.py"], raw_output="", tool_events=[], token_count=1)
    return NS(validation=NS(action="retry"), term_reason="soft_quality_gate",
              planner_decomposed=False, ok=True, dispatch_result=NS(executor_result=er),
              qa_verdict=verdict, qa_issues=list(issues), tool_events=[], turn_count=0,
              pre_search_skipped=False, pre_search_reason="", pre_search_top_decisions=[],
              pre_search_memory={})


@pytest.fixture(autouse=True)
def _no_chancellor(monkeypatch):
    """Chancellor 会真发报告 —— 这组用例不测它。"""
    monkeypatch.setattr(tr.chan_mod, "assess",
                        lambda *a, **k: NS(severity="info", task_ids=[]))


def test_没到上限_照旧转PENDING():
    t = _mk_task(rc=0, cap=3)
    tr.TaskRunner().finalize(t, _batch("escalate"), NS(task_type=""), None, [])

    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.PENDING, "中间态还没到顶 ⇒ 该重排"
    assert after.retry_count == 1, "重排次数没 +1 ⇒ 上限永远够不着"


def test_到上限_判FAILED而不是再转PENDING():
    """**这条就是本次修的东西**：到顶还转 PENDING = 一个没有上限的黑洞。"""
    t = _mk_task(rc=3, cap=3)
    tr.TaskRunner().finalize(t, _batch("escalate"), NS(task_type=""), None, [])

    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.FAILED, "到顶又转回 PENDING ⇒ 会无限重派（T4 20 次）"
    assert "重排到顶" in after.error, "判词要说清是「到顶了」，不是普通失败"


def test_上限边界_第cap次还能转第cap加1次就停():
    """`rc >= cap` 才停 ⇒ 一共转 cap 次。少停一次就是"没上限"，多停一次是提前杀。"""
    t = _mk_task(rc=2, cap=3)
    assert tr.requeue_after_qa(t, "retry", []) == 3, "第 cap 次应该还能转"
    t2 = _mk_task(rc=3, cap=3)
    assert tr.requeue_after_qa(t2, "retry", []) is None, "到 cap 必须握手"
    assert tracker.read_task(t2.id).status == TaskStatus.PENDING, \
        "握手时**不许自己改状态** —— 判终态是调用方的事（同 requeue_if_never_dispatched）"


def test_判fail那一支不受影响():
    """QA 判 `fail` 走的是另一支（有自己的 `requeue_if_never_dispatched`）——
    别被这次的改动连带改坏：没派发过的照旧回 PENDING。"""
    t = _mk_task(rc=0, cap=3)
    tr.TaskRunner().finalize(t, _batch("fail"), NS(task_type=""), None, [])

    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.PENDING, "没轮到过的任务不该被判死"
    assert "未轮到" in after.error
