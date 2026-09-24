"""`cas(→DISPATCHED)` 到 `pool.submit` 之间那个窗口（2026-09-25）。

Qoder 外审第二轮 #5（`docs/Qoder-审查-20260925-第二轮.md`），我核过。

`_dispatch_ready` 里的顺序是：
  `cas(ROUTED→DISPATCHED)` 成功 → `snap_mod.take(...)` → `transition(RUNNING)`
  → `try: pool.submit(...)`
而那个 `try` **从 submit 前一行才开始** ⇒ 中间两步一抛，任务就留在 `DISPATCHED`。
`DISPATCHED ∈ _INFLIGHT 但 ∉ _SCHEDULABLE` ⇒ `ready_tasks` 不返回它、又没有 future
⇒ **900s 收割够不着**；而当时的孤儿探测器 `_warn_orphan_running` **只认 `RUNNING`**
⇒ 一声不出，只能等进程重启才被 `recover()` 捞。

`DISPATCHED` 本来就是个过路态（同一个函数里紧接着就转 RUNNING）——
所以"还停在这儿"必然意味着没人管，判它孤儿不会误报。

两处一起修：`_strand_guard` 认 `DISPATCHED`（转 FAILED），
`_warn_orphan_running` 的状态面从"写死 RUNNING"改成 `_INFLIGHT − _SCHEDULABLE`
（**`ROUTED` 必须留在外面** —— 它是"等着被派"的正常停留态，算了会天天误报）。
"""
import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import tracker


# ═══════════════════════════════════════════════════════════════
# ① `_strand_guard` 认 DISPATCHED
# ═══════════════════════════════════════════════════════════════

def _task_in(status):
    t = tracker.create("派发窗口测试")
    if status != tracker.TaskStatus.PENDING:
        tracker.transition(t.id, status)
    return tracker.read_task(t.id)


def test_DISPATCHED_的任务会被兜底转成FAILED():
    """**正题**：`cas` 之后那两步抛了，任务就停在这儿 —— 没有人会再来管它。

    变异：把 `_strand_guard` 的条件改回 `== TaskStatus.RUNNING` ⇒ 这条红。
    """
    t = _task_in(tracker.TaskStatus.DISPATCHED)

    orch._strand_guard(t, RuntimeError("快照目录不在"), "dispatch",
                       detail="派发时快照/状态切换失败")

    assert tracker.read_task(t.id).status == tracker.TaskStatus.FAILED, \
        "停在 DISPATCHED 没人管 —— 它 ∈ _INFLIGHT 但 ∉ _SCHEDULABLE，收割够不着"


def test_对照_ROUTED_不许被兜底动():
    """**对照**：`ROUTED` 是"等着被派"的正常停留态，`ready_tasks` 下一轮就会捡它。

    把它一起兜成 FAILED，等于**把排队中的任务杀掉**——比不兜更坏。
    """
    t = _task_in(tracker.TaskStatus.ROUTED)

    orch._strand_guard(t, RuntimeError("x"), "dispatch")

    assert tracker.read_task(t.id).status == tracker.TaskStatus.ROUTED


def test_对照_已经到终态的绝不许覆盖():
    """`finalize` 可能已经把它推到 DONE/FAILED 了 —— 覆盖会丢掉真实终态。"""
    t = _task_in(tracker.TaskStatus.DONE)

    orch._strand_guard(t, RuntimeError("x"), "dispatch")

    assert tracker.read_task(t.id).status == tracker.TaskStatus.DONE


# ═══════════════════════════════════════════════════════════════
# ② 孤儿探测器的状态面
# ═══════════════════════════════════════════════════════════════

def test_孤儿状态面_ROUTED_必须在外面():
    """ROUTED 同时进 `_SCHEDULABLE` —— 它是正常的，算孤儿会天天误报。"""
    assert tracker.TaskStatus.ROUTED.value not in orch._ORPHAN_STATUS_VALUES
    assert tracker.TaskStatus.DISPATCHED.value in orch._ORPHAN_STATUS_VALUES
    assert tracker.TaskStatus.RUNNING.value in orch._ORPHAN_STATUS_VALUES


def test_不在活任务表里的_DISPATCHED_必须出声(monkeypatch):
    """**接线**：探测器真扫到一条没人管的 DISPATCHED 时要报。
    变异：状态判据改回 `== RUNNING.value` ⇒ 这条红。
    """
    seen = []
    monkeypatch.setattr(orch.witness, "warn", lambda *a, **k: seen.append(k.get("key")))
    t = _task_in(tracker.TaskStatus.DISPATCHED)

    orch._warn_orphan_running({}, {})

    assert "orphan_running_task" in seen, f"没人管的 DISPATCHED 没出声：{seen}"


def test_对照_不在活任务表里的_ROUTED_不许出声(monkeypatch):
    """**对照组**：同一份盘上放一条 `ROUTED` —— 它是正常的，**必须一声不出**。"""
    seen = []
    monkeypatch.setattr(orch.witness, "warn", lambda *a, **k: seen.append(k.get("key")))
    _task_in(tracker.TaskStatus.ROUTED)

    orch._warn_orphan_running({}, {})

    assert "orphan_running_task" not in seen, f"正常排队中的任务被报成孤儿：{seen}"


# ═══════════════════════════════════════════════════════════════
# ③ 接线：`_dispatch_ready` 里那两步真的被兜住了
# ═══════════════════════════════════════════════════════════════

def test_接线_派发时快照抛了_任务不许留在DISPATCHED(monkeypatch):
    """**这条钉接线**（也是这个 bug 的真症状）：`cas` 之后 `snap_mod.take` 抛。

    只测 `_strand_guard` 验的是"函数对"；这里走的是**真的派发入口** ——
    那个 `try` 有没有把那两步包进去，只有这条能验。
    变异：把 `try/except` 去掉（退回原样）⇒ 这条红（任务留在 DISPATCHED）。
    """
    t = tracker.create("快照会炸的任务")

    def _boom(*a, **k):
        raise RuntimeError("快照目录不在")

    monkeypatch.setattr(orch.snap_mod, "take", _boom)

    class _Pool:
        def submit(self, *a, **k):        # 不该走到这儿
            raise AssertionError("take 都炸了还去 submit")

    orch._dispatch_ready({}, _Pool(), {}, None, {}, None)

    assert tracker.read_task(t.id).status == tracker.TaskStatus.FAILED, \
        "任务留在 DISPATCHED —— 没人派它、没人收割它，只能等进程重启"
