"""「一次都没派发过 ⇒ 回 PENDING 重排」—— `_task_runner.requeue_if_never_dispatched`。

来历（2026-09-21 `round-20260921c` 真机，用户拍板）：并发只有 2、任务又互相依赖，
**排在后面的任务，900 秒有效期在排队时就被等没了** —— 那轮 11 个任务里 8 个失败，
其中 **3 个一次模型调用都没发起过**，却被判到终态；T5 的判词甚至是 QA 的
「无文件改动；检测到 1 个偷懒信号」，**读起来像它偷懒，其实它没轮到**。

实测时刻（不是推测）：T8 死在 **18:47:08**，而 T6 最后一次派发是 **18:47:07**
—— 它是**在别人让出位子后 1 秒被砍的**；同形还有 T5@18:33:36、T10/T11@19:00:39。

钉五条 + 一条接线。**每条都问过"改掉哪一行它会红"**：
  ① 从没派发 ⇒ PENDING（把判据删掉 ⇒ 红）
  ② 派发过 ⇒ 照旧 FAILED（把这条去掉 ⇒ 红；这是最危险的那半边：把"做过没成"
     也放回队列，就是拿排队问题掩盖真失败）
  ③ 到上限 ⇒ 判 FAILED，不再转（把 cap 去掉 ⇒ 红）
  ④ 探测抛异常 ⇒ 保守不改判定（把 except 改成 return True ⇒ 红）
  ⑤ 接线：QA 判 fail 那条路**真的走了它**（把调用删掉 ⇒ 红；函数对 ≠ 接线通）
"""
import json

import pytest

from singularity.scheduler import config, tracker
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler._task_runner import requeue_if_never_dispatched


def _mk_task(rc=0, cap=3):
    """`max_retries` 走 `transition` 设 —— 它是 Task 的真字段，
    直接改对象再自己落盘绕过了 `_apply_attrs` 那套（会漏掉"不认的键要留痕"）。"""
    t = tracker.create("[T1] 实现某模块: 创建 x.py", depth=0)
    tracker.transition(t.id, TaskStatus.PENDING, retry_count=rc, max_retries=cap)
    return tracker.read_task(t.id)


def _mark_dispatched(task_id):
    """把"进过 dispatch"这件事落到盘上（生产里是 `_exec._mark_dispatch_started`）。"""
    config.ensure_dirs()
    (config.PARTIAL_USAGE_DIR / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "started_at": 1.0}), encoding="utf-8")


def test_从没派发过_回PENDING而不是判死(tmp_path):
    t = _mk_task(rc=0)
    assert requeue_if_never_dispatched(t) is True
    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.PENDING, "没轮到 ≠ 失败：不该落终态"
    assert after.retry_count == 1, "重排次数没 +1 ⇒ 上限永远够不着，会无限转"
    assert "未轮到" in after.error


def test_派发过的照旧判终态(tmp_path):
    """🔴 最危险的那半边：派发过 = 真的做过但没成，那是真失败，不许放回队列。"""
    t = _mk_task(rc=0)
    _mark_dispatched(t.id)
    assert requeue_if_never_dispatched(t) is False
    assert tracker.read_task(t.id).status == TaskStatus.PENDING, "本函数没动它"


def test_到上限就不再转(tmp_path):
    """防无限循环：rc 已经到 max_retries ⇒ 握手，判终态由调用方做。"""
    t = _mk_task(rc=3, cap=3)
    assert requeue_if_never_dispatched(t) is False
    assert tracker.read_task(t.id).retry_count == 3, "到上限还 +1 就等于没上限"


def test_探测本身失败时保守(tmp_path, monkeypatch):
    """判据失效 ⇒ **默认行为必须是最保守的那一边**（照旧判终态），
    不能因为探不出来就把任务放回队列转圈。"""
    from singularity.scheduler import _task_runner as tr
    monkeypatch.setattr(tr, "read_partial_started_at",
                        lambda _tid: (_ for _ in ()).throw(OSError("盘挂了")))
    t = _mk_task(rc=0)
    assert requeue_if_never_dispatched(t) is False


def test_接线_QA判fail那一支里真的有这个调用():
    """**函数对 ≠ 接线通**。`TaskRunner.execute` 几百行、驱动不起，所以钉**调用点**：
    在 `if qa_verdict == "fail":` 这个 If 节点里必须能走到 `requeue_if_never_dispatched`。

    ⚠️ 这条是**结构测试**，它的边界要说清：它证明**那句在场且在那个分支里**，
    不证明运行时真会走到（那由上面四条行为测试 + 真机兜）。**把调用删掉，这条会红。**
    （同族先例：`test_fixes_batch3_20260914.test_质量钩子异常不许报_ok` 也是解析源码。）
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "singularity" / "scheduler"
           / "_task_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    hits = []
    for node in ast.walk(tree):
        # 找 `if qa_verdict == "fail":`
        if not isinstance(node, ast.If):
            continue
        c = node.test
        if not (isinstance(c, ast.Compare) and isinstance(c.left, ast.Name)
                and c.left.id == "qa_verdict"
                and any(isinstance(x, ast.Constant) and x.value == "fail" for x in c.comparators)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "requeue_if_never_dispatched"):
                hits.append(sub.lineno)

    assert hits, "QA 判 fail 那一支里没有 requeue_if_never_dispatched —— 判据接不上线"


def test_接线_收割那条路_真驱动一遍(tmp_path, monkeypatch):
    """`orchestrator._reap_futures` 的超时支 —— **真跑那个函数**，不是读源码。

    同时钉住**顺序**：重排必须挡在"写超时标记"之前。给一个压根没起来的任务留
    `by:timeout` 标记，下一轮重排上来会被 `_exec._check_cancelled` 读成"被用户取消了"
    —— 白跑一次，而且归因是错的（和 09-19 那条"我们自己的超时被记成用户取消"同族）。
    """
    import time as _t
    from singularity.scheduler import orchestrator as orch

    monkeypatch.setattr(orch, "wait", lambda *a, **k: None)   # 别真等 10 秒

    t = _mk_task(rc=0)

    class _Fut:
        def done(self): return False
        def cancel(self): return True

    fut = _Fut()
    running = {fut: (t, None, None, None, _t.time() - 10_000)}
    results = []

    orch._reap_futures(running, {}, None, None, results)

    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.PENDING, "没轮到的任务在收割那条路上被判死了"
    assert any(r[1] == "requeue_never_dispatched" for r in results), results
    assert not (config.CANCEL_DIR / f"{t.id}.json").exists(), \
        "重排的任务被留了超时标记 —— 下一轮上来会被读成『用户取消了』"


def test_接线_收割那条路_派发过的照旧判死(tmp_path, monkeypatch):
    """同一支的反例：派发过的任务，收割照旧走 FAILED（别把真失败也放回队列）。"""
    import time as _t
    from singularity.scheduler import orchestrator as orch

    monkeypatch.setattr(orch, "wait", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_account_salvaged", lambda *a, **k: None)

    t = _mk_task(rc=0)
    _mark_dispatched(t.id)

    class _Fut:
        def done(self): return False
        def cancel(self): return True

    results = []
    orch._reap_futures({_Fut(): (t, None, None, None, _t.time() - 10_000)}, {}, None, None, results)
    assert tracker.read_task(t.id).status == TaskStatus.FAILED
