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


# ═══════════════════════════════════════════════════════════════
# 「任务标成 running 却没人管」—— 三件防护（2026-09-13 真机抓到）
# ═══════════════════════════════════════════════════════════════
# 真机症状：`1789239155520` 被标 RUNNING + 拍了快照，之后**一个字节没跑**
# （0 日志 / 无 sidecar / 无心跳 / 无取消标记 / 无 trace），过了 1100 秒**收割也没触发**。
# py-spy 栈给了两条硬证据：池子里**没有工作线程**、循环空转到 `time.sleep(3)`。
# ⇒ 它被标成 RUNNING 了，但 future **从来没登记进 running_futures** ⇒ 被孤立。

class _Runner:
    """假 runner：`_dispatch_ready` 会取 `runner.execute` 当提交参数。"""
    def execute(self, *a, **k):
        raise AssertionError("假 runner 不该真的被执行")


class _BoomPool:
    """提交必炸的假池子。"""
    def submit(self, *a, **k):
        raise RuntimeError("cannot schedule new futures after shutdown")


def _patch_dispatch_env(monkeypatch, tmp_path):
    from singularity.scheduler import config, tracker as tr
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tr.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(orch, "tracker", tr)
    monkeypatch.setattr(orch.router_mod, "route",
                        lambda d: type("R", (), {"gate_required": False, "task_type": "default"})())
    monkeypatch.setattr(orch.pre_mod, "pre_search",
                        lambda *a, **k: type("P", (), {
                            "code_context": "", "skipped": True, "reason": "",
                            "top_decisions": [], "memory": None})())
    monkeypatch.setattr(orch.pre_mod, "apply_escalation", lambda *a: None)
    monkeypatch.setattr(orch.snap_mod, "take",
                        lambda *a, **k: type("S", (), {"id": "s1", "method": "git", "ref": "r1"})())
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: tmp_path)
    return tr


def test_dispatch_failure_leaves_no_orphan_running_task(monkeypatch, tmp_path):
    """`transition(RUNNING)` 之后 `pool.submit` 炸了 → 任务必须变 FAILED，**不许留在 RUNNING**。

    留在 RUNNING 就是孤儿：没有 future ⇒ 循环看不见它 ⇒ `ready_tasks()` 也不返回它
    ⇒ 900s 收割永远够不着。这正是 2026-09-13 真机那个凭空少掉的任务。
    """
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    warns = []
    monkeypatch.setattr(orch.witness, "warn", lambda scope, msg, **kw: warns.append(msg))

    t = tr.create("孤儿测试：提交必炸")
    orch._dispatch_ready(set(), _BoomPool(), {}, _Runner(), {}, None)

    fresh = tr.read_task(t.id)
    assert fresh.status == tr.TaskStatus.FAILED, (
        f"任务被留在 {fresh.status} —— 没有 future，永远没人收割它")
    assert "派发失败" in (fresh.error or ""), fresh.error
    assert any("dispatch_failed" in w for w in warns), warns


class _OkPool:
    """提交成功的假池子：返回一个永不完成的假 future。"""
    def submit(self, *a, **k):
        return object()


def test_dispatch_success_still_registers_and_runs(monkeypatch, tmp_path):
    """**对照**：正常提交时任务照旧进 RUNNING 并被登记 —— 别把上面那条改宽了。"""
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    t = tr.create("正常派发")
    rf: dict = {}
    assert orch._dispatch_ready(set(), _OkPool(), {}, _Runner(), rf, None) is True
    assert tr.read_task(t.id).status == tr.TaskStatus.RUNNING
    assert len(rf) == 1, "future 没登记进 running_futures"


def test_orphan_running_task_is_reported_once(monkeypatch, tmp_path):
    """孤立探测：没有 future 也没人收割的 RUNNING 任务，要**报出来**（只报不改）。"""
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    warns = []
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, **kw: warns.append((msg, kw.get("key"))))
    orch._orphans_warned.clear()

    t = tr.create("孤儿")
    tr.transition(t.id, tr.TaskStatus.RUNNING)
    orch._warn_orphan_running()
    assert warns and warns[0][1] == "orphan_running_task", warns
    assert t.id in warns[0][0]

    # 幂等：这个检查每次空转都会走到，不去重会刷屏
    n = len(warns)
    orch._warn_orphan_running()
    assert len(warns) == n, "同一个孤儿被重复报"

    # 终态任务不算孤儿
    orch._orphans_warned.clear()
    tr.transition(t.id, tr.TaskStatus.FAILED)
    orch._warn_orphan_running()
    assert len(warns) == n, "终态任务被当成孤儿了"


def test_loop_error_is_persisted_not_just_pushed(monkeypatch, tmp_path):
    """循环级异常必须进**告警通道** —— 只推 SSE 的话飘一次就没了。

    2026-09-13：一次"任务被孤立"的事故**一条持久痕迹都没留**（日志没有、
    alerts.jsonl 没有），当天刚造的聚合视图也看不见它 —— 排障只能靠猜。
    """
    from singularity.web import app as webapp
    from singularity.scheduler import witness

    monkeypatch.setenv("QIDIAN_DIR", str(tmp_path))
    warns = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **kw: warns.append(msg))
    monkeypatch.setattr(webapp.time, "sleep", lambda *a: None)
    monkeypatch.setattr(webapp, "_push_event", lambda *a: None)
    monkeypatch.setattr(webapp, "_log_info", lambda *a: None)
    monkeypatch.setattr(webapp, "_sse_broadcast", lambda *a: None)
    monkeypatch.setattr(webapp.disp_mod, "load_agents", lambda: {})
    monkeypatch.setattr(webapp.tracker, "recover", lambda: 0)

    def _boom(*a, **k):
        webapp._loop_stop.set()          # 让它下一轮退出
        raise RuntimeError("boom-xyz")

    monkeypatch.setattr(webapp.orchestrator, "run_queue", _boom)
    webapp._loop_stop.clear()
    webapp._loop_worker()

    assert any("loop_error" in w and "boom-xyz" in w for w in warns), (
        f"循环异常没进告警通道: {warns}")


def test_run_queue_calls_orphan_check_at_break(monkeypatch, tmp_path):
    """**接线**测试：走到"没活干"那一刻，必须真的去查孤儿。

    上面那条测的是**函数**（直接调 `_warn_orphan_running()`）—— 函数对不等于接线通。
    2026-09-13 这条是变异验证逼出来的：把 `_run_queue_v3` 里那句调用删掉，
    上面那条照样绿。
    """
    _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "_auto_trigger_test_fix", lambda *a: None)
    called = []
    monkeypatch.setattr(orch, "_warn_orphan_running", lambda: called.append(1))

    orch._run_queue_v3({}, 1)      # 队列空 → 立刻走到 break 那一支

    assert called, "空转到退出时没查孤儿 —— 探测没接上"


# ═══════════════════════════════════════════════════════════════
# §65 那条形状，全仓扫出另外 4 处（2026-09-13）
# ═══════════════════════════════════════════════════════════════
# "future / batch **已经消费掉**、后续那步却抛了" —— 任务不在任何人的视野里，
# 循环会当成"没活干"退出，它就永远停在 RUNNING。

class _DoneFuture:
    """一个"已完成"的 future。"""
    def __init__(self, batch):
        self._b = batch
    def done(self): return True
    def result(self): return (self._b, None, None)
    def cancel(self): return False


class _SubmitBoomMQ:
    def submit(self, req): raise RuntimeError("入队炸了")
    def drain(self): return []


def test_finalize_failure_does_not_strand_task(monkeypatch, tmp_path):
    """`runner.finalize` 抛了 —— 任务不能留在 RUNNING（future 已经 pop 掉了）。"""
    import time as _t
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    warns = []
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, **kw: warns.append(kw.get("key")))

    t = tr.create("finalize 炸")
    tr.transition(t.id, tr.TaskStatus.RUNNING)

    class _R:
        def finalize(self, *a, **k):
            raise RuntimeError("finalize 炸了")

    orch._reap_futures({_DoneFuture(_batch()): (t, None, None, None, _t.time())},
                       {}, _MQ([]), _R(), [])

    assert tr.read_task(t.id).status == tr.TaskStatus.FAILED, "任务被留在 RUNNING 没人管"
    assert "finalize_failed" in warns, warns


def test_enqueue_merge_failure_does_not_strand_task(monkeypatch, tmp_path):
    """`mq.submit` 抛了 —— 同上，任务既不在 pending 也不在 future，就是孤儿。"""
    import time as _t
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    warns = []
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, **kw: warns.append(kw.get("key")))

    t = tr.create("入队炸")
    tr.transition(t.id, tr.TaskStatus.RUNNING)
    b = _batch()
    b.merge_request = object()          # 走 mq.submit 那条

    pending = {}
    orch._reap_futures({_DoneFuture(b): (t, None, None, None, _t.time())},
                       pending, _SubmitBoomMQ(), None, [])

    assert pending == {}, "没入成队却记进了 pending_batches"
    assert tr.read_task(t.id).status == tr.TaskStatus.FAILED
    assert "enqueue_merge_failed" in warns, warns


def test_drain_pending_failure_does_not_strand_task(monkeypatch, tmp_path):
    """`_drain_pending` 里抛了 —— batch 已经 pop，任务不能留在 RUNNING。"""
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_maybe_complete_parents", lambda *a: None)
    monkeypatch.setattr(orch, "_release_ref", lambda *a, **k: None)
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: tmp_path)
    warns = []
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, **kw: warns.append(kw.get("key")))

    t = tr.create("drain 炸")
    tr.transition(t.id, tr.TaskStatus.RUNNING)
    monkeypatch.setattr(tr, "read_task", lambda tid: (_ for _ in ()).throw(RuntimeError("读盘炸")))

    pending = {t.id: (t, None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id)]), [])

    assert "drain_pending_failed" in warns, warns


def test_strand_guard_only_touches_running(monkeypatch, tmp_path):
    """**对照**：只有还停在 RUNNING 的才改。

    ⚠️ 这条第一版是**假绿**：它断言"终态没被覆盖"，可那个结果是**状态机自己**
    拒绝 `done→failed` 挡下来的，跟本判据无关（变异验证抓出来的）。
    现在直接钉"`transition` 被调了几次、调在谁身上" —— 测的是**我这条判据**。
    """
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch.witness, "warn", lambda *a, **k: None)

    t_run = tr.create("还在跑的")
    tr.transition(t_run.id, tr.TaskStatus.RUNNING)
    t_done = tr.create("已经完事的")
    tr.transition(t_done.id, tr.TaskStatus.DONE)

    calls = []
    monkeypatch.setattr(orch.tracker, "transition", lambda *a, **k: calls.append(a))
    orch._strand_guard(t_run, RuntimeError("x"), "finalize")
    orch._strand_guard(t_done, RuntimeError("x"), "finalize")

    assert len(calls) == 1, f"该只改 RUNNING 那个，实际改了 {len(calls)} 个"
    assert calls[0][0] == t_run.id


def test_save_trace_keeps_tool_events_without_disp_result(monkeypatch, tmp_path):
    """**真落盘**验一遍：取消路径的 `tool_events` 要能进 trace。

    `_check_cancelled` 造的 BatchOutput 是"有 tool_events、**没有** dispatch_result"，
    而 `_save_trace` 的唯一输入本来是 `disp_result` ⇒ 事件攥着也进不了 trace
    （修复前 `tool_batches.turns` 恒 0）。

    真机上验这条**窗口很窄**（`_check_cancelled` 只在两次 dispatch 之间生效，
    任务往往已经跑完了）—— 所以这里直接把整条写盘路径走通、再读回来核对：
    比赌时机可靠，而且钉的是同一个东西。
    """
    from singularity.scheduler import config, tracker as tr
    from singularity.scheduler._exec import _save_trace
    from singularity.scheduler.router import RouteResult
    from singularity.scheduler.snapshot import Snapshot
    from singularity.scheduler import neijinglu as nj

    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    # TRACE_DIR 是 import 时算好的常量，光改 QIDIAN_DIR 不跟着动
    monkeypatch.setattr(config, "TRACE_DIR", tmp_path / "traces")
    (tmp_path / "traces").mkdir()
    monkeypatch.setattr(tr.config, "QIDIAN_DIR", tmp_path)

    t = tr.create("取消路径的 trace")
    events = [{"kind": "tool:start", "tool": "read_file", "turn": 1},
              {"kind": "tool:done", "tool": "read_file", "turn": 1},
              {"kind": "tool:start", "tool": "write_file", "turn": 2}]

    _save_trace(t, RouteResult(gate_required=False, task_type="default"),
                Snapshot(id="s", method="git", ref="r", created_at=0.0),
                None, None, False, tool_events=events)

    raw = (config.TRACE_DIR / f"{t.id}.json").read_text(encoding="utf-8")
    # 顺带走一遍 `from_dict` —— 它是 `GET /api/tasks/<id>/trace?format=md` 的唯一入口，
    # 2026-09-13 之前**恒 500**（它还在给 RouteResult 传早就删掉的 `level=`）。
    import json as _json
    on_disk = _json.loads(raw)["tool_batches"]
    assert on_disk["turns"] == 2 and on_disk["total_calls"] == 2, f"事件没进 trace: {on_disk}"

    report = nj.DeliveryReport.from_dict(_json.loads(raw))
    assert report.to_dict()["tool_batches"] == on_disk, "转一圈回来把轮次丢了（会报成 0）"
    assert nj.format_report(report), "trace 导 markdown 没产出内容"
