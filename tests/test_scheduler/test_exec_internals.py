"""_exec.py 内部函数单元测试 — 白盒覆盖关键分支。

ponytail: 只测分支密度最高的 leaf 函数。run() 路径已由 test_exec_run.py 覆盖。
"""

import os, sys, json, tempfile, time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from singularity.scheduler._exec import (
    _build_effective_task,
    _check_cancelled,
    _decide_cascade,
    _premium_first,
)
from singularity.scheduler._types import RunContext, BatchOutput


# ═══════════════════════════════════════════════════════════════
# force_premium 重排（2026-09-12 补）
#
# 三个"重排点"里唯一**既没测试也没 docs** 的一个（另两个有 test_phase_models_wiring
# / test_router 钉着）。这里钉的是**两道闸门**，不是"哪个模型更贵"：
#   ① 重试次数不到阈值 → 不动
#   ② 用户点名了名单 → 不动
# ═══════════════════════════════════════════════════════════════

def _chain(*models):
    return [{"model": m} for m in models]


# 价目表打桩：测试**不该依赖用户那份会改的 `model_prices.json`**
# （它读 config.QIDIAN_DIR，测试环境里本来是空的）。
_FAKE_PRICES = {"贵-2.0": 2.0, "中-0.92": 0.92, "便宜-0.48": 0.48}


@pytest.fixture
def fake_prices(monkeypatch):
    import singularity.scheduler.model_prices as mp
    monkeypatch.setattr(mp, "load_prices", lambda: dict(_FAKE_PRICES))
    return _FAKE_PRICES


class TestPremiumFirst:
    def test_重试到阈值才重排(self, fake_prices):
        c = _chain("便宜-0.48", "中-0.92")
        assert [a["model"] for a in _premium_first(c, True, False)] == ["中-0.92", "便宜-0.48"]
        assert [a["model"] for a in _premium_first(c, False, False)] == ["便宜-0.48", "中-0.92"]

    def test_受限时不重排(self, fake_prices):
        """用户点名了主力 —— 不该因为"重试过两次"被悄悄换掉。"""
        c = _chain("便宜-0.48", "中-0.92")
        assert [a["model"] for a in _premium_first(c, True, True)] == ["便宜-0.48", "中-0.92"]

    def test_空链不炸(self, fake_prices):
        assert _premium_first([], True, False) == []

    def test_只改顺序不改成员(self, fake_prices):
        c = _chain("便宜-0.48", "贵-2.0", "中-0.92")
        out = _premium_first(c, True, False)
        assert sorted(a["model"] for a in out) == sorted(a["model"] for a in c)
        assert [a["model"] for a in out] == ["贵-2.0", "中-0.92", "便宜-0.48"]

    def test_按价格排_不是按名字排(self, monkeypatch):
        """**回归**：这条是当初那个缺陷的反证。

        以前按**模型名子串**判 premium（含 "glm"/"opus" 就算）。价目表里最便宜的
        `glm-5.3-flash`(0.25) 名字带 "glm"，重试时会被提到 `deepseek-v4-pro`(1.848) 前面。
        现在读实价 —— 名字里带什么都不影响。
        """
        import singularity.scheduler.model_prices as mp
        monkeypatch.setattr(mp, "load_prices",
                            lambda: {"glm-5.3-flash": 0.25, "deepseek-v4-pro": 1.848})
        out = _premium_first(_chain("glm-5.3-flash", "deepseek-v4-pro"), True, False)
        assert [a["model"] for a in out] == ["deepseek-v4-pro", "glm-5.3-flash"]

    def test_没配价的排最后(self, fake_prices):
        """不知道贵不贵 → 不优先（但也不丢掉）。"""
        out = _premium_first(_chain("没配过价的模型", "中-0.92"), True, False)
        assert [a["model"] for a in out] == ["中-0.92", "没配过价的模型"]

    def test_同价保持原顺序(self, monkeypatch):
        import singularity.scheduler.model_prices as mp
        monkeypatch.setattr(mp, "load_prices", lambda: {"a": 1.0, "b": 1.0, "c": 1.0})
        c = _chain("a", "b", "c")
        assert [x["model"] for x in _premium_first(c, True, False)] == ["a", "b", "c"]

    def test_读不到价目表就保持原样(self, monkeypatch):
        """读价失败不能把执行拖挂 —— 原顺序返回，什么都不动。"""
        import singularity.scheduler.model_prices as mp
        def _boom():
            raise OSError("价目表读不到")
        monkeypatch.setattr(mp, "load_prices", _boom)
        c = _chain("a", "b", "c")
        assert [x["model"] for x in _premium_first(c, True, False)] == ["a", "b", "c"]


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

    # ── 分支4: D方案 + 升级链耗尽 → E+ 修复 ──
    # ⚠️ 输入词 2026-09-14 改过：生产侧 `_exec.py:755` 早就不写 `escalation_exhausted`
    # 了（改成 `no_escalation_path`），而这条测试一直拿**旧词**当输入 —— 全绿地
    # 给一个**永不触发**的分支作证（外派⑤核出、我复核属实）。改成现役词。
    def test_dplan_escalation_to_eplus(self, monkeypatch):
        val = type("V", (), {"action": "abort", "verdict": "阻断"})()

        transitions = []
        def record_transition(tid, status, **kw):
            transitions.append((tid, status.name if hasattr(status, 'name') else str(status), kw))

        reason, results, _ = self._call(
            monkeypatch,
            batch=self._make_batch(ok=False, validation=val, term_reason="no_escalation_path (level=any)"),
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

            # 2026-09-16：被测代码改用 `iter_text()`（按**字节批**吐、行切分自己做）。
            # 替身必须跟着真实类型长大，否则替身喂的路径和线上不是同一条。
            def iter_text(self):
                for l in self._lines:
                    yield l + "\n"

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
        """提取员就是选手之一 → 必须告警（自己给自己出题，结论作废）。

        直接测 `_warn_same_model` 本身，不绕已删的 v1 入口。
        v2 这类位置只有提取员一个（旧的 judge/synth 双角色是 v1 的）。
        """
        from singularity.scheduler import execution_judge as ej
        beats = []
        monkeypatch.setattr(ej.witness, "warn", lambda src, msg: beats.append(msg))
        ej._warn_same_model("deepseek-v4-flash", ["deepseek-v4-flash", "glm-5.3-flash"], role="extractor")
        assert any("fusion_self_judge:extractor" in b for b in beats), beats

    def test_no_warning_when_judge_is_outsider(self, monkeypatch):
        from singularity.scheduler import execution_judge as ej
        beats = []
        monkeypatch.setattr(ej.witness, "warn", lambda src, msg: beats.append(msg))
        ej._warn_same_model("kimi-k3", ["deepseek-v4-flash"], role="extractor")
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


class TestCommitteeRoleVeto:
    """角色否决：关键词命中但这是实现活儿 → 不进委员会（2026-09-11 外派评审）。

    planner 拆出来的子任务描述是**从架构 JSON 的 title/desc 抄的**，天然继承
    「技术栈 / 模块划分 / 数据模型」这些词。只按关键词判 → 实现任务也被送进
    委员会，而委员会走 no_tools：跑几波拿回来的是一份架构 JSON，不是代码。
    """

    def _spy(self, monkeypatch):
        """让 dispatch 走通，并记录委员会有没有被进。"""
        from types import SimpleNamespace
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        monkeypatch.setattr(ej, "_is_architecture_task", lambda t: True)   # 关键词恒命中
        monkeypatch.setattr(de, "pick_agent_fallback_chain",
                            lambda *a, **k: [{"model": "m1", "type": "claude-cli"},
                                             {"model": "m2", "type": "claude-cli"}])
        entered = []
        monkeypatch.setattr(de, "_dispatch_committee",
                            lambda *a, **k: entered.append(1) or "COMMITTEE")
        monkeypatch.setattr(de, "_run_executor",
                            lambda *a, **k: SimpleNamespace(raw_output="SINGLE"))
        return entered

    def test_implementer_role_vetoes_committee(self, monkeypatch):
        from singularity.scheduler import _dispatch_exec as de
        entered = self._spy(monkeypatch)
        res = de.dispatch("技术栈 模块划分", "any", "t", {},
                          route_role="implementer")
        assert entered == [], "实现角色不该进委员会"
        assert res.executor_result.raw_output == "SINGLE"

    def test_empty_role_still_allows_committee(self, monkeypatch):
        """架构阶段走 _safe_dispatch 不带 route_role —— 不能把它一起关掉。"""
        from singularity.scheduler import _dispatch_exec as de
        entered = self._spy(monkeypatch)
        assert de.dispatch("技术栈 模块划分", "any", "t", {}) == "COMMITTEE"
        assert entered == [1]

    def test_non_implementer_role_allows_committee(self, monkeypatch):
        from singularity.scheduler import _dispatch_exec as de
        entered = self._spy(monkeypatch)
        assert de.dispatch("技术栈 模块划分", "any", "t", {},
                           route_role="architect") == "COMMITTEE"
        assert entered == [1]

    def test_veto_helper_reads_phase_role(self, monkeypatch, tmp_path):
        """不写死 "implementer"：用户改过执行角色时要跟着走。"""
        import json
        from singularity.scheduler import config as cfg
        from singularity.scheduler import _dispatch_exec as de
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        assert de._impl_role_veto("") is False
        assert de._impl_role_veto("architect") is False
        assert de._impl_role_veto("implementer") is True
        (tmp_path / "phases.json").write_text(
            json.dumps({"executing": "builder"}), encoding="utf-8")
        assert de._impl_role_veto("builder") is True, "改了执行角色后应跟着走"
        assert de._impl_role_veto("implementer") is False


class TestCommitteePerspective:
    """席位视角默认关（A/B 盲评：有视角 31 vs 无视角 32，略输）。"""

    def _draft_prompts(self, monkeypatch, tmp_path):
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        seen = []
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, prompt, tag, level, baseline_ref="", cwd="":
                            (seen.append(prompt), ('{"architecture":"x"}', 0, 0.0))[1])
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
                            None if c.get("model") == "m2" else ('{"architecture":"x"}', 0, 0.0))
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
                            ('{"architecture":"x"}', 0, 0.0))
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

    def test_fallback_synthesizer_is_tool_free(self, monkeypatch, tmp_path):
        """兜底合成也必须禁工具 —— 否则它会往奇点自己的仓库里写文件。

        实测（2026-09-11 真流水线）：融合失败 → 兜底合成 → 合成 agent **带着工具**、
        cwd 是奇点仓库根 → 把目标项目的架构写成了 `docs/ARCHITECTURE.json`。
        委员会本身禁工具（`_run_no_tools`），这条兜底漏了。
        """
        from singularity.scheduler import _dispatch_exec as de
        from singularity.scheduler import execution_judge as ej
        from singularity.scheduler import config as cfg
        seen = {}
        monkeypatch.setattr(de.witness, "warn", lambda *a: None)
        monkeypatch.setattr(de, "_run_no_tools",
                            lambda c, p, tag, level, baseline_ref="", cwd="":
                            ('{"architecture":"x"}', 0, 0.0))

        def fake_run_executor(executor_cls, agent_cfg, prompt, tag, level, **kw):
            seen["agent_cfg"] = agent_cfg
            return None                      # 合成"失败"，走完这条分支即可

        monkeypatch.setattr(de, "_run_executor", fake_run_executor)
        monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
        monkeypatch.setattr(ej, "fuse_architecture_v2", lambda *a, **k: "")
        de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                               [{"model": "m1"}, {"model": "m2"}])
        assert seen.get("agent_cfg", {}).get("no_tools") is True, seen


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
                            ('{"architecture":"' + (c.get("model") or "?") + '"}', 0, 0.0))
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
        assert r[0] == "方案"
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
            def iter_text(self):
                # 只有 reasoning 没有 content，且 finish_reason=length —— 思考模型烧光额度
                yield 'data: {"choices":[{"delta":{"reasoning_content":"想"},"finish_reason":"length"}]}\n'
                yield 'data: [DONE]\n'

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
            def iter_text(self):
                for l in lines:
                    yield l + "\n"

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

    def test_bad_chunk_不杀整轮(self, monkeypatch):
        """坏帧（截断 / 厂商噪声）只**跳过 + 告警**，不能抛穿。

        抛穿的代价不是"少一个 token"：它会穿出 `_stream_call` → 被当成
        "agent 失败" → 整条 fallback 链全灭 → **任务 0 产物**。
        实测 2026-09-12：glm-5.3-flash 一个坏帧
        （`Unterminated string ... (char 187)`）就让 `any` 层两个 agent 一起废掉。
        """
        oa, ex = self._ex(monkeypatch)
        seen = []
        monkeypatch.setattr(oa.witness, "warn", lambda src, msg: seen.append(msg))
        lines = [
            'data: {"choices":[{"delta":{"content":"前"}}]}',
            'data: {"choices":[{"delta":{"content":"截断',      # ← 坏帧：JSON 没闭合
            'data: {"choices":[{"delta":{"content":"后"}}]}',
            'data: [DONE]',
        ]
        monkeypatch.setattr(oa, "_get_http_client", lambda: self._client(oa, lines))
        d = ex._stream_call({})                                  # 不应抛
        assert d["choices"][0]["message"]["content"] == "前后"   # 坏帧前后的都还在
        assert any("sse_chunk_unparsed:1" in m for m in seen), seen  # 但要明报

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
            def iter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"开头"}}]}\n'
                raise httpx.ReadTimeout("stalled")

        class _C:
            def stream(self, *a, **k): return _Resp()

        monkeypatch.setattr(oa, "_get_http_client", lambda: _C())
        with pytest.raises(oa._NetworkError) as e:
            ex._stream_call({})
        assert "停滞" in str(e.value)


# ═══════════════════════════════════════════════════════════════
# 执行器自查总预算（2026-09-13 补）
#
# 症状：三个任务撞 orchestrator 的 900s 收割（8003 / 8384 / 8386），死法都是
# "一直跑到被砍"。根因不是"哪个模型卡住了"，是**执行器的 turn 循环一圈表都不看**
# —— 唯一的上限就是外面那次无声收割，砍完 token / 改动文件 / 轮次全丢。
# 修法：执行器提前 TASK_WRAPUP_MARGIN_S 自己收尾，把已知事实交回去。
# 这里钉两道闸门：轮间看表、以及"看表之后别再把账丢掉"。
# ═══════════════════════════════════════════════════════════════

class TestExecutorBudgetWrapup:

    def _ex(self, monkeypatch, budget):
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setenv("TEST_KEY", "k")
        monkeypatch.setattr(oa, "_EXEC_BUDGET", budget)
        cfg = {"model": "m", "api_key_env": "TEST_KEY", "entry": "http://x", "max_turns": 5}
        return oa, oa.OpenAIAgentExecutor(
            cfg, "任务", "tid", skill_tools=[], mcp_tools=[])

    @staticmethod
    def _tool_reply(_body):
        """一次"模型要调工具"的响应 —— 让循环有第二轮的由头。"""
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]}}],
            "usage": {"total_tokens": 7}}

    def test_budget_already_spent_makes_no_call(self, monkeypatch):
        """预算已到 → **一个调用都不发**，直接收尾。"""
        oa, ex = self._ex(monkeypatch, budget=0.0)
        calls = []
        monkeypatch.setattr(ex, "_api_call", lambda b: (calls.append(b), self._tool_reply(b))[1])
        r = ex.run()
        assert calls == [], "预算已到还在发模型调用"
        assert r.error_kind == "deadline"
        assert r.success is False, "活没干完不许谎报成功"

    def test_wrapup_carries_the_account(self, monkeypatch):
        """撞预算收尾时，**已经烧掉的 token / 跑过的轮次 / 改过的文件要跟着交回去**。

        这是这次修改的全部意义：以前被 900s 砍掉 = 什么都没留下（8003 的
        `token_count=None` / `turns=0` 就是这么来的）。
        """
        oa, ex = self._ex(monkeypatch, budget=600.0)
        calls = []

        def _call(body):
            calls.append(body)
            ex._deadline_at = time.time() - 1     # 这次调用之后预算就过点了
            return self._tool_reply(body)

        monkeypatch.setattr(ex, "_api_call", _call)
        r = ex.run()
        assert len(calls) == 1, "过点之后不该再开新轮"
        assert r.error_kind == "deadline", "收尾要能被上层识别成'别换模型重来'"
        assert r.token_count == 7, "收尾把已烧的 token 丢了 —— 那和被砍没区别"
        assert r.tool_events, "收尾把跑过的轮次丢了"

    def test_budget_comes_from_caller_not_own_start(self, monkeypatch):
        """**接线**：执行器用调用方给的"还剩多久"，不是自己那个 `start`。

        钉的是 2026-09-13 查明的真根因（`docs/防御模式.md` §67）：执行器是
        `_run_executor` **每次 dispatch 新建**的，用 `start` 起算等于每 dispatch 归零
        ⇒ 任务跑过 ≥2 次 dispatch 就永远不收尾，人却被外面 900s 无声收割。

        判据只有一种解释：给一个**比 `_EXEC_BUDGET` 小得多**的 budget，
        `_deadline_at` 必须落在它上面。接线断了就会落在 600 上 ⇒ 红。
        """
        oa, ex = self._ex(monkeypatch, budget=600.0)
        ex.budget_s = 12.0
        before = time.time()
        monkeypatch.setattr(ex, "_api_call", lambda b: self._tool_reply(b))
        ex.run()
        assert ex._deadline_at <= before + 12.5, (
            f"budget_s=12 没生效 —— _deadline_at 落在 {ex._deadline_at - before:.0f}s 后，"
            f"说明还是按自己的 start + _EXEC_BUDGET(600) 算的")
        assert ex._deadline_at >= before, "死线不该早于起跑时刻"

    def test_zero_budget_wraps_up_immediately(self, monkeypatch):
        """`budget_s <= 0`（任务那把尺已经用完）⇒ 第 1 轮就收尾，一个调用都别再发。

        这是"两层预算同源"的**兑现点**：外面马上要砍了，执行器必须立刻交出已知事实，
        否则又回到"跑到被砍、砍完无账"。
        """
        oa, ex = self._ex(monkeypatch, budget=600.0)
        ex.budget_s = -5.0
        calls = []
        monkeypatch.setattr(ex, "_api_call",
                            lambda b: (calls.append(b), self._tool_reply(b))[1])
        r = ex.run()
        assert calls == [], "预算已经用完了还在发模型调用"
        assert r.error_kind == "deadline"

    def test_env_knob_still_wins_over_a_bigger_task_budget(self, monkeypatch):
        """`budget_s` 比 `QIDIAN_EXEC_BUDGET` 大时，**开关必须仍然优先**。

        否则这次改动会把 §63 那套真机验法**悄悄废掉** —— 那套是
        "设 `QIDIAN_EXEC_BUDGET=15` 起后端，看它自己在 15 秒收尾"，
        靠的就是"小值优先"。判据只有一种解释：任务尺给 800，开关给 15，
        收尾必须落在 15 上；写成 `max` 就会落在 800 上 ⇒ 红。
        """
        oa, ex = self._ex(monkeypatch, budget=15.0)
        ex.budget_s = 800.0
        before = time.time()
        monkeypatch.setattr(ex, "_api_call", lambda b: self._tool_reply(b))
        ex.run()
        assert ex._deadline_at <= before + 16.0, (
            f"QIDIAN_EXEC_BUDGET=15 被任务预算盖掉了 "
            f"（_deadline_at 落在 {ex._deadline_at - before:.0f}s 后）⇒ 真机验法失效")

    def test_none_budget_keeps_old_behaviour(self, monkeypatch):
        """`budget_s=None` = 调用方不管（goal_loop / 阶段级那条路）⇒ 退回老行为。

        没有这条，`None` 会等价于 0 ⇒ 那两条路**一次模型调用都不发**、任务直接判死。
        """
        oa, ex = self._ex(monkeypatch, budget=600.0)
        ex.budget_s = None
        calls = []
        monkeypatch.setattr(ex, "_api_call",
                            lambda b: (calls.append(b), self._tool_reply(b))[1])
        ex.run()
        assert calls, "budget_s=None 被当成'预算已到'了 —— 那两条路会被饿死"

    def test_wrapup_is_not_retried(self, monkeypatch):
        """收尾结果**不许再重试** —— 重试就是把剩下的时间再烧一遍。"""
        from singularity.scheduler._exec import _run_with_retry
        attempts = []

        class _R:
            executor_result = MagicMock(ok=False)
            agent_cfg = {"model": "m"}

        class _D:
            executor_result = _R.executor_result
            agent_cfg = {"model": "m"}

        batch = BatchOutput(ok=False, task_id="tid", deadline_wrapup=True,
                            term_reason="no_escalation_path (level=any, last_action=abort)",
                            validation=MagicMock())
        task = MagicMock(max_retries=3)
        monkeypatch.setattr("singularity.scheduler._exec.run",
                            lambda *a, **k: (attempts.append(1), batch)[1])
        out = _run_with_retry(task, MagicMock(retry_count=0, merge_queue=None), {})
        assert out is batch
        assert len(attempts) == 1, f"撞预算收尾被重试了 {len(attempts)} 次"


# ═══════════════════════════════════════════════════════════════
# 「一把尺」的接线（2026-09-13 补，§67）
#
# 上面那组测的是**执行器内部**认不认 budget_s。这里测的是**外面那两段线**：
#   ① `_exec.run` 从 `ctx.deadline_at` 倒推出 budget_s（每次重算）
#   ② `_run_executor` 把它真的挂到执行器上
# 少了任何一段，执行器内部再对也没用 —— 它还是会退回"按自己 start 起算"，
# 也就是每 dispatch 归零、永远不收尾。**"函数对"≠"接线通"。**
# ═══════════════════════════════════════════════════════════════

class TestBudgetWiring:

    def test_derives_from_ctx_deadline_and_a_clock_keeps_ticking(self):
        """`_dispatch_budget_s` 从任务那把尺倒推，且**每次调用都重算**。

        重算是要害：不然"第二次 dispatch 拿到的是第一次那一刻的剩余量"，
        多轮任务照样晚收尾。
        """
        from singularity.scheduler._exec import _dispatch_budget_s
        from singularity.scheduler import config

        ctx = RunContext(batch_id="t", snapshot_ref="r",
                         deadline_at=time.time() + config.TASK_DEADLINE_S)
        first = _dispatch_budget_s(ctx)
        assert first is not None
        # 起点：900 − 90 = 810 上下（扣掉这行代码自己花的时间）
        assert 800 < first <= config.TASK_DEADLINE_S - config.TASK_WRAPUP_MARGIN_S

        ctx.deadline_at -= 300          # 模拟"已经花掉 300 秒"
        assert _dispatch_budget_s(ctx) < first - 250, "没有重算 —— 还在用上一次的剩余量"

    def test_no_deadline_means_none_not_zero(self):
        """`deadline_at == 0`（没给）⇒ `None`。**不是 0** —— 0 会让执行器立刻收尾。"""
        from singularity.scheduler._exec import _dispatch_budget_s
        assert _dispatch_budget_s(RunContext(batch_id="t", snapshot_ref="r")) is None

    def test_budget_already_blown_goes_negative(self):
        """任务那把尺已经过点 ⇒ 负数（执行器据此立刻收尾），不是 `None`。"""
        from singularity.scheduler._exec import _dispatch_budget_s
        from singularity.scheduler import config
        ctx = RunContext(batch_id="t", snapshot_ref="r",
                         deadline_at=time.time() - config.TASK_DEADLINE_S)
        got = _dispatch_budget_s(ctx)
        assert got is not None and got < 0

    def test_run_executor_hangs_budget_on_the_executor(self, monkeypatch):
        """`_run_executor` 把 budget_s 挂到执行器实例上 —— 这是唯一的接头。

        用真 `BaseExecutor` 子类，顺便钉住"默认值是 `None`"（不设时不能是 0）。
        """
        from singularity.scheduler import dispatcher      # 先导它绕开循环 import
        from singularity.scheduler import _dispatch_exec as dx
        from singularity.scheduler.executors.base import BaseExecutor, ExecutorResult

        monkeypatch.setattr(dx, "_load_skills_for_agent", lambda *a, **k: ({}, [], {}))
        monkeypatch.setattr(dx, "_load_mcp_for_agent", lambda *a, **k: ([], None))
        monkeypatch.setattr(dx, "_make_permission_checker", lambda *a, **k: None)

        assert BaseExecutor.budget_s is None, "类属性默认值被改了 —— 不设时会变成'立即到期'"

        seen = {}

        class _Ex(BaseExecutor):
            def run(self):
                seen["budget_s"] = self.budget_s
                return ExecutorResult(success=True)

        dx._run_executor(_Ex, {}, "任务", "tid", "any", budget_s=42.5)
        assert seen["budget_s"] == 42.5, "budget_s 没接到执行器上 —— 它还会按自己 start 起算"


# ═══════════════════════════════════════════════════════════════
# 收尾是**终态**，不是"这个模型空输出"（2026-09-13 真机抓到的）
# ═══════════════════════════════════════════════════════════════
# `dispatch()` 的 fallback 链判据是 `if result and result.raw_output` ——
# 而执行器撞预算收尾时**没有终答**，raw_output 就是空的（它就是"没写完"）。
# 于是收尾结果落进"空输出"分支 ⇒ 被当成"换个模型再试" ⇒ 换一个把剩下的时间
# 再烧一遍，`error_kind="deadline"` 和那份账（token/文件）**一起丢掉**。
#
# 真机实测（2026-09-13 02:42，`QIDIAN_EXEC_BUDGET=60`）：一次正常收尾
# **被吞成 3 轮重试、423 秒**，而收尾本身只用了 57.5 秒。

def test_deadline_wrapup_is_terminal_not_empty_output(monkeypatch):
    """收尾结果必须**原样返回**，不能被"空输出"那条判据吞掉。"""
    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult

    wrapped = ExecutorResult(
        success=False, raw_output="",              # ← 关键：收尾没有终答
        error="到达执行预算 60s，主动收尾", error_kind="deadline",
        token_count=1234, tool_events=[{"kind": "tool:start", "tool": "read_file"}])

    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": "m", "type": "openai-agent"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_run_executor", lambda *a, **k: wrapped)

    out = pd.dispatch("任务", "any", "tid", {})
    assert out.executor_result is wrapped, (
        "收尾结果被吞掉了 —— 它多半是被当成'空输出'换模型重试了（真机上烧了 423 秒）")
    assert out.executor_result.token_count == 1234, "账跟着丢了"


def test_empty_output_still_falls_through(monkeypatch):
    """**对照**：真正的空输出（不是 deadline）仍然要走"换模型"那条 —— 别把这次修复改宽了。"""
    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult

    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": "m", "type": "openai-agent"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_model_breaker",
                        type("B", (), {"record_failure": lambda *a: None,
                                       "record_success": lambda *a: None})())
    monkeypatch.setattr(pd, "_run_executor",
                        lambda *a, **k: ExecutorResult(success=False, raw_output="",
                                                       error="模型吐了个空", error_kind="exec"))
    with pytest.raises(RuntimeError):
        pd.dispatch("任务", "any", "tid", {})


# ═══════════════════════════════════════════════════════════════
# 流式调用必须有**总时长**上限（2026-09-15 真机坐实）
# ═══════════════════════════════════════════════════════════════
# 真机现场：一次 dispatch `elapsed = 1615.7` 秒（**27 分钟**）、`tokens = 0`
# —— usage 只在流结束时才到，说明**流压根没结束**。
#
# 根因：`httpx.Timeout(read=)` 封的是**两次读之间**的时间，**不是总时长** ——
# 服务端只要持续吐 token，一次调用就能跑任意久。而轮间那句
# `if time.time() >= self._deadline_at` **只在两轮之间**，单次调用里根本轮不到
# ⇒ 执行器全程"没看见表"，最后被外层 900s 的刀无声砍掉（`task_killed_no_wrapup`）。
# ⚠️ 这跟 09-13 那条「每 dispatch 归零」**是两个病**，修了一条不等于修了另一条。

class TestStreamTotalBudget:

    def _executor(self):
        from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
        ex = OpenAIAgentExecutor({"model": "m", "api_key_env": "K"},
                                 "测试任务", "t_stream_budget", cwd=".")
        ex._api_key = "k"
        ex._url = "http://x"
        ex._is_responses_api = False
        return ex

    def _endless_client(self, monkeypatch, stop_after_lines=300):
        """一个**永不结束**的流：一直吐合法 chunk，从不给 [DONE]。"""
        import httpx
        from singularity.scheduler.executors import openai_agent as oa

        class _Resp:
            status_code = 200
            text = ""
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass
            def iter_text(self):
                # ⚠️ **必须带延迟**：不加 sleep 的话 10 万行瞬间吐完，循环是
                # "生成器耗尽"退出的 —— **根本走不到总时长那条判据**，
                # 用例会**假绿**（我第一版就是这么写的，差点蒙过去）。
                import time as _t
                for i in range(stop_after_lines):
                    _t.sleep(0.01)
                    yield 'data: {"choices":[{"delta":{"content":"x"}}]}\n'

        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def stream(self, *a, **k): return _Resp()

        monkeypatch.setattr(oa, "_get_http_client", lambda: _C())

    def test_流跑过总时长就断开_不再无限跑(self, monkeypatch):
        """判据：跑到 `_call_deadline` 就必须断，而不是一直被流拖着。"""
        import time
        ex = self._executor()
        ex._deadline_at = time.time() + 1.0        # 剩余预算 1 秒 ⇒ _cap≈1s
        self._endless_client(monkeypatch)

        t0 = time.time()
        ex._stream_call({"model": "m", "messages": []})
        elapsed = time.time() - t0
        # 假流**不设上限时**要跑 ~3s（300 行 × 10ms），断得掉就该 ~1s（_cap）
        assert elapsed < 2.5, f"流跑了 {elapsed:.1f}s 还没断 —— 总时长上限没生效"

    def test_主动断开要出声(self, monkeypatch):
        """⚠️ **必须出声**：不说的话下游只看到"这轮输出特别短"，会去怀疑模型。"""
        import time
        ex = self._executor()
        ex._deadline_at = time.time() + 1.0
        self._endless_client(monkeypatch)

        seen = []
        from singularity.scheduler.executors import openai_agent as oa
        monkeypatch.setattr(oa.witness, "warn", lambda *a, **k: seen.append((a, k)))

        ex._stream_call({"model": "m", "messages": []})
        assert any("stream_over_budget" in str(a) for a, _ in seen), \
            f"断流没出声 —— 就查不到「输出为什么变短」：{seen}"

    def test_正常结束的流不受影响(self, monkeypatch):
        """对照组：正常 [DONE] 结束的流，照旧把内容拼回来（别把正常路径也断了）。"""
        import httpx
        from singularity.scheduler.executors import openai_agent as oa
        ex = self._executor()
        ex._deadline_at = 0.0                      # 没有任务级死线 → _cap = 240s

        class _Resp:
            status_code = 200
            text = ""
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): pass
            def iter_text(self):
                yield 'data: {"choices":[{"delta":{"content":"你好"}}]}\n'
                yield 'data: [DONE]\n'
        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def stream(self, *a, **k): return _Resp()
        monkeypatch.setattr(oa, "_get_http_client", lambda: _C())

        out = ex._stream_call({"model": "m", "messages": []})
        assert out["choices"][0]["message"]["content"] == "你好"

    def test_吐字节但凑不满一行_也要断得掉(self, monkeypatch):
        """**这条钉的就是 2026-09-16 真机两轮卡死那个形状。**

        服务端一直在发字节、却**永远不换行**：
          · `read=` 超时会被"又有字节进来了"**重置** ⇒ 兜不住；
          · `iter_lines()` **一行都吐不出来** ⇒ 写在循环体里的判据**永远轮不到**。
        ⇒ 两条保护**同时**失效。真机上 planning 阶段各卡死 30+ 分钟，
        `stream_over_budget` 全库 **0 次**，线程栈停在
        `_ssl__SSLSocket_read → PySSL_select → poll`。

        ⚠️ **必须用真 HTTP 服务器**：手搭的替身只会把我猜的"`iter_lines()` 会一直
        缓冲"再喂回给我 —— 那测的是我的假设，不是 httpx 的真实行为。
        （顺带：这个形状本身在 `iter_lines` 里也是**对的**，能凑满行时它就好使。）

        变异验证：把 `_stream_call` 里的 `_lines()` 换回 `resp.iter_lines()` → 红
        （会一直等到服务器收工才回来）。
        """
        import http.server
        import threading
        import time

        import httpx
        from singularity.scheduler.executors import openai_agent as oa

        TRICKLE_S = 6.0          # 服务器吐多久 —— 超过它就是"没断掉"

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                t_end = time.time() + TRICKLE_S
                try:
                    while time.time() < t_end:
                        self.wfile.write(b":")       # ← 有字节，但永远不换行
                        self.wfile.flush()
                        time.sleep(0.1)
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        ex = self._executor()
        ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
        ex._deadline_at = time.time() + 1.0          # 剩余 1 秒 ⇒ _cap≈1s
        monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())

        seen = []
        monkeypatch.setattr(oa.witness, "warn", lambda *a, **k: seen.append((a, k)))

        try:
            t0 = time.time()
            ex._stream_call({"model": "m", "messages": []})
            elapsed = time.time() - t0
        finally:
            srv.shutdown()
            srv.server_close()

        assert elapsed < 3.0, (
            f"流跑了 {elapsed:.1f}s 还没断（服务器一共才吐 {TRICKLE_S}s）—— "
            f"判据又挂在「等一整行」上了")
        assert any("stream_over_budget" in str(a) for a, _ in seen), \
            f"断流没出声 —— 就查不到「输出为什么变短」：{seen}"
