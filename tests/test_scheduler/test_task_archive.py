"""任务收尾的三件归档，**两条收尾路径都必须做**。

  · `TaskRunner.finalize`            —— 单任务直接合并
  · `orchestrator._drain_pending`    —— v3 并行，任务走合并队列，合并完才收尾

实测（2026-09-11 真机验证）：跑完一个任务，`experiences.json` / `token_usage.json`
**根本没被创建**，`route_learner.json` 一动不动。而 `events.json` 正常长大 ——
因为 `_save_trace` 两条路径都有，从外面看像是"归档跑了"，其实只跑了一半。
"""
import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker


class _MR:
    """假 MergeResult。"""
    def __init__(self, task_id, status="merged"):
        self.task_id = task_id
        self.status = status
        self.new_head = "abcdef123456"
        self.conflict_files = []
        self.reason = ""


class _MQ:
    def __init__(self, results):
        self._results = results

    def drain(self):
        return self._results


def _setup(monkeypatch, tmp_path):
    from singularity.scheduler import config
    import singularity.scheduler._task_runner as tr
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(orch, "tracker", tracker)
    monkeypatch.setattr(orch, "_maybe_complete_parents", lambda *a: None)
    monkeypatch.setattr(orch, "_release_ref", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)

    called = []
    monkeypatch.setattr(tr.mem_mod, "archive_experience",
                        lambda **k: called.append("experience"))
    monkeypatch.setattr(tr, "record_tokens", lambda **k: called.append("tokens"))
    monkeypatch.setattr(tr.rl_mod, "save_learner", lambda *a: called.append("learner"))
    return called


def _batch():
    """**用真的 `BatchOutput`，不用 SimpleNamespace 替身**（2026-09-13 改）。

    替身是手搭的，于是"真实类型上有的字段"它没有 —— `_save_trace` 加了
    `tool_events=batch.tool_events` 之后，这种替身当场 `AttributeError`，
    而**生产上不可能缺这个字段**。这正是 postmortem 模板里点名的
    "手动构造对象挂载，而非走真实加载"：替身不会跟着真实类型一起长大。
    """
    from singularity.scheduler._types import BatchOutput
    return BatchOutput(ok=False, task_id="")


def test_drain_pending_archives_all_three(monkeypatch, tmp_path):
    """合并成功后，三件归档一件都不能少。

    旧代码在 _drain_pending 里自己重写了收尾（transition + _save_trace），
    完全没做这三件 —— 所以这条测试在旧代码上会因为 called 是空的而红。
    """
    called = _setup(monkeypatch, tmp_path)
    t = tracker.create("测试任务：合并路径收尾")
    tracker.transition(t.id, tracker.TaskStatus.DONE)

    pending = {t.id: (tracker.read_task(t.id), None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id)]), [])

    assert sorted(called) == ["experience", "learner", "tokens"], called


def test_drain_pending_conflict_also_archives(monkeypatch, tmp_path):
    """冲突也是任务的终态，同样要归档（failure_mode 记冲突原因）。"""
    called = _setup(monkeypatch, tmp_path)
    t = tracker.create("测试任务：冲突收尾")

    pending = {t.id: (tracker.read_task(t.id), None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id, status="conflict")]), [])

    assert sorted(called) == ["experience", "learner", "tokens"], called


# ═══════════════════════════════════════════════════════════════
# 异常收尾也要留"已知事实"（2026-09-13）
# ═══════════════════════════════════════════════════════════════
# 数出来的三个缺口里，这条覆盖两个：
#   · worker 异常：原来 `_save_trace(t, route, snap, None, None, False)` ⇒ 全无
#   · 取消路径：BatchOutput 手里有 tool_events，却没有 dispatch_result ⇒ 攥着也丢

def test_report_keeps_tool_events_without_executor_result():
    """`executor_result` 是 None 时，**手里那份 tool_events 不能白攥着**。

    取消路径就是这个形状（`_check_cancelled` 造的 BatchOutput 有事件、没 dispatch_result）。
    修之前 `to_dict()` 里那句 `getattr(None, "tool_events", [])` 拿空列表 ⇒ turns 恒 0。
    """
    from singularity.scheduler import neijinglu as nj
    from singularity.scheduler.router import RouteResult
    from singularity.scheduler.snapshot import Snapshot
    ev = [{"kind": "tool:start", "tool": "read_file", "turn": 1},
          {"kind": "tool:done", "tool": "read_file", "turn": 1},
          {"kind": "tool:start", "tool": "write_file", "turn": 2}]
    r = nj.build_report(task="t", route=RouteResult(gate_required=False, task_type="default"),
                        executor_result=None, validation=None,
                        snapshot=Snapshot(id="s", method="git", ref="r", created_at=0.0),
                        tool_events=ev)
    tb = r.to_dict()["tool_batches"]
    assert tb["turns"] == 2 and tb["total_calls"] == 2, tb


def test_report_turns_zero_when_nothing_to_show():
    """没有事件时仍然是 0 —— 兜底不许把"没有"编成"有"。"""
    from singularity.scheduler import neijinglu as nj
    from singularity.scheduler.router import RouteResult
    from singularity.scheduler.snapshot import Snapshot
    r = nj.build_report(task="t", route=RouteResult(gate_required=False, task_type="default"),
                        executor_result=None, validation=None,
                        snapshot=Snapshot(id="s", method="git", ref="r", created_at=0.0))
    assert r.to_dict()["tool_batches"]["turns"] == 0


def test_worker_exception_salvages_and_accounts(monkeypatch, tmp_path):
    """worker 抛异常时，**不许**再走"传 None"那条 —— 要抢救事实 + 记账。

    这条钉的是分支接线：`_salvage_timed_out` 的返回值必须真的传给 `_save_trace`
    （而不是 None），且 `_account_salvaged` 必须被调用。修复前两者都不发生。
    """
    import concurrent.futures as cf
    import time as _time

    called = _setup(monkeypatch, tmp_path)
    t = tracker.create("测试任务：worker 异常")

    sentinel = object()
    monkeypatch.setattr(orch, "_salvage_timed_out", lambda *a, **k: sentinel)
    traces = []
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: traces.append(a))
    accounted = []
    monkeypatch.setattr(orch, "_account_salvaged", lambda *a, **k: accounted.append(a))
    monkeypatch.setattr(orch, "cleanup_task_artifacts", lambda *a, **k: None)

    def _boom():
        raise RuntimeError("worker 炸了")

    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_boom)
        _time.sleep(0.05)          # 让它真的结束（_reap_futures 先 wait 再收）
        orch._reap_futures({fut: (t, None, None, None, _time.time())}, {}, None, None, [])
    finally:
        ex.shutdown(wait=False)

    assert traces, "worker 异常时没写 trace"
    assert traces[0][3] is sentinel, "又把 None 传给了 _save_trace —— 事实全丢"
    assert accounted, "worker 异常这条没记账（钱和经验都不会落）"
