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


def test_drain_pending_one_failure_does_not_skip_the_rest(monkeypatch, tmp_path):
    """收尾三件**每件各自 try** —— 第一件炸了，后两件照样得跑。

    ⚠️ 原来这三件和 `transition` 挤在**同一个 try** 里 ⇒ `_release_ref` 一抛，
    `_save_trace` 和 `_archive_task_outcome` **静默全跳过**：任务状态是 DONE，
    可盘上没有 trace、账没记、经验没进记忆、路由没学习 —— 四件事一起消失，
    而外面看起来一切正常（`_strand_guard` 报的是第一件，不是被跳过的那三件）。

    `_account_salvaged` 的 docstring 早就写着"每件各自 try（跟 `_archive_task_outcome`
    同规矩）"—— 这条正常收尾路**没跟**。变异验证：把三件挪回同一个 try → 这条红。
    """
    called = _setup(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("release_ref 炸了")

    monkeypatch.setattr(orch, "_release_ref", _boom)
    warns: list[str] = []
    monkeypatch.setattr(orch.witness, "warn", lambda scope, msg, **kw: warns.append(msg))

    t = tracker.create("测试任务：收尾一件炸不该连累其余")
    tracker.transition(t.id, tracker.TaskStatus.DONE)

    pending = {t.id: (tracker.read_task(t.id), None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id)]), [])

    assert sorted(called) == ["experience", "learner", "tokens"], \
        f"第一件炸了，后面几件被静默跳过了: {called}"
    assert any("release_ref_failed" in w for w in warns), f"炸了没出声: {warns}"


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
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, **kw: warns.append((msg, kw.get("key"))))

    t = tr.create("孤儿测试：提交必炸")
    orch._dispatch_ready(set(), _BoomPool(), {}, _Runner(), {}, None)

    fresh = tr.read_task(t.id)
    assert fresh.status == tr.TaskStatus.FAILED, (
        f"任务被留在 {fresh.status} —— 没有 future，永远没人收割它")
    assert "派发失败" in (fresh.error or ""), fresh.error
    # **钉 key 而不是正文**：`alert_summary` 按 key 归并，key 才是有下游的那个；
    # 正文（`dispatch:…`）只给人看。（2026-09-13 这处从手写段改走 `_strand_guard`，
    # 正文前缀跟着统一成了 `dispatch:` —— 旧断言卡的是正文，卡错了地方。）
    assert any(k == "dispatch_failed" for _, k in warns), warns


class _OkPool:
    """提交成功的假池子：返回一个永不完成的假 future。"""
    def submit(self, *a, **k):
        return object()


def test_dispatch_failure_does_not_clobber_a_state_someone_else_set(monkeypatch, tmp_path):
    """派发那处现在也走 `_strand_guard` —— **改之前必须先看它现在是什么**。

    钉的是 2026-09-13 发现的那处差：`_dispatch_ready` 原来是**自己手写**的一段、
    不走 `_strand_guard`，而它少的**唯一**一样东西就是这道重读检查。

    ⚠️ **别用"终态没被覆盖"当判据 —— 那是假绿**：`_TERMINAL_EXIT[DONE]` 是空集，
    `done→failed` 本来就被状态机挡掉，跟本判据无关。
    （`test_strand_guard_only_touches_running` 的第一版就是这么绿的，它自己的
    docstring 记着这笔；2026-09-13 我在这里**又犯了一次**，也是变异验证抓出来的。）
    ⇒ 这里挑一个**状态机不管**的中转态：`submit` 把任务挪回 **PENDING**
    （`RUNNING→PENDING` 合法，`PENDING→FAILED` 也合法）—— 没有那道重读检查的话，
    它会**照改不误**，把一个刚被重新排队的任务打成失败。
    这就是有第二个写入者（`tests/integration/role_probe.py` 那个独立进程）时的真实风险。
    """
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch.witness, "warn", lambda *a, **k: None)
    t = tr.create("派发时被别人挪回 PENDING")

    class _PoolThatRequeuesItFirst:
        """`submit` 里先把任务挪回 PENDING（=第二个写入者重新排队），再抛。"""
        def submit(self, *a, **k):
            tr.transition(t.id, tr.TaskStatus.PENDING)
            raise RuntimeError("boom")

    orch._dispatch_ready(set(), _PoolThatRequeuesItFirst(), {}, _Runner(), {}, None)

    assert tr.read_task(t.id).status == tr.TaskStatus.PENDING, (
        "刚被别人重新排队的任务被打成了失败 —— 缺了'改之前先重读'那道检查")


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


def test_reconcile_runs_while_queue_is_busy(monkeypatch, tmp_path):
    """**接线**测试：队列**不空**的时候，周期对账也必须跑。

    原来那句写在 `if count == 0:` 里面（"空转满 100 轮"才比一遍）。而忙的时候
    `idle_ticks` **每轮被清零** ⇒ 计数永远够不到 100 ⇒ **最该对账的时候（一直在跑）
    一次都不对账**。对账的判据本来就是"从盘上重算 vs 状态说的"，和忙闲无关
    ⇒ 触发改成"每轮 + 按时间节流"（见 `docs/结构性-水位触发-清单-20260914.md` 的 A1）。

    这条钉的是**接线**：把那句的判据改成 `if False:` 会红 ——
    只测 `reconcile_projects()` 函数本身是测不出这个的（外派⑬ 报过同款假绿）。
    """
    from singularity.web import app as webapp
    from singularity.scheduler import orchestrator as orch_mod
    from singularity.scheduler import memory as mem_mod

    monkeypatch.setattr(webapp.time, "sleep", lambda *a: None)
    monkeypatch.setattr(webapp, "_push_event", lambda *a: None)
    monkeypatch.setattr(webapp, "_log_info", lambda *a: None)
    monkeypatch.setattr(webapp, "_sse_broadcast", lambda *a: None)
    monkeypatch.setattr(webapp.disp_mod, "load_agents", lambda: {})
    monkeypatch.setattr(webapp.tracker, "recover", lambda: 0)
    monkeypatch.setattr(webapp, "_RECONCILE_INTERVAL_S", 0.0)   # 让节流别挡住这一轮
    # `else`（忙）分支里的旁路全堵掉：桌面通知 / 记忆整合 / 项目推进 —— 只留被测那句
    monkeypatch.setattr(webapp.proj_mod, "recover_all", lambda: [])
    monkeypatch.setattr(mem_mod, "consolidate_memory", lambda: 0)

    class _Verdict:          # 必须带 `.action == "pass"`，否则会去调 osascript 发桌面通知
        action = "pass"

    rounds: list[int] = []

    def _busy(*a, **k):
        rounds.append(1)
        if len(rounds) >= 2:
            webapp._loop_stop.set()             # 跑两轮就退出
        return [("t1", "pass", _Verdict())]     # ⚠️ 非空 = 队列忙

    monkeypatch.setattr(webapp.orchestrator, "run_queue", _busy)
    reconciles: list[int] = []
    monkeypatch.setattr(orch_mod, "reconcile_projects",
                        lambda: reconciles.append(1) or [])

    webapp._loop_stop.clear()
    webapp._loop_worker()

    assert reconciles, "队列忙的时候没对账 —— 又退回'只在空转时才看'的边沿触发了"


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


# ═══════════════════════════════════════════════════════════════
# §59 的边界：两种"没账"要分得开（2026-09-13）
# ═══════════════════════════════════════════════════════════════
# 被 900s 收割的任务 `token_count = None` 有**两种成因**：
#   ① 压根没发起过模型调用   ② 发起了，但那一刻正在飞的调用没落盘
# 以前一律报"一次都没落盘" —— **分不出就等于没有这个信号**。

def test_partial_usage_distinguishes_never_started_from_not_persisted(monkeypatch, tmp_path):
    from singularity.scheduler import config
    from singularity.scheduler._exec import (
        _mark_dispatch_started, _persist_partial_usage,
        read_partial_started_at, read_partial_usage)

    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", tmp_path / "partial_usage")
    (tmp_path / "partial_usage").mkdir()

    assert read_partial_started_at("t1") is None, "没进过 dispatch 却说进过"

    _mark_dispatch_started("t2")
    assert read_partial_started_at("t2") is not None, "dispatch 开始了却没留痕"
    assert read_partial_usage("t2")[0] == 0, "标记不该凭空造出 token"

    # ⚠️ 累加落盘**不许把 started_at 抹掉** —— 抹了又变成"分不出"
    _persist_partial_usage("t2", "any", "m", 100)
    assert read_partial_started_at("t2") is not None, "累加落盘把'进过 dispatch'抹了"
    assert read_partial_usage("t2")[0] == 100


def test_salvage_names_which_kind_of_no_account(monkeypatch, tmp_path):
    """收尾那句要**说清是哪一种**没账。"""
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    from singularity.scheduler import config
    from singularity.scheduler._exec import _mark_dispatch_started
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", tmp_path / "partial_usage")
    (tmp_path / "partial_usage").mkdir()
    monkeypatch.setattr(orch, "_worktrees_dir", lambda *a, **k: tmp_path, raising=False)
    monkeypatch.setattr("singularity.scheduler._git_worktree._worktrees_dir",
                        lambda *a, **k: tmp_path, raising=False)

    t_never = tr.create("从没进过 dispatch")
    r1 = orch._salvage_timed_out(t_never, 12.0, None)
    out1 = r1.executor_result.raw_output
    assert "一次 dispatch 都没进去过" in out1, out1

    t_started = tr.create("进过 dispatch 但没落账")
    _mark_dispatch_started(t_started.id)
    r2 = orch._salvage_timed_out(t_started, 12.0, None)
    out2 = r2.executor_result.raw_output
    assert "dispatch 已经开始了" in out2, out2
    assert "没落账" in out2


def test_dispatch_start_is_marked_before_dispatch_runs(monkeypatch, tmp_path):
    """**接线**：`_mark_dispatch_started` 必须在 `dispatch(...)` **之前**执行。

    上面那两条测的是函数本身 —— 函数对 ≠ 接线通（今晚已经栽过两次）。
    这条借 `tests/test_exec_run.py` 的桩驱动一遍真的 `_exec.run`，
    在 `dispatch` 被调用**的那一刻**回头看 sidecar 在不在。
    """
    import importlib.util
    import pathlib
    from singularity.scheduler import config, _exec
    from singularity.scheduler._exec import read_partial_started_at

    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(config, "PARTIAL_USAGE_DIR", tmp_path / "partial_usage")
    (tmp_path / "partial_usage").mkdir()

    spec = importlib.util.spec_from_file_location(
        "exec_run_harness", pathlib.Path(__file__).resolve().parents[1] / "test_exec_run.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)        # 有 __main__ 保护，import 不会跑用例

    # ⚠️ `install_stubs()` 是**直接改模块属性**的（不走 monkeypatch）——
    # 不还原就会污染**后面所有测试**。2026-09-13 实测：跑完这条再跑
    # `test_tracker.py` 会挂两条（症状是"单独跑是绿的"）。
    # 这是我自己写测试时踩的坑，就地钉住。
    import singularity.scheduler.supervisor as _sup
    # ⚠️ 要拍的**不止 `_exec`** —— 桩是顺着 `_exec.X` 改到**别的模块本身**上的：
    # `_exec.tracker.read_task = ...` 改的是 tracker 模块、`_exec.witness.heartbeat = ...`
    # 改的是 witness 模块。只还原 `_exec` 等于没还（2026-09-13 实测：
    # 漏了那两个，跑完这条再跑 `test_tracker.py` 挂两条，而单独跑是绿的）。
    snap = [(m, dict(vars(m))) for m in (_exec, _exec.tracker, _exec.witness, _sup)]
    snap_s = dict(vars(harness.S))
    seen: dict = {}
    try:
        harness.install_stubs()
        harness.reset_wt()
        harness.S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
        harness.S.dispatch_queue = [("ok", harness.FakeExec(success=True))]
        harness.S.validate_queue = [harness.FakeVal(action="pass")]
        t = harness.make_task()
        harness.S.task = t

        orig = _exec.disp_mod.dispatch

        def _spy(*a, **k):
            seen["started_at"] = read_partial_started_at(t.id)
            return orig(*a, **k)

        _exec.disp_mod.dispatch = _spy
        _exec.run(t, harness.make_ctx(v3=True), {"any": list(harness.S.chain)})
    finally:
        for mod, saved in snap:
            for k in [k for k in vars(mod) if k not in saved]:
                delattr(mod, k)
            for k, v in saved.items():
                setattr(mod, k, v)
        vars(harness.S).clear()
        vars(harness.S).update(snap_s)

    assert seen.get("started_at") is not None, (
        "dispatch 被调用时 sidecar 还不存在 —— 说明那个标记没接在 dispatch 前面")


class _RecordingPool:
    """记录每次 `submit` 的实参（第 1 个是 `runner.execute` 这个绑定方法本身）。"""
    def __init__(self):
        self.calls = []

    def submit(self, *a, **k):
        self.calls.append((a, k))
        return object()


def test_死线在_submit_那一刻就定好(monkeypatch, tmp_path):
    """🔴 池子满时任务**先在队列里排队**，worker 才起来 —— 死线必须在 submit 前定。

    `runner.execute` 原来是在 worker 线程开头才起表，两把尺差一个**排队时间**。
    并发默认 1、单任务可跑 810s ⇒ 排队几分钟是常态，差超过收尾余量(90s) 时
    执行器的自收尾就晚于外面那把 900s 的刀（§67 那个病换了个更常见的触发）。

    删掉 orchestrator 里"submit 前先算 deadline_at、并按位置传第 4 个实参"那两行，
    这条会红：第 4 个位置参数会变回 `mq`（不是 float）。
    """
    import time as _t
    tr = _patch_dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(orch.witness, "warn", lambda *a, **k: None)
    tr.create("死线要在 submit 前定")

    pool = _RecordingPool()
    orch._dispatch_ready(set(), pool, {}, _Runner(), {}, None)

    assert pool.calls, "压根没派发出去（这条测试的前提没成立）"
    args, _kw = pool.calls[0]
    assert len(args) == 5, f"submit 的实参个数变了：{args}"
    deadline = args[-1]
    assert isinstance(deadline, float) and not isinstance(deadline, bool), \
        f"第 4 个位置参数不是死线（拿到 {deadline!r}）—— 排队那段就落在两把尺之外了"
    assert deadline > _t.time() + 800, f"死线算得不对：{deadline}"
