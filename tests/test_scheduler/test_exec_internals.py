"""_exec.py 内部函数单元测试 — 白盒覆盖关键分支。

ponytail: 只测分支密度最高的 leaf 函数。run() 路径已由 test_exec_run.py 覆盖。
"""

import os, sys, json, tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from singularity.scheduler._exec import (
    _build_effective_task,
    _check_cancelled,
    _decide_cascade,
)
from singularity.scheduler._types import RunContext, BatchOutput


# ═══════════════════════════════════════════════════════════════
# 辅助工厂
# ═══════════════════════════════════════════════════════════════

def _task(**kw):
    defaults = {
        "id": "1234567890", "description": "测试任务",
        "route_level": "any", "route_gate": False, "route_type": "default",
        "depends_on": [], "retry_count": 0, "max_retries": 2, "depth": 0,
        "project_id": "", "status": None,
    }
    defaults.update(kw)
    return type("T", (), defaults)()


def _val(action="pass", confidence=0.9, verdict="通过", evidence=None):
    """创建模拟 ValidationReport。"""
    return type("V", (), {
        "action": action, "confidence": confidence,
        "verdict": verdict, "evidence": evidence or {},
        "quality_signals": {},
    })()


def _disp(agent_cfg=None):
    """创建模拟 DispatchResult。"""
    exec_result = type("E", (), {
        "success": True, "raw_output": "结果",
        "changed_files": [], "tool_events": [],
        "elapsed": 0.0, "tokens": 0,
    })()
    return type("D", (), {
        "executor_result": exec_result,
        "agent_cfg": agent_cfg or {"model": "test-model"},
        "level": "any", "attempts": 1,
    })()


# ═══════════════════════════════════════════════════════════════
# _decide_cascade — 5 分支决策
# ═══════════════════════════════════════════════════════════════

class TestDecideCascade:
    """cascade routing 决策树: pass / cascade_accept / retry / cascade_skip / 终态。"""

    def _call(self, validation, quality=None, fallback_chain=None):
        return _decide_cascade(
            task=_task(),
            level="any",
            turn=1,
            validation=validation,
            disp_result=_disp(),
            all_tool_events=[],
            pending_merge_req=None,
            fallback_chain=fallback_chain or [{"model": "m1"}],
            tried_models=set(),
            quality=quality or {"warnings": [], "failure_kind": "ok", "confidence": 0.5},
        )

    def test_pass_returns_ok(self):
        """action=pass → 返回 BatchOutput(ok=True)。"""
        action, payload = self._call(_val(action="pass"))
        assert action == "return"
        assert payload.ok is True
        assert "pass" in payload.term_reason

    def test_retry_high_confidence_accepts(self):
        """retry + conf≥0.75 → cascade_accept (省钱跳过升级)。"""
        action, payload = self._call(_val(action="retry", confidence=0.85))
        assert action == "return"
        assert payload.ok is True
        assert "cascade_accept" in payload.term_reason

    @pytest.mark.parametrize("conf", [0.50, 0.45, 0.35])
    def test_retry_mid_confidence_continues(self, conf):
        """retry + 0.35≤conf<0.75 → continue with feedback。"""
        action, feedback = self._call(_val(action="retry", confidence=conf))
        assert action == "continue"
        assert isinstance(feedback, str)
        assert len(feedback) > 0

    def test_retry_low_confidence_skips(self):
        """retry + conf<0.35 + 有更高层模型 → break (cascade_skip)。"""
        action, payload = self._call(
            _val(action="retry", confidence=0.20),
            fallback_chain=[{"model": "m1"}, {"model": "m2"}],
        )
        assert action == "break"
        assert payload is None

    def test_retry_low_confidence_exhausted_escalates(self):
        """retry + conf<0.35 + 无 fallback → continue (让 turn loop 耗尽后升级)。"""
        action, feedback = self._call(
            _val(action="retry", confidence=0.20),
            fallback_chain=[{"model": "m1"}],  # 只剩1个
        )
        # 无更高层可供 break → 降级为 continue retry
        assert action == "continue"

    def test_rollback_returns_not_ok(self):
        """action=rollback → 返回 BatchOutput(ok=False)。"""
        action, payload = self._call(_val(action="rollback", verdict="阻断"))
        assert action == "return"
        assert payload.ok is False
        assert "rollback" in payload.term_reason

    def test_abort_returns_not_ok(self):
        """action=abort → 返回 BatchOutput(ok=False)。"""
        action, payload = self._call(_val(action="abort", verdict="阻断"))
        assert action == "return"
        assert payload.ok is False


# ═══════════════════════════════════════════════════════════════
# _build_effective_task — prompt 拼接
# ═══════════════════════════════════════════════════════════════

class TestBuildEffectiveTask:
    """task description + 记忆注入 + planner preamble + 项目上下文。"""

    def test_basic_no_additions(self, monkeypatch):
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory", lambda d: "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context", lambda t: "")
        result = _build_effective_task(_task(description="核心任务"), turn=1, feedback="",
                                       is_planner=False)
        assert "核心任务" in result

    def test_turn2_no_memory_injection(self, monkeypatch):
        """turn>1 不注入记忆。"""
        called = []
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory",
                            lambda d: called.append(1) or "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context", lambda t: "")
        _build_effective_task(_task(), turn=2, feedback="", is_planner=False)
        assert len(called) == 0, "turn≥2 不应注入记忆"

    def test_with_feedback_skips_memory(self, monkeypatch):
        """有 feedback 时即使 turn=1 也不注入记忆。"""
        called = []
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory",
                            lambda d: called.append(1) or "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context", lambda t: "")
        _build_effective_task(_task(), turn=1, feedback="重做", is_planner=False)
        assert len(called) == 0, "有 feedback 不注入记忆"

    def test_planner_mode_adds_preamble(self, monkeypatch):
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory", lambda d: "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context", lambda t: "")
        result = _build_effective_task(_task(description="规划"), turn=1, feedback="",
                                       is_planner=True)
        assert "PLANNER" in result or "规划" in result

    def test_includes_project_context(self, monkeypatch):
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory", lambda d: "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context",
                            lambda t: "项目上下文内容")
        result = _build_effective_task(_task(), turn=1, feedback="", is_planner=False)
        assert "项目上下文内容" in result

    def test_with_construct_context(self, monkeypatch):
        """turn≥2 + 有工具事件 → 注入裁剪后的上下文。"""
        monkeypatch.setattr("singularity.scheduler._exec._inject_memory", lambda d: "")
        monkeypatch.setattr("singularity.scheduler._exec._build_project_context", lambda t: "")
        monkeypatch.setattr("singularity.scheduler._exec._construct_context",
                            lambda events, turn: "裁剪上下文")
        result = _build_effective_task(_task(), turn=2, feedback="", is_planner=False,
                                       tool_events=[{"tool": "read", "status": "done"}])
        assert "裁剪上下文" in result


# ═══════════════════════════════════════════════════════════════
# _check_cancelled — 人工取消检测
# ═══════════════════════════════════════════════════════════════

class TestCheckCancelled:
    """取消标记文件存在 → 返回取消 BatchOutput; 否则 None。"""

    def test_no_cancel_file_returns_none(self, monkeypatch):
        tmp = Path(tempfile.mkdtemp())
        monkeypatch.setattr("singularity.scheduler._exec.config.CANCEL_DIR", tmp)
        result = _check_cancelled(_task(id="no_cancel"), [])
        assert result is None

    def test_cancel_file_exists_returns_cancelled(self, monkeypatch):
        tmp = Path(tempfile.mkdtemp())
        monkeypatch.setattr("singularity.scheduler._exec.config.CANCEL_DIR", tmp)
        (tmp / "will_cancel.json").write_text("{}")
        result = _check_cancelled(_task(id="will_cancel"), [{"tool": "read"}])
        assert result is not None
        assert result.term_reason == "cancelled_by_user"
        assert result.ok is False
        assert not (tmp / "will_cancel.json").exists(), "取消文件应被删除"


# ═══════════════════════════════════════════════════════════════
# _save_planner_patch / _read_planner_patch — 规划方案持久化
# ═══════════════════════════════════════════════════════════════

class TestPlannerPatch:
    def test_write_and_read(self, monkeypatch, tmp_path):
        monkeypatch.setattr("singularity.scheduler._exec.config.PATCH_DIR", tmp_path)
        from singularity.scheduler._exec import _save_planner_patch, _read_planner_patch
        _save_planner_patch("t001", "方案内容")
        assert _read_planner_patch("t001") == "方案内容"

    def test_read_nonexistent(self, monkeypatch, tmp_path):
        monkeypatch.setattr("singularity.scheduler._exec.config.PATCH_DIR", tmp_path)
        from singularity.scheduler._exec import _read_planner_patch
        assert _read_planner_patch("nonexistent") is None


# ═══════════════════════════════════════════════════════════════
# _safe_dep_list — int → list[int] 标准化
# ═══════════════════════════════════════════════════════════════

class TestSafeDepList:
    def test_int_to_list(self):
        from singularity.scheduler._exec import _safe_dep_list
        assert _safe_dep_list(5) == [5]

    def test_list_passthrough(self):
        from singularity.scheduler._exec import _safe_dep_list
        assert _safe_dep_list([1, 2, 3]) == [1, 2, 3]

    def test_other_returns_empty(self):
        from singularity.scheduler._exec import _safe_dep_list
        assert _safe_dep_list("str") == []
        assert _safe_dep_list(None) == []
        assert _safe_dep_list({}) == []


# ═══════════════════════════════════════════════════════════════
# _finalize_result — 8+ 分支后处理 (mock 重型依赖)
# ═══════════════════════════════════════════════════════════════

class TestFinalizeResult:
    """覆盖 TaskRunner.finalize() 的关键决策分支 (架构 #1.1 搬迁后)。

    用 monkeypatch 桩掉 _task_runner 模块级依赖。
    """

    @staticmethod
    def _make_task(**kw):
        d = {"id": "1234567890ab", "description": "测试", "depends_on": [],
             "retry_count": 0, "max_retries": 3, "depth": 0, "project_id": "",
             "status": None}
        d.update(kw)
        return type("T", (), d)()

    @staticmethod
    def _make_batch(ok=True, term_reason="ok", validation=None, disp_result=None,
                     planner_decomposed=False, pre_search_skipped=False,
                     pre_search_reason="", pre_search_top_decisions=None,
                     pre_search_memory=None, tool_events=None, turn_count=0):
        exec_out = type("E", (), {
            "raw_output": "output", "changed_files": [], "elapsed": 0.0, "tokens": 0,
        })()
        disp = disp_result or type("D", (), {
            "executor_result": exec_out,
            "agent_cfg": {"model": "test"},
        })()
        val = validation or type("V", (), {
            "action": "pass", "verdict": "通过", "evidence": {},
        })()
        return type("B", (), {
            "ok": ok, "term_reason": term_reason, "validation": val,
            "dispatch_result": disp, "planner_decomposed": planner_decomposed,
            "pre_search_skipped": pre_search_skipped,
            "pre_search_reason": pre_search_reason,
            "pre_search_top_decisions": pre_search_top_decisions or [],
            "pre_search_memory": pre_search_memory or {},
            "tool_events": tool_events or [],
            "turn_count": turn_count,
            "merge_request": None,
        })()

    @staticmethod
    def _install_stubs(monkeypatch, **overrides):
        """桩掉 _task_runner 模块级 heavy 依赖。"""
        import singularity.scheduler._task_runner as tr
        from singularity.scheduler.tracker import TaskStatus

        # 默认 stub (target _task_runner module)
        stubs = {
            "_judge_and_profile": lambda t, b: None,
            "_materialize_in_main": lambda b, t: None,
            "_maybe_complete_parents": lambda tid: None,
            "_save_trace": lambda *a, **k: None,
            "_read_planner_patch": lambda tid: None,
            "materialize_plan": lambda tid, subs: ["child1", "child2"],
            "decompose": lambda desc: [{"desc": "sub1"}, {"desc": "sub2"}],
            "witness.heartbeat": lambda *a, **k: None,
            "witness.warn": lambda *a, **k: None,
            "mem_mod.archive_experience": lambda *a, **k: None,
            "rl_mod.load_learner": lambda: type("L", (), {"record": lambda *a, **k: None})(),
            "rl_mod.save_learner": lambda l: None,
            "chan_mod.assess": lambda desc, reason, files: type("R", (), {"severity": "info"})(),
            "chan_mod.save_report": lambda r: None,
            "snap_mod.rollback": lambda s, **kw: None,
            "tracker.transition": lambda tid, status, **kw: None,
            "tracker.create": lambda desc, **kw: type("NT", (), {"id": "fix00001"})(),
            "tracker.TaskStatus": TaskStatus,
            "time.time": lambda: 1782000000.0,
        }
        stubs.update(overrides)

        for path, stub in stubs.items():
            parts = path.split(".")
            if len(parts) == 1:
                monkeypatch.setattr(tr, parts[0], stub, raising=False)
            else:
                # 多级属性: "tracker.transition" → tr.tracker.transition
                obj = tr
                for p in parts[:-1]:
                    if not hasattr(obj, p):
                        stub_obj = MagicMock()
                        monkeypatch.setattr(obj, p, stub_obj, raising=False)
                    obj = getattr(obj, p)
                monkeypatch.setattr(type(obj) if hasattr(obj, '__self__') else obj,
                                    parts[-1], stub, raising=False)

        # 清空 _pending_sse_events
        import singularity.scheduler._types as _t
        _t._pending_sse_events.clear()
        tr._pending_sse_events.clear()

        return tr, TaskStatus

    def _call(self, monkeypatch, task=None, batch=None, route=None, snap=None, **stub_overrides):
        from singularity.scheduler._task_runner import TaskRunner
        task = task or self._make_task()
        batch = batch or self._make_batch()
        route = route or type("R", (), {"level": "any", "task_type": "default"})()

        from singularity.scheduler.snapshot import Snapshot
        snap = snap or Snapshot(id="s1", method="git", ref="abc", created_at=0.0)

        self._install_stubs(monkeypatch, **stub_overrides)
        runner = TaskRunner()
        results = []
        with monkeypatch.context() as m:
            # supervisor 是懒加载，提前桩掉
            m.setattr("singularity.scheduler.supervisor.supervise",
                      lambda *a, **k: type("SV", (), {"verdict": "pass", "issues": []})(),
                      raising=False)
            reason = runner.finalize(task, batch, route, snap, results)
        return reason, results, runner

    # ── 分支1: planner_decomposed ──
    def test_planner_decomposed_path(self, monkeypatch):
        batch = self._make_batch(planner_decomposed=True, term_reason="decomposed (level=E, turn=1)")
        reason, results, _ = self._call(monkeypatch, batch=batch)
        assert reason.startswith("decomposed:")
        assert len(results) >= 1
        assert results[0][0] == "1234567890ab"

    # ── 分支2: pass ──
    def test_pass_path(self, monkeypatch):
        val = type("V", (), {"action": "pass", "verdict": "通过"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status, kw))

        reason, results, _ = self._call(
            monkeypatch,
            batch=self._make_batch(validation=val, term_reason="pass"),
            **{"tracker.transition": record_transition},
        )
        assert reason.startswith("pass:")
        assert any(s.name == "DONE" for _, s, _ in transitions)

    # ── 分支3: rollback ──
    def test_rollback_path(self, monkeypatch):
        val = type("V", (), {"action": "rollback", "verdict": "阻断"})()

        rollback_called = []
        def record_rollback(s, **kw):
            rollback_called.append(s)

        reason, results, _ = self._call(
            monkeypatch,
            batch=self._make_batch(ok=False, validation=val, term_reason="rollback"),
            **{"snap_mod.rollback": record_rollback},
        )
        assert reason.startswith("rolled_back:")
        assert len(rollback_called) == 1

    # ── 分支4: D方案 + escalation_exhausted → E+ 修复 ──
    def test_dplan_escalation_to_eplus(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        reason, results, _ = self._call(
            monkeypatch,
            batch=self._make_batch(ok=False, validation=val, term_reason="escalation_exhausted (level=any)"),
            **{
                "_read_planner_patch": lambda tid: "规划方案内容",
                "tracker.transition": record_transition,
            },
        )
        assert "auto_fix" in reason
        assert any("PENDING" in str(s) for _, s, _ in transitions), f"transitions: {transitions}"

    # ── 分支5: 重试耗尽 + depth<MAX → 自动拆分 ──
    def test_auto_decompose_on_exhaustion(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        task = self._make_task(retry_count=3, max_retries=3, depth=0)
        reason, results, _ = self._call(
            monkeypatch,
            task=task,
            batch=self._make_batch(ok=False, validation=val, term_reason="abort: 失败"),
            **{"tracker.transition": record_transition},
        )
        assert "auto_decomposed" in reason
        assert any("DECOMPOSED" in str(s) for _, s, _ in transitions), f"transitions: {transitions}"

    # ── 分支6: 重试耗尽但无法拆分 → FAILED ──
    def test_exhausted_cannot_decompose(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        task = self._make_task(retry_count=3, max_retries=3, depth=0)
        reason, results, _ = self._call(
            monkeypatch,
            task=task,
            batch=self._make_batch(ok=False, validation=val, term_reason="abort: 失败"),
            **{
                "decompose": lambda desc: [],  # 拆不出来
                "tracker.transition": record_transition,
            },
        )
        assert "exhausted" in reason
        assert any("FAILED" in str(s) for _, s, _ in transitions), f"transitions: {transitions}"

    # ── 分支7: 普通失败 (无D方案, 未耗尽) ──
    def test_plain_failure(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        task = self._make_task(retry_count=0, max_retries=3, depth=0)
        reason, results, _ = self._call(
            monkeypatch,
            task=task,
            batch=self._make_batch(ok=False, validation=val, term_reason="abort: 失败"),
            **{"tracker.transition": record_transition},
        )
        assert reason.startswith("failed:")
        assert any("FAILED" in str(s) for _, s, _ in transitions), f"transitions: {transitions}"

    # ── 边界: QA gate → fail ──
    def test_qa_gate_fail_overrides(self, monkeypatch):
        val = type("V", (), {"action": "pass", "verdict": "通过"})()
        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        with monkeypatch.context() as m:
            m.setattr("singularity.scheduler.supervisor.supervise",
                      lambda *a, **k: type("SV", (), {"verdict": "fail", "issues": ["缺陷1"]})(),
                      raising=False)
            self._install_stubs(monkeypatch, **{"tracker.transition": record_transition})
            from singularity.scheduler._task_runner import TaskRunner
            from singularity.scheduler.snapshot import Snapshot
            snap = Snapshot(id="s1", method="git", ref="abc", created_at=0.0)
            runner = TaskRunner()
            results = []
            reason = runner.finalize(
                self._make_task(),
                self._make_batch(validation=val, term_reason="pass"),
                type("R", (), {"level": "any", "task_type": "default"})(),
                snap, results,
            )
        assert "; QA:fail" in reason

    # ── 边界: depth >= MAX → 不自动拆分 ──
    def test_max_depth_no_auto_decompose(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()
        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        task = self._make_task(retry_count=3, max_retries=3, depth=6)  # depth=MAX
        reason, results, _ = self._call(
            monkeypatch, task=task,
            batch=self._make_batch(ok=False, validation=val, term_reason="abort"),
            **{"tracker.transition": record_transition},
        )
        # depth=6 >= _MAX_DEPTH=6 → 不拆分, 直接 FAILED
        assert "failed" in reason or "exhausted" in reason
        assert any("FAILED" in str(s) for _, s, _ in transitions)


# ═══════════════════════════════════════════════════════════════
# 委员会辩论的波数（性能相关：轮数直接决定架构阶段耗时）
# ═══════════════════════════════════════════════════════════════

class TestFusionModelResolution:

    def _fake_client(self, responses):
        """responses: [(status, sse_lines, err_text)]，按调用顺序取。返回 (Client 实例, 记录每次 body)。"""
        seen = []

        class _Resp:
            def __init__(self, status, lines, text):
                self.status_code, self._lines, self.text = status, lines, text

            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass

            def iter_lines(self):
                for l in self._lines:
                    yield l

        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False

            def stream(self, method, url, headers=None, json=None):
                seen.append(dict(json or {}))
                status, lines, text = responses[min(len(seen) - 1, len(responses) - 1)]
                return _Resp(status, lines, text)
        return _C(), seen

    def test_call_model_retries_without_temperature(self, monkeypatch):
        """kimi-k3 只接受 temperature=1（实测 400 'only 1 is allowed'）→ 必须去掉后重试。"""
        import httpx
        from singularity.scheduler import execution_judge as ej
        monkeypatch.setattr(ej, "_resolve_api", lambda m: ("BENCH_KEY", "http://x"))
        monkeypatch.setenv("BENCH_KEY", "k")
        ok_lines = ['data: {"choices":[{"delta":{"content":"OK"}}]}', 'data: [DONE]']
        client, seen = self._fake_client([
            (400, [], 'invalid temperature: only 1 is allowed'),
            (200, ok_lines, ""),
        ])
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        assert ej._call_model("hi", "kimi-k3") == "OK"
        assert len(seen) == 2, seen
        assert "temperature" not in seen[1], seen

    def test_call_model_logs_non_200(self, monkeypatch):
        """非 200 不能再静默返回空串。"""
        import httpx
        from singularity.scheduler import execution_judge as ej
        monkeypatch.setattr(ej, "_resolve_api", lambda m: ("BENCH_KEY2", "http://x"))
        monkeypatch.setenv("BENCH_KEY2", "k")
        beats = []
        monkeypatch.setattr(ej.witness, "warn", lambda src, msg: beats.append(msg))

        client, _ = self._fake_client([(500, [], "boom")])
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        assert ej._call_model("hi", "m") == ""
        assert any("http500" in b for b in beats), beats

    def test_warns_when_judge_is_a_committee_member(self, monkeypatch):
        """裁判就是选手之一 → 必须告警（自己评自己，结论作废）。

        直接测 `_warn_same_model` 本身，不绕已删的 v1 入口。
        v2 只有 judge 一个角色（synth 固定传空串），旧的 synth 断言不再适用。
        """
        from singularity.scheduler import execution_judge as ej
        beats = []
        monkeypatch.setattr(ej.witness, "warn", lambda src, msg: beats.append(msg))
        ej._warn_same_model("deepseek-v4-flash", "", ["deepseek-v4-flash", "glm-5.3-flash"])
        assert any("fusion_self_judge:judge" in b for b in beats), beats

    def test_no_warning_when_judge_is_outsider(self, monkeypatch):
        from singularity.scheduler import execution_judge as ej
        beats = []
        monkeypatch.setattr(ej.witness, "warn", lambda src, msg: beats.append(msg))
        ej._warn_same_model("kimi-k3", "", ["deepseek-v4-flash"])
        assert not any("fusion_self_judge" in b for b in beats), beats

    def test_plan_char_limit_is_applied(self, monkeypatch):
        """上限本身要生效（不是把截断整个删掉）。"""
        from singularity.scheduler import execution_judge as ej
        monkeypatch.setattr(ej, "_FUSION_PLAN_CHARS", 100)
        block = ej._plans_block([("A", "X" * 500), ("B", "短")])
        assert "X" * 100 in block
        assert "X" * 101 not in block

    def test_api_resolved_from_registry_not_whitelist(self, monkeypatch):
        """激活模型（如 deepseek-v4-flash）必须能解析出 key/base_url。

        曾硬编码 8 个 id 的白名单，其余模型静默返回空串 —— 下拉里能选、调了没结果。
        """
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import model_registry, api_store

        class _Entry:
            api_key_env = "DEEPSEEK_API_KEY"
            base_url = "https://api.deepseek.com/v1"

        monkeypatch.setattr(model_registry, "provider_for_model",
                            lambda m: "deepseek" if m == "deepseek-v4-flash" else "")
        monkeypatch.setattr(api_store, "get", lambda pid: _Entry() if pid == "deepseek" else None)

        assert ej._resolve_api("deepseek-v4-flash") == ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1")
        # 注册表查不到 → 兜底表
        assert ej._resolve_api("gpt-5.5") == ("OPENAI_API_KEY", "https://api.openai.com/v1")
        # 两边都没有 → 空（调用方会告警并返回 ""）
        assert ej._resolve_api("查无此模型") == ("", "")


# ═══════════════════════════════════════════════════════════════
# no_tools 必须真的禁掉工具（否则委员会"禁工具"只是空话）
# ═══════════════════════════════════════════════════════════════

class TestNoToolsEnforced:

    def test_capability_flags(self):
        """执行器要声明自己能不能禁工具；声明不了 → 调用方告警，不假装禁住了。"""
        from singularity.scheduler.executors.anthropic_api import AnthropicApiExecutor
        from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
        from singularity.scheduler.executors.claude_cli import ClaudeCliExecutor
        from singularity.scheduler.executors.zhipu_api import ZhipuApiExecutor
        assert AnthropicApiExecutor.honors_no_tools is True
        assert OpenAIAgentExecutor.honors_no_tools is True
        assert ZhipuApiExecutor.honors_no_tools is True    # 纯补全，不支持工具
        assert ClaudeCliExecutor.honors_no_tools is False  # claude CLI 自带工具，禁不掉

    def _captured_body(self, monkeypatch, no_tools: bool) -> dict:
        import httpx
        from singularity.scheduler.executors import anthropic_api as aa
        captured = {}

        class _Resp:
            status_code = 200
            text = ""
            def json(self):
                return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json or {})
            return _Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        cfg = {"model": "claude-sonnet-4-6"}
        if no_tools:
            cfg["no_tools"] = True
        ex = aa.AnthropicApiExecutor(
            cfg, "任务", "tid",
            skill_tools=[{"type": "function",
                          "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}],
            skill_prompt="", mcp_tools=[],
        )
        ex.run()
        return captured

    def test_no_tools_drops_tools(self, monkeypatch):
        body = self._captured_body(monkeypatch, no_tools=True)
        assert "tools" not in body, f"禁工具调用仍注入了工具: {body.get('tools')}"

    def test_normal_call_keeps_tools(self, monkeypatch):
        body = self._captured_body(monkeypatch, no_tools=False)
        assert len(body.get("tools", [])) == 1


# ═══════════════════════════════════════════════════════════════
# 架构任务触发判据 + 委员会席位视角
# ═══════════════════════════════════════════════════════════════

class TestArchitectureTrigger:
    """只认强短语。松词（"模块"/"entity"）会把实现任务误送进委员会 ——
    禁工具跑 7 波、拿回架构 JSON 而不是代码。"""

    def test_architect_prompt_triggers(self):
        """架构提示词搬进 roles.toml 后，触发判据必须仍然命中。"""
        from singularity.scheduler.roles import get_role
        from singularity.scheduler.workflow import _ARCHITECT_CONTEXT
        from singularity.scheduler.execution_judge import _is_architecture_task
        role = get_role("architect")
        prompt = f"{role.get_full_prompt()}\n\n" + _ARCHITECT_CONTEXT.format(
            description="x", scope="y", constraints="z", research="w")
        assert _is_architecture_task(prompt) is True

    def test_implementation_tasks_dont_trigger(self):
        from singularity.scheduler.execution_judge import _is_architecture_task
        for t in ["修复登录模块的 token 过期判断",
                  "在 User entity 上增加 email 唯一索引",
                  "把 config 模块拆成两个文件",
                  "实现 /api/tasks 的分页查询"]:
            assert _is_architecture_task(t) is False, f"实现任务误触发委员会: {t}"


class TestCommitteePerspective:
    """席位视角默认关（A/B 盲评：有视角 31 vs 无视角 32，略输）。"""

    def _draft_prompts(self, monkeypatch, tmp_path):
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        seen = []
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, prompt, tag, level, baseline_ref="", cwd="":
                            (seen.append(prompt), '{"architecture":"x"}')[1])
        monkeypatch.setattr(ej, "fuse_architecture_v2", lambda *a, **k: '{"architecture":"fused"}')
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                               [{"model": "m1"}, {"model": "m2"}])
        return seen

    def test_perspective_off_by_default(self, monkeypatch, tmp_path):
        prompts = self._draft_prompts(monkeypatch, tmp_path)
        assert prompts, "委员会没产出初稿"
        assert not any("[你的视角]" in p for p in prompts), "席位视角默认应为关闭"

    def test_perspective_can_be_reenabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QIDIAN_COMMITTEE_PERSPECTIVE", "1")
        prompts = self._draft_prompts(monkeypatch, tmp_path)
        assert sum("[你的视角]" in p for p in prompts) == 2

    def test_partial_committee_warns(self, monkeypatch, tmp_path):
        """有成员没产出必须告警 —— 否则"3 家碰撞"实际只有 1 家，外面看不出来。"""
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        seen = []
        monkeypatch.setattr(de.witness, "warn", lambda *a, **k: seen.append(a))
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, prompt, tag, level, baseline_ref="", cwd="":
                            None if c.get("model") == "m2" else '{"architecture":"x"}')
        monkeypatch.setattr(ej, "fuse_architecture_v2", lambda *a, **k: '{}')
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                               [{"model": "m1"}, {"model": "m2"}])
        assert any("committee_partial" in str(x) for x in seen), seen


class TestCommitteeDegradationVisibility:
    """委员会/融合的降级路径必须留痕。

    `_dispatch_committee` 的「融合空手而归」分支此前**一条测试都没有**，
    而它原本是 `except Exception: pass` —— 失败在这一层完全不可见，
    外面只看到"融合跑完了"。
    """

    def _run(self, monkeypatch, tmp_path, *, fuse=None):
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        seen = []
        monkeypatch.setattr(de.witness, "warn", lambda *a: seen.append(a))
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, p, tag, level, baseline_ref="", cwd="":
                            '{"architecture":"x"}')
        monkeypatch.setattr(de, "_run_executor", lambda *a, **k: None)  # 别真调通用合成
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ej, "fuse_architecture_v2",
                            fuse or (lambda *a, **k: '{"architecture":"fused"}'))
        de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                               [{"model": "m1"}, {"model": "m2"}])
        return [str(x) for x in seen]

    def test_fusion_empty_falls_back_loudly(self, monkeypatch, tmp_path):
        warns = self._run(monkeypatch, tmp_path, fuse=lambda *a, **k: "")
        assert any("fusion_empty_fallback_synthesis" in w for w in warns), warns


class TestFusionMetaHandoff:
    """委员会产物随 DispatchResult 回传，不落 .qidian/.last_fusion.json 全局单文件。

    那个全局文件在并发下会串项目：Flask threaded=True + 调度循环 concurrent=2，
    HTTP(_api_projects.run_phase) 和后台循环(app.py 结果处理) 两条路径都能进委员会，
    两个架构任务写同一路径 → 后写的覆盖先写的，先写的那家读到**别人的**产物；
    读不到时整段静默跳过。另外非委员会路径会捡到上一轮残留的文件。
    """

    def _run(self, monkeypatch, tmp_path):
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, p, tag, level, baseline_ref="", cwd="":
                            '{"architecture":"' + (c.get("model") or "?") + '"}')
        monkeypatch.setattr(ej, "fuse_architecture_v2",
                            lambda *a, **k: '{"architecture":"fused"}')
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        # 显式关掉 v2：这几条测的是**旧两阶段**路径（默认已改成 v2 开）
        return de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                                      [{"model": "m1"}, {"model": "m2"}])

    def test_fusion_meta_rides_on_result(self, monkeypatch, tmp_path):
        """属性名两边必须对得上 —— 对不上就是静默 None，正是这次要防的失败模式。"""
        r = self._run(monkeypatch, tmp_path)
        fm = getattr(r.executor_result, "fusion_meta", None)
        assert fm, "委员会产物没挂在返回结果上 → _run_planning 取不到"
        assert sorted(fm["models"]) == ["m1", "m2"]   # 收集自 wait() 的 done 集合，不保序
        assert fm["fused"] == '{"architecture":"fused"}'
        assert len(fm["outputs"]) == 2

    def test_no_global_handoff_file_left_behind(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path)
        assert not (tmp_path / ".last_fusion.json").exists(), \
            "又写回全局交接文件了 → 并发会串项目"


class TestNoToolsFailureVisibility:
    """_run_no_tools 曾静默吞异常 —— 委员会里模型失败完全看不见，
    只能靠猜是超时还是空输出（实测 3 家阵容 2 家无产出）。"""

    def _run(self, monkeypatch, behavior):
        from singularity.scheduler import _dispatch_exec as de
        seen = []
        monkeypatch.setattr(de.witness, "warn", lambda *a, **k: seen.append(a))
        monkeypatch.setattr(de, "_run_executor", behavior)
        r = de._run_no_tools({"model": "m", "type": "openai-agent"}, "p", "tag", "any")
        return r, seen

    def test_exception_warns(self, monkeypatch):
        def boom(*a, **k):
            raise TimeoutError("240s 超时")
        r, seen = self._run(monkeypatch, boom)
        assert r is None
        assert any("no_tools_fail" in str(x) for x in seen), seen

    def test_empty_output_warns(self, monkeypatch):
        class _R:
            raw_output = ""
            error = "timeout"
        r, seen = self._run(monkeypatch, lambda *a, **k: _R())
        assert r is None
        assert any("no_tools_empty" in str(x) for x in seen), seen

    def test_success_no_warn(self, monkeypatch):
        class _R:
            raw_output = "方案"
            error = ""
        r, seen = self._run(monkeypatch, lambda *a, **k: _R())
        assert r == "方案"
        assert not seen, seen


# ═══════════════════════════════════════════════════════════════
# 禁工具调用的系统提示词（通用 SYSTEM_PROMPT 说"你有工具/直接写代码"，
# 与"输出架构 JSON"冲突 → 模型吐假 tool_call 就结束，初稿作废）
# ═══════════════════════════════════════════════════════════════

class TestNoToolsSystemPrompt:

    def _system_msg(self, monkeypatch, no_tools: bool) -> str:
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setenv("TEST_KEY", "k")
        captured = {}
        cfg = {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x"}
        if no_tools:
            cfg["no_tools"] = True
        ex = oa.OpenAIAgentExecutor(
            cfg, "任务", "tid",
            skill_tools=[], skill_prompt="技能正文: 用 node 渲染架构图", mcp_tools=[],
        )
        monkeypatch.setattr(ex, "_api_call", lambda body: (
            captured.update(body),
            {"choices": [{"message": {"content": "ok"}}]},
        )[1])
        ex.run()
        return captured["messages"][0]["content"]

    def test_no_tools_uses_dedicated_prompt(self, monkeypatch):
        sys_msg = self._system_msg(monkeypatch, no_tools=True)
        assert "你有工具可以用" not in sys_msg, "禁工具调用仍说'你有工具'"
        assert "直接写代码" not in sys_msg, "禁工具调用仍要求写代码"
        assert "[重要] 本次调用已禁用所有工具" in sys_msg, "缺禁令"

    def test_normal_call_keeps_general_prompt(self, monkeypatch):
        sys_msg = self._system_msg(monkeypatch, no_tools=False)
        assert "你有工具可以用" in sys_msg
        assert "[重要] 本次调用已禁用所有工具" not in sys_msg


class TestCallModelEmptyContent:
    """思考模型把 max_tokens 烧在 reasoning 上 → content 空，必须告警不能静默。"""

    def test_warns_on_empty_content(self, monkeypatch):
        from singularity.scheduler import execution_judge as ej
        seen = []
        monkeypatch.setattr(ej, "_resolve_api", lambda m: ("AB_TEST_KEY", "http://x"))
        monkeypatch.setenv("AB_TEST_KEY", "k")
        monkeypatch.setattr(ej.witness, "warn", lambda *a, **k: seen.append(a))

        class _Resp:
            status_code, text = 200, ""
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass
            def iter_lines(self):
                # 只有 reasoning 没有 content，且 finish_reason=length —— 思考模型烧光额度
                yield 'data: {"choices":[{"delta":{"reasoning_content":"想"},"finish_reason":"length"}]}'
                yield 'data: [DONE]'

        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def stream(self, *a, **k): return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "Client", lambda **k: _C())

        assert ej._call_model("p", "some-model") == ""
        assert any("empty_content" in str(a) for a in seen), seen


# ═══════════════════════════════════════════════════════════════
# _api_call 分层重试（Temporal 五字段语义）—— 以前网络错误一次就判任务失败
# ═══════════════════════════════════════════════════════════════

class TestApiCallRetry:

    def _ex(self, monkeypatch):
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setenv("TEST_KEY", "k")
        monkeypatch.setattr(oa, "_RETRY_INITIAL", 0)
        monkeypatch.setattr(oa, "_RETRY_MAX_INTERVAL", 0)
        ex = oa.OpenAIAgentExecutor(
            {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x"},
            "任务", "tid", [], "", [])
        return oa, ex

    def test_network_error_is_retried_until_success(self, monkeypatch):
        oa, ex = self._ex(monkeypatch)
        calls = []

        def once(body):
            calls.append(1)
            if len(calls) < 3:
                raise oa._NetworkError("超时")
            return {"ok": True}

        monkeypatch.setattr(ex, "_api_call_once", once)
        assert ex._api_call({}) == {"ok": True}
        assert len(calls) == 3, calls

    def test_gives_up_after_max_attempts(self, monkeypatch):
        oa, ex = self._ex(monkeypatch)
        monkeypatch.setattr(oa, "_RETRY_MAX_ATTEMPTS", 3)
        calls = []

        def once(body):
            calls.append(1)
            raise oa._NetworkError("超时")

        monkeypatch.setattr(ex, "_api_call_once", once)
        with pytest.raises(oa._NetworkError):
            ex._api_call({})
        assert len(calls) == 3, calls

    def test_4xx_is_not_retried(self, monkeypatch):
        """4xx 重试也不会好（non_retryable）。"""
        oa, ex = self._ex(monkeypatch)
        calls = []

        def once(body):
            calls.append(1)
            raise oa._FormatError("HTTP 400")

        monkeypatch.setattr(ex, "_api_call_once", once)
        with pytest.raises(oa._FormatError):
            ex._api_call({})
        assert len(calls) == 1, calls

    def test_total_budget_stops_retries(self, monkeypatch):
        """超预算就不再重试 —— 防止 3×240s 撞穿 900s deadline。"""
        oa, ex = self._ex(monkeypatch)
        monkeypatch.setattr(oa, "_RETRY_TOTAL_BUDGET", -1)   # 一开始就已超预算
        calls = []

        def once(body):
            calls.append(1)
            raise oa._NetworkError("超时")

        monkeypatch.setattr(ex, "_api_call_once", once)
        with pytest.raises(oa._NetworkError):
            ex._api_call({})
        assert len(calls) == 1, calls

    def test_5xx_transient_4xx_format(self, monkeypatch):
        """状态码分类：5xx 可重试，4xx 不可。（非流式路径）"""
        oa, ex = self._ex(monkeypatch)
        monkeypatch.setattr(oa, "_STREAM", False)

        class _R:
            def __init__(self, code):
                self.status_code, self.text = code, "boom"

        class _C:
            def __init__(self, code): self.code = code
            def post(self, *a, **k): return _R(self.code)

        monkeypatch.setattr(oa, "_get_http_client", lambda: _C(503))
        with pytest.raises(oa._TransientError):
            ex._api_call_once({})

        monkeypatch.setattr(oa, "_get_http_client", lambda: _C(400))
        with pytest.raises(oa._FormatError):
            ex._api_call_once({})


# ═══════════════════════════════════════════════════════════════
# 流式调用 + 停滞检测（read timeout = 多久没新 token 就断开）
# ═══════════════════════════════════════════════════════════════

class TestStreamCall:

    def _ex(self, monkeypatch):
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setenv("TEST_KEY", "k")
        ex = oa.OpenAIAgentExecutor(
            {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x"},
            "任务", "tid", [], "", [])
        return oa, ex

    def _client(self, oa, lines, code=200):
        class _Resp:
            def __init__(self): self.status_code, self.text = code, "boom"
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass
            def iter_lines(self):
                for l in lines:
                    yield l

        class _C:
            def stream(self, *a, **k): return _Resp()
        return _C()

    def test_assembles_content_and_usage(self, monkeypatch):
        oa, ex = self._ex(monkeypatch)
        lines = [
            'data: {"choices":[{"delta":{"content":"消息"}}]}',
            'data: {"choices":[{"delta":{"content":"队列"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":42}}',
            'data: [DONE]',
        ]
        monkeypatch.setattr(oa, "_get_http_client", lambda: self._client(oa, lines))
        d = ex._stream_call({})
        assert d["choices"][0]["message"]["content"] == "消息队列"
        assert d["choices"][0]["finish_reason"] == "stop"
        assert d["usage"]["total_tokens"] == 42

    def test_assembles_tool_calls(self, monkeypatch):
        oa, ex = self._ex(monkeypatch)
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"write_","arguments":"{\\"pa"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"file","arguments":"th\\":\\"a\\"}"}}]}}]}',
            'data: [DONE]',
        ]
        monkeypatch.setattr(oa, "_get_http_client", lambda: self._client(oa, lines))
        tc = ex._stream_call({})["choices"][0]["message"]["tool_calls"][0]
        assert tc["id"] == "c1"
        assert tc["function"]["name"] == "write_file"
        assert tc["function"]["arguments"] == '{"path":"a"}'

    def test_emits_throttled_progress_event(self, monkeypatch):
        """进度必须上流到 SSE（前端任务卡滚动日志），且是节流后的。"""
        oa, ex = self._ex(monkeypatch)
        monkeypatch.setattr(oa, "_PROGRESS_INTERVAL", 0)      # 关掉节流
        oa._pending_sse_events.clear()
        lines = [
            'data: {"choices":[{"delta":{"content":"消息"}}]}',
            'data: {"choices":[{"delta":{"content":"队列"}}]}',
            'data: [DONE]',
        ]
        monkeypatch.setattr(oa, "_get_http_client", lambda: self._client(oa, lines))
        ex._stream_call({})
        gen = [e for e in oa._pending_sse_events if e.get("kind") == "gen"]
        assert gen, oa._pending_sse_events
        assert gen[-1]["task_id"] == "tid"
        assert "2 字" in gen[0]["msg"], gen[0]      # 首条是累计 2 字
        assert "4 字" in gen[-1]["msg"], gen[-1]    # 末条是累计 4 字

    def test_progress_is_throttled(self, monkeypatch):
        """节流开着时，一瞬间的多个 chunk 只推一条 —— 否则 SSE 会被淹掉。"""
        oa, ex = self._ex(monkeypatch)
        monkeypatch.setattr(oa, "_PROGRESS_INTERVAL", 60)     # 60s 内只推一条
        oa._pending_sse_events.clear()
        lines = ['data: {"choices":[{"delta":{"content":"x"}}]}'] * 5 + ['data: [DONE]']
        monkeypatch.setattr(oa, "_get_http_client", lambda: self._client(oa, lines))
        ex._stream_call({})
        gen = [e for e in oa._pending_sse_events if e.get("kind") == "gen"]
        assert len(gen) <= 1, gen

    def test_stall_raises_network_error(self, monkeypatch):
        """read timeout = 停滞 → 抛 _NetworkError（可重试），且连接随之关闭。"""
        oa, ex = self._ex(monkeypatch)
        import httpx

        class _Resp:
            status_code, text = 200, ""
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass
            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"开头"}}]}'
                raise httpx.ReadTimeout("stalled")

        class _C:
            def stream(self, *a, **k): return _Resp()

        monkeypatch.setattr(oa, "_get_http_client", lambda: _C())
        with pytest.raises(oa._NetworkError) as e:
            ex._stream_call({})
        assert "停滞" in str(e.value)
