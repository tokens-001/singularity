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
    _extract_findings,
    _needs_human_confirm,
)
from singularity.scheduler._types import RunContext, BatchOutput


# ═══════════════════════════════════════════════════════════════
# 辅助工厂
# ═══════════════════════════════════════════════════════════════

def _task(**kw):
    defaults = {
        "id": "1234567890", "description": "测试任务",
        "route_level": "E", "route_gate": False, "route_type": "default",
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
        "level": "E", "attempts": 1,
    })()


# ═══════════════════════════════════════════════════════════════
# _decide_cascade — 5 分支决策
# ═══════════════════════════════════════════════════════════════

class TestDecideCascade:
    """cascade routing 决策树: pass / cascade_accept / retry / cascade_skip / 终态。"""

    def _call(self, validation, quality=None, fallback_chain=None):
        return _decide_cascade(
            task=_task(),
            level="E",
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
# _extract_findings — 从文本提取发现列表
# ═══════════════════════════════════════════════════════════════

class TestExtractFindings:
    def test_dash_list(self):
        text = "- 发现1\n- 发现2\n普通文本\n- 发现3"
        assert _extract_findings(text) == ["发现1", "发现2", "发现3"]

    def test_bullet_list(self):
        text = "• 问题A\n• 问题B"
        assert _extract_findings(text) == ["问题A", "问题B"]

    def test_numbered_list(self):
        text = "1. 第一点\n2. 第二点"
        assert _extract_findings(text) == ["第一点", "第二点"]

    def test_empty(self):
        assert _extract_findings("") == []
        assert _extract_findings("没有列表项的普通文本。") == []

    def test_capped_at_10(self):
        text = "\n".join(f"- 发现{i}" for i in range(20))
        assert len(_extract_findings(text)) == 10


# ═══════════════════════════════════════════════════════════════
# _needs_human_confirm — 安全/架构关键词检测
# ═══════════════════════════════════════════════════════════════

class TestNeedsHumanConfirm:
    def test_safety_keyword_triggers(self):
        assert _needs_human_confirm("删除数据库操作", "") is True
        assert _needs_human_confirm("", "SQL注入漏洞") is True
        assert _needs_human_confirm("sudo rm -rf /", "") is True

    def test_arch_keyword_triggers(self):
        assert _needs_human_confirm("数据库迁移方案", "") is True
        assert _needs_human_confirm("", "API破坏性变更") is True

    def test_normal_task_no_trigger(self):
        assert _needs_human_confirm("添加日志输出", "优化了性能瓶颈") is False
        assert _needs_human_confirm("修复 CSS 样式", "") is False

    def test_case_insensitive(self):
        assert _needs_human_confirm("修复 xss 漏洞", "") is True
        assert _needs_human_confirm("fix sql注入 here", "") is True


# ═══════════════════════════════════════════════════════════════
# _save_planner_patch / _read_planner_patch — D层方案持久化
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
            "mem_mod.archive_experience": lambda *a, **k: None,
            "rl_mod.load_learner": lambda: type("L", (), {"record": lambda *a, **k: None})(),
            "rl_mod.save_learner": lambda l: None,
            "chan_mod.assess": lambda desc, reason, files: type("R", (), {"severity": "info"})(),
            "chan_mod.save_report": lambda r: None,
            "snap_mod.rollback": lambda s: None,
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
        route = route or type("R", (), {"level": "E", "task_type": "default"})()

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
        def record_rollback(s):
            rollback_called.append(s)

        reason, results, _ = self._call(
            monkeypatch,
            batch=self._make_batch(validation=val, term_reason="rollback"),
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
            batch=self._make_batch(validation=val, term_reason="escalation_exhausted (level=E)"),
            **{
                "_read_planner_patch": lambda tid: "D层分析方案内容",
                "tracker.transition": record_transition,
            },
        )
        assert "escalated_to_E+" in reason
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
            batch=self._make_batch(validation=val, term_reason="abort: 失败"),
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
            batch=self._make_batch(validation=val, term_reason="abort: 失败"),
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
            batch=self._make_batch(validation=val, term_reason="abort: 失败"),
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
                type("R", (), {"level": "E", "task_type": "default"})(),
                snap, results,
            )
        assert "; QA:fail" in reason

    # ── 边界: depth >= MAX → 不自动拆分 ──
    def test_max_depth_no_auto_decompose(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()
        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        task = self._make_task(retry_count=3, max_retries=3, depth=3)  # depth=MAX
        reason, results, _ = self._call(
            monkeypatch, task=task,
            batch=self._make_batch(validation=val, term_reason="abort"),
            **{"tracker.transition": record_transition},
        )
        # depth=3 >= _MAX_DEPTH=3 → 不拆分, 直接 FAILED
        assert "failed" in reason or "exhausted" in reason
        assert any("FAILED" in str(s) for _, s, _ in transitions)
