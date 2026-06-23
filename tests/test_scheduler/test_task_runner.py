"""_task_runner.py 单元测试 — 白盒覆盖 execute() 分支 + 辅助函数。

ponytail: 只测 execute() 决策分叉和纯函数 _reorder_agents_by_rank。
finalize() 9 分支已由 test_exec_internals.py 覆盖。
"""

import pytest
from types import SimpleNamespace as NS

# ═══════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════

def _make_task(**kw):
    d = {"id": "test12345678", "description": "测试任务", "depends_on": [],
         "retry_count": 0, "max_retries": 3, "depth": 0, "project_id": "",
         "route_locked": False, "route_level": "E", "route_gate": None,
         "route_type": "default"}
    d.update(kw)
    return type("Task", (), d)()


def _make_agents():
    return {
        "E": [{"model": "gpt-4"}, {"model": "claude"}],
        "D": [{"model": "claude-opus"}, {"model": "gpt-5.5"}],
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
        "agent_cfg": {"model": "test"}, "level": "E",
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
    d = {"level": "E", "gate_required": None, "task_type": "default"}
    d.update(kw)
    return type("Route", (), d)()


def _make_snap_stub():
    return type("Snap", (), {"id": "s1", "ref": "abc", "created_at": 0.0})()


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
    _o2.pre_search = lambda d, r: pre
    _o2.apply_escalation = lambda r, p: None
    monkeypatch.setattr(tr, "pre_mod", _o2)

    _o3 = NS()
    _o3.take = lambda tid: snap
    monkeypatch.setattr(tr, "snap_mod", _o3)

    _o4 = NS()
    _o4.ValidationReport = tr.val_mod.ValidationReport
    monkeypatch.setattr(tr, "val_mod", _o4)

    monkeypatch.setattr(tr, "_run_with_retry", lambda t, ctx, agents: batch)
    monkeypatch.setattr(tr, "_run_committee", lambda t, ctx, agents, d_agents: batch)

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
    # _run_fusion 在 execute() 内 inline import, 需桩源模块
    monkeypatch.setattr("singularity.scheduler._exec._run_fusion",
        lambda t, level, agents, ctx, tier: batch)

    _pending_sse_events.clear()

    # overrides
    for name, val in overrides.items():
        monkeypatch.setattr(tr, name, val, raising=False)

    return tr, batch, pre, route, snap


# ═══════════════════════════════════════════════════════════════
# _reorder_agents_by_rank — 纯函数
# ═══════════════════════════════════════════════════════════════

class TestReorderAgentsByRank:
    def test_ranked_first(self):
        """排名靠前的模型排到列表前面。"""
        from singularity.scheduler._task_runner import _reorder_agents_by_rank
        agents = [{"model": "gpt-4"}, {"model": "claude"}, {"model": "qwen"}]
        result = _reorder_agents_by_rank(agents, ["claude", "gpt-4"])
        assert [a["model"] for a in result] == ["claude", "gpt-4", "qwen"]

    def test_unranked_to_end(self):
        """未在排名中的模型排到末尾。"""
        from singularity.scheduler._task_runner import _reorder_agents_by_rank
        agents = [{"model": "gpt-4"}, {"model": "qwen"}]
        result = _reorder_agents_by_rank(agents, ["qwen"])
        assert result[0]["model"] == "qwen"
        assert result[1]["model"] == "gpt-4"

    def test_empty_list(self):
        """空列表直接返回。"""
        from singularity.scheduler._task_runner import _reorder_agents_by_rank
        assert _reorder_agents_by_rank([], ["claude"]) == []

    def test_all_unranked_preserves_order(self):
        """全未排名保持原顺序。"""
        from singularity.scheduler._task_runner import _reorder_agents_by_rank
        agents = [{"model": "a"}, {"model": "b"}]
        result = _reorder_agents_by_rank(agents, ["x", "y"])
        assert [a["model"] for a in result] == ["a", "b"]

    def test_rank_missing_model_not_in_agents(self):
        """排名引用的模型不在 agents 中 → 忽略。"""
        from singularity.scheduler._task_runner import _reorder_agents_by_rank
        agents = [{"model": "a"}, {"model": "b"}]
        result = _reorder_agents_by_rank(agents, ["c", "a"])
        assert [a["model"] for a in result] == ["a", "b"]


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
        task = _make_task(route_locked=True, route_level="D",
                          route_gate="security", route_type="fix")
        runner = TaskRunner()
        result_batch, result_route, result_snap = runner.execute(task, _make_agents())

        assert not route_called
        assert result_route.level == "D"
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

    def test_committee_d_level_two_agents(self, monkeypatch):
        """D 层 + ≥2 个 D agent → _run_committee。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        # 路由到 D 层
        monkeypatch.setattr(tr.router_mod, "route",
            lambda d: _make_route_stub(level="D", task_type="default"))

        committee_called = []
        monkeypatch.setattr(tr, "_run_committee",
            lambda t, ctx, agents, d_agents: committee_called.append(len(d_agents)) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="重构数据库层")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert committee_called == [2]  # 2个D agent

    def test_d_level_one_agent_no_committee(self, monkeypatch):
        """D 层但只有 1 个 D agent → 不走委员会，走默认路径。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        monkeypatch.setattr(tr.router_mod, "route",
            lambda d: _make_route_stub(level="D", task_type="default"))

        retry_called = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_called.append(1) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        agents = {"D": [{"model": "claude-opus"}]}  # 只有1个D agent
        runner = TaskRunner()
        runner.execute(task, agents)

        assert retry_called == [1]

    def test_fusion_path(self, monkeypatch):
        """fusion 任务类型 → _run_fusion。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        monkeypatch.setattr(tr.router_mod, "route",
            lambda d: _make_route_stub(level="E", task_type="fusion"))

        fusion_calls = []
        monkeypatch.setattr("singularity.scheduler._exec._run_fusion",
            lambda t, level, agents, ctx, tier: fusion_calls.append(tier) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task(description="融合分析多个方案")
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert fusion_calls == ["dual"]  # E层 fusion → dual tier

    def test_fusion_d_level_super_tier(self, monkeypatch):
        """D 层 fusion → super tier（1个D agent 避免 committee 先触发）。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        monkeypatch.setattr(tr.router_mod, "route",
            lambda d: _make_route_stub(level="D", task_type="fusion"))

        fusion_calls = []
        monkeypatch.setattr("singularity.scheduler._exec._run_fusion",
            lambda t, level, agents, ctx, tier: fusion_calls.append(tier) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        runner = TaskRunner()
        # 只有1个D agent → 不触发 committee, 走 fusion 路径
        agents = {"D": [{"model": "claude-opus"}]}
        runner.execute(task, agents)

        assert fusion_calls == ["super"]

    def test_default_retry_path(self, monkeypatch):
        """普通任务非 Goal/委员会/fusion → _run_with_retry。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        retry_calls = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_calls.append(t.id) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        assert retry_calls == ["test12345678"]

    def test_agents_reordered_with_ranked_models(self, monkeypatch):
        """有模型排名 → effective_agents 按排名重排。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        monkeypatch.setattr(tr.router_mod, "rank_models_for_task",
            lambda *a, **k: ["claude", "gpt-4"])

        retry_agents = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_agents.append(agents) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        runner = TaskRunner()
        runner.execute(task, _make_agents())

        e_agents = retry_agents[0].get("E", [])
        assert e_agents[0]["model"] == "claude"
        assert e_agents[1]["model"] == "gpt-4"

    def test_no_ranked_models_preserves_agents(self, monkeypatch):
        """无排名 → agents 保持原顺序。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        monkeypatch.setattr(tr.router_mod, "rank_models_for_task",
            lambda *a, **k: [])  # 空排名

        retry_agents = []
        monkeypatch.setattr(tr, "_run_with_retry",
            lambda t, ctx, agents: retry_agents.append(agents) or batch)

        from singularity.scheduler._task_runner import TaskRunner
        task = _make_task()
        agents = _make_agents()
        runner = TaskRunner()
        runner.execute(task, agents)

        # E层保持原顺序
        assert retry_agents[0]["E"] == agents["E"]

    def test_code_context_injected(self, monkeypatch):
        """pre.code_context 非空 → 追加到 task.description。"""
        tr, batch, pre, route, snap = _setup(monkeypatch)

        pre.code_context = "src/auth.py\nsrc/login.py"
        monkeypatch.setattr(tr.pre_mod, "pre_search", lambda d, r: pre)

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
        monkeypatch.setattr(tr.pre_mod, "pre_search", lambda d, r: pre)

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
        monkeypatch.setattr(tr.snap_mod, "take", lambda tid: snap_ids.append(tid) or snap)

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
