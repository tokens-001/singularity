"""_task_runner.py 单元测试 — 白盒覆盖 execute() 分支 + 辅助函数。

ponytail: 只测 execute() 决策分叉。
finalize() 9 分支已由 test_exec_internals.py 覆盖。

（原来的 `_reorder_agents_by_rank` 那 5 条已删：那个函数**全仓零调用点**——两档制合并后
`execute()` 里"按模型排名重排"那段就没了。用例判据本身是真的，守的东西却不在任何生产路径上。）
"""

import pytest
from types import SimpleNamespace as NS

# ═══════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════

def _make_task(**kw):
    d = {"id": "test12345678", "description": "测试任务", "depends_on": [],
         "retry_count": 0, "max_retries": 3, "depth": 0, "project_id": "",
         "route_locked": False, "route_level": "any", "route_gate": None,
         "route_type": "default"}
    d.update(kw)
    return type("Task", (), d)()


def _make_agents():
    return {
        "any": [{"model": "gpt-4"}, {"model": "claude"}, {"model": "claude-opus"}, {"model": "gpt-5.5"}],
    }


def _make_batch_stub():
    b = type("Batch", (), {})()
    b.ok = True; b.task_id = ""; b.term_reason = "ok"
    b.tool_events = []; b.turn_count = 1
    b.planner_decomposed = False
    b.pre_search_skipped = False; b.pre_search_reason = ""
    b.pre_search_top_decisions = []; b.pre_search_code_context = ""
    b.pre_search_memory = {}
    b.validation = type("V", (), {"verdict": "通过", "action": "pass", "unverified": []})()
    b.dispatch_result = type("D", (), {
        "executor_result": type("E", (), {"success": True, "raw_output": "ok", "elapsed": 0.1, "tokens": 100, "changed_files": []})(),
        "agent_cfg": {"model": "test"}, "level": "any",
    })()
    return b


def _make_pre_stub(**kw):
    d = {"skipped": False, "reason": "", "top_decisions": [],
         "code_context": "",
         "memory": type("Mem", (), {
             "intent": "", "narrative": "", "entity_matches": [], "graph_coverage": 0.0,
         })()}
    d.update(kw)
    return type("Pre", (), d)()


def _make_route_stub(**kw):
    d = {"level": "any", "gate_required": None, "task_type": "default"}
    d.update(kw)
    return type("Route", (), d)()


def _make_snap_stub():
    # method 必须给：RunContext 要把它透传给 _SnapProxy，而审查层靠它判这个 ref
    # 能不能当 diff 基准（漏了它基准就恒空，审查五道检查一起短路 —— 2026-09-11 P0）。
    return type("Snap", (), {"id": "s1", "ref": "abc", "created_at": 0.0,
                             "method": "git"})()


def _setup(monkeypatch, **overrides):
    """安装 execute() 路径所需的全部 stubs。overrides 直接 setattr 到 tr 模块。"""
    import singularity.scheduler._task_runner as tr
    from singularity.scheduler._types import _pending_sse_events

    batch = _make_batch_stub()
    pre = _make_pre_stub()
    route = _make_route_stub()
    snap = _make_snap_stub()

    # 模块级替换 — 用 SimpleNamespace 避免 type() class attr 的 bound method
    _o = NS()
    _o.route = lambda d: route
    _o.rank_models_for_task = lambda *a, **k: []
    _o.RouteResult = tr.router_mod.RouteResult
    monkeypatch.setattr(tr, "router_mod", _o)

    _o2 = NS()
    _o2.pre_search = lambda d, r, **kw: pre  # **kw: pre_search 新增了 deep= 参数
    _o2.apply_escalation = lambda r, p: None
    monkeypatch.setattr(tr, "pre_mod", _o2)

    _o3 = NS()
    _o3.take = lambda tid, **kw: snap
    monkeypatch.setattr(tr, "snap_mod", _o3)

    _o4 = NS()
    _o4.ValidationReport = tr.val_mod.ValidationReport
    monkeypatch.setattr(tr, "val_mod", _o4)

    monkeypatch.setattr(tr, "_run_with_retry", lambda t, ctx, agents: batch)

    _o5 = NS()
    _gr = NS()
    _gr.success = True; _gr.final_output = "done"; _gr.iterations = 2
    _o5.run = lambda task, goal, max_iter: _gr
    monkeypatch.setattr(tr, "GoalLoop", lambda agents: _o5)

    _o6 = NS()
    _o6.heartbeat = lambda *a, **k: None
    monkeypatch.setattr(tr, "witness", _o6)

    _o7 = NS()
    _o7.time = lambda: 1782000000.0
    monkeypatch.setattr(tr, "time", _o7)
    # ponytail: _run_fusion 已移除，不再需要桩

    _pending_sse_events.clear()

    # overrides
    for name, val in overrides.items():
        monkeypatch.setattr(tr, name, val, raising=False)

    return tr, batch, pre, route, snap


# ═══════════════════════════════════════════════════════════════
# TaskRunner.execute() — 执行分叉
# ═══════════════════════════════════════════════════════════════

class TestTaskRunnerExecute:
    def test_route_locked_skips_router(self, monkeypatch):
        """route_locked=True → 用任务属性构造 RouteResult，不调 router.route()。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        route_called = []
        monkeypatch.setattr(tr.router_mod, "route", lambda d: route_called.append(1) or route)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(route_locked=True, route_level="any",
                          route_gate="security", route_type="fix")
        runner = TaskRunner()
        result_batch, result_route, result_snap = runner.execute(task, _make_agents())

        assert not route_called
        # 两档后 level 不再使用, gate_required 和 task_type 保留
        assert result_route.gate_required == "security"
        assert result_route.task_type == "fix"

    def test_route_unlocked_calls_router(self, monkeypatch):
        """route_locked=False → 调用 router.route(description)。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        route_calls = []
        monkeypatch.setattr(tr.router_mod, "route",
            lambda d: route_calls.append(d) or route)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(route_locked=False, description="写一个登录页面")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert len(route_calls) == 1
        assert "登录" in route_calls[0]

    def test_goal_loop_path(self, monkeypatch):
        """描述以 [Goal] 开头 → GoalLoop。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        goals = []
        _gl = NS()
        _gr = NS()
        _gr.success = True; _gr.final_output = "done"; _gr.iterations = 3
        _gl.run = lambda task, goal, max_iter: (goals.append(goal), _gr)[1]
        monkeypatch.setattr(tr, "GoalLoop", lambda agents: _gl)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="[Goal] 实现用户认证系统\n其它说明")
        runner = TaskRunner()
        batch, route, snap = runner.execute(task, _make_agents())

        assert goals == ["实现用户认证系统"]
        assert "goal_met_3iter" in batch.term_reason
        assert batch.validation.action == "pass"

    def test_goal_not_matched_falls_through(self, monkeypatch):
        """非 [Goal] 格式不触发 GoalLoop。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        retry_called = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_called.append(1) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="实现用户认证系统")  # 无 [Goal] 前缀
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert retry_called == [1]  # 走了默认路径

    def test_goal_loop_exhausted(self, monkeypatch):
        """Goal 未达成 → validation action=abort。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        _gl = NS()
        _gr = NS()
        _gr.success = False; _gr.final_output = "未完成"; _gr.iterations = 5
        _gl.run = lambda task, goal, max_iter: _gr
        monkeypatch.setattr(tr, "GoalLoop", lambda agents: _gl)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="[Goal] 不可能的任务\n")
        runner = TaskRunner()
        batch, route, snap = runner.execute(task, _make_agents())

        assert "goal_exhausted_5iter" in batch.term_reason
        assert batch.validation.action == "abort"

    def test_default_retry_path(self, monkeypatch):
        """普通任务非 Goal/委员会 → _run_with_retry。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        retry_calls = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_calls.append(t.id) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert retry_calls == ["test12345678"]

    def test_execute_preserves_agents(self, monkeypatch):
        """执行时 agents 保持原样 (两档后无模型重排)。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        retry_agents = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_agents.append(agents) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        agents = _make_agents()
        runner = TaskRunner()
        runner.execute(task, agents)

        # agents 保持传入时的结构
        assert "any" in retry_agents[0]
        assert "any" in retry_agents[0]

    def test_code_context_injected(self, monkeypatch):
        """pre.code_context 非空 → 追加到 task.description。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        pre.code_context = "src/auth.py\nsrc/login.py"
        monkeypatch.setattr(tr.pre_mod, "pre_search", lambda d, r, **kw: pre)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="实现登录功能")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert "[代码结构上下文]" in task.description
        assert "src/auth.py" in task.description

    def test_no_code_context_no_injection(self, monkeypatch):
        """pre.code_context 为空 → 描述不变。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="实现登录功能")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert "[代码结构上下文]" not in task.description

    def test_pre_search_memory_passed_to_batch(self, monkeypatch):
        """pre_search 的 memory 正确传递到 batch。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        pre.memory.intent = "修复登录bug"
        pre.memory.narrative = "用户无法登录"
        monkeypatch.setattr(tr.pre_mod, "pre_search", lambda d, r, **kw: pre)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        runner = TaskRunner()
        batch_out, _, _ = runner.execute(task, _make_agents())

        assert batch_out.pre_search_memory["intent"] == "修复登录bug"
        assert batch_out.pre_search_memory["narrative"] == "用户无法登录"

    def test_snapshot_taken(self, monkeypatch):
        """每次 execute 都调用 snap_mod.take()。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        snap_ids = []
        monkeypatch.setattr(tr.snap_mod, "take", lambda tid, **kw: snap_ids.append(tid) or snap)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(id="abc123")
        runner = TaskRunner()
        _, _, result_snap = runner.execute(task, _make_agents())

        assert snap_ids == ["abc123"]
        assert result_snap.ref == "abc"

    def test_sse_event_emitted_for_goal(self, monkeypatch):
        """Goal 循环 → _pending_sse_events 追加 system 事件。"""
        from singularity.scheduler._types import _pending_sse_events
        _pending_sse_events.clear()

        tr, batch, pre, route, snap = _setup(monkeypatch)

        _gl = NS()
        _gr = NS()
        _gr.success = True; _gr.final_output = "done"; _gr.iterations = 1
        _gl.run = lambda task, goal, max_iter: _gr
        monkeypatch.setattr(tr, "GoalLoop", lambda agents: _gl)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="[Goal] 完成功能\n")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        events = [e for e in _pending_sse_events if e["kind"] == "system"]
        assert len(events) == 1
        assert "Goal循环" in events[0]["msg"]


# ═══════════════════════════════════════════════════════════════
# 死线（deadline_at）的**起点**（2026-09-14）
# ═══════════════════════════════════════════════════════════════
# 它是任务级**唯一那把尺**。原来在**本 worker 线程开头**起算 —— 但池子满时任务是先
# 在队列里排队、worker 才起来的，两把尺差一个**排队时间**。并发默认 1、单任务可跑 810s
# ⇒ 排队几分钟是常态，差 > 收尾余量(90s) 时执行器算出的"该收尾了"就**晚于**外面那把
# 900s 的刀，自收尾照样赶不上收割（§67 那个病，换了更常见的触发条件）。
# ⇒ 正常路径由 orchestrator 在 **submit 前**算好传进来，本处起算只作兜底。

class TestDeadlineComesFromSubmit:
    def _capture(self, monkeypatch, tr) -> dict:
        seen = {}
        monkeypatch.setattr(tr, "_run_with_retry",
                            lambda t, ctx, agents: (seen.update(deadline_at=ctx.deadline_at),
                                                    _make_batch_stub())[1])
        return seen

    def test_调用方给了死线就照用(self, monkeypatch):
        tr, *_ = _setup(monkeypatch)
        seen = self._capture(monkeypatch, tr)
        tr.TaskRunner().execute(_make_task(), _make_agents(), None, 1782000123.0)
        assert seen["deadline_at"] == 1782000123.0, \
            "调用方（submit 前）算好的死线没被采用 —— 排队那段又落回两把尺之外了"

    def test_没给死线才退回本线程起算(self, monkeypatch):
        """兜底：测试 / 直接调用（`deadline_at=0.0`）时仍按老行为算。

        `_setup` 把 `tr.time` 换成了常量 1782000000.0，所以这里能精确断言。
        """
        from singularity.scheduler import config
        tr, *_ = _setup(monkeypatch)
        seen = self._capture(monkeypatch, tr)
        tr.TaskRunner().execute(_make_task(), _make_agents())
        assert seen["deadline_at"] == 1782000000.0 + config.TASK_DEADLINE_S
