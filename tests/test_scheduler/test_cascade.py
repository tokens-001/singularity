"""test_cascade.py — cascade routing 决策 + dispatcher 选模型快速验证。"""
import pytest
import re
from singularity.scheduler._exec import _decide_cascade
from singularity.scheduler import validator as val_mod
from singularity.scheduler.dispatcher import pick_agent_fallback_chain, load_agents, agent_api_available


class TestDecideCascade:
    """_decide_cascade 是 cascade routing 的核心决策函数。"""

    def _make_task(self):
        from singularity.scheduler.tracker import create
        return create("test cascade task")

    def _make_disp(self):
        class D: pass
        d = D(); d.agent_cfg = {"model": "test-model"}; d.executor_result = None
        return d

    def test_pass_action(self):
        task = self._make_task()
        validation = val_mod.ValidationReport(verdict="通过", action="pass", unverified=[])
        action, result = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(), {"warnings": [], "failure_kind": "ok", "confidence": 0.0}
        )
        assert action == "return"
        assert result.ok is True

    def test_retry_high_confidence_skips(self):
        task = self._make_task()
        validation = val_mod.ValidationReport(
            verdict="需改进", action="retry", confidence=0.85,
            evidence={"issues": ["minor"]}, unverified=[]
        )
        action, result = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(), {"warnings": [], "failure_kind": "ok", "confidence": 0.0}
        )
        # 高置信 retry → 接受当前结果，不浪费重试
        assert action == "return"
        assert result.ok is True

    def test_retry_low_confidence_upgrades(self):
        task = self._make_task()
        validation = val_mod.ValidationReport(
            verdict="不行", action="retry", confidence=0.2,
            evidence={"issues": ["serious"]}, unverified=[]
        )
        action, _ = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1", "E+_model2"], set(), {"warnings": [], "failure_kind": "ok", "confidence": 0.0}
        )
        # 低置信 + 有 fallback → 立即升级
        assert action == "break"

    def test_abort_terminal(self):
        task = self._make_task()
        validation = val_mod.ValidationReport(verdict="阻断", action="abort", unverified=["fatal"])
        action, result = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(), {"warnings": [], "failure_kind": "ok", "confidence": 0.0}
        )
        assert action == "return"
        assert result.ok is False

    def test_soft_quality_hard_gate(self):
        """软质量触顶(turn>=2) → 硬门槛: ok=False 不静默放行。"""
        task = self._make_task()
        validation = val_mod.ValidationReport(
            verdict="需改进", action="retry", confidence=0.5,
            evidence={"issues": ["soft"]}, unverified=[])
        action, result = _decide_cascade(
            task, "any", 2, validation, self._make_disp(), [], None,
            ["E_model1"], set(), {"warnings": ["软伤"], "failure_kind": "soft_quality", "confidence": 0.5}
        )
        assert action == "return"
        assert result.ok is False
        assert "soft_quality_gate" in result.term_reason


class TestPickAgent:
    """dispatcher 选模型 + fallback 链。（原 pick_agent 是零调用的死函数，已删；
    实际在用的一直是 pick_agent_fallback_chain。）"""

    def test_可用_agent_必须进链(self, monkeypatch):
        """**无条件断言**：有可用 agent 就必须给出非空链。

        ⚠️ 原来这两条的断言全埋在 `if available_agents:` / `if cfg:` / `try:` 里 ——
        而「**有可用 agent 却返回空链**」（最该防的那条回归）走的是 `cfg = None` 那条路，
        **零断言也能绿**（2026-09-14，外派⑤核出、我核过）。
        也顺手改成**不依赖 `load_agents()` 的真实配置**（同文件 `test_breaker_*` 的写法）——
        原来 `if level not in agents: continue` 会让它在没配 agent 的机器上**什么都不测**。
        """
        from singularity.scheduler.dispatcher import pick_agent_fallback_chain

        monkeypatch.setattr(
            "singularity.scheduler.dispatcher.agent_api_available", lambda a: True)
        agents = {"any": [{"model": "__m_x__", "type": "claude-cli", "entry": "x"}]}

        chain = pick_agent_fallback_chain(agents, "any")

        assert chain, "有可用 agent 却返回空链 —— 这正是要防的回归"
        assert chain[0]["model"] == "__m_x__"
        assert "model" in chain[0] and "type" in chain[0]

    def test_没有可用_agent_返回空链(self, monkeypatch):
        """对照：一个可用 agent 都没有时必须是空链（别把上面的修法做成"永远非空"）。"""
        from singularity.scheduler import _model_breaker as mb
        from singularity.scheduler.dispatcher import pick_agent_fallback_chain

        monkeypatch.setattr(
            "singularity.scheduler.dispatcher.agent_api_available", lambda a: False)
        monkeypatch.setattr(mb, "_breakers", {})
        monkeypatch.setattr(mb, "_loaded", True)     # 别读真实 .qidian
        agents = {"any": [{"model": "__m_x__", "type": "claude-cli", "entry": "x"}]}

        assert pick_agent_fallback_chain(agents, "any") == []

    def test_breaker_filters_open_model_but_fails_open(self, monkeypatch, tmp_path):
        """熔断中的模型从链里剔除；全池熔断时 fail-open 原样返回，防调度停摆。"""
        from singularity.scheduler import _model_breaker as mb
        from singularity.scheduler.dispatcher import pick_agent_fallback_chain

        monkeypatch.setattr(mb, "_path", lambda: tmp_path / "breakers.json")
        monkeypatch.setattr(mb, "_breakers", {})
        monkeypatch.setattr(mb, "_loaded", True)   # 别读真实 .qidian
        monkeypatch.setattr("singularity.scheduler.dispatcher.agent_api_available", lambda a: True)

        agents = {"any": [{"model": "__m_a__", "type": "claude-cli", "entry": "x"},
                          {"model": "__m_b__", "type": "claude-cli", "entry": "x"}]}
        assert len(pick_agent_fallback_chain(agents, "any")) == 2

        for _ in range(mb.MAX_FAILURES):
            mb.record_failure("__m_a__")
        assert [a["model"] for a in pick_agent_fallback_chain(agents, "any")] == ["__m_b__"]

        for _ in range(mb.MAX_FAILURES):
            mb.record_failure("__m_b__")
        assert len(pick_agent_fallback_chain(agents, "any")) == 2, "全熔断应 fail-open"

        mb.record_success("__m_a__")
        assert mb.is_available("__m_a__"), "成功后应立刻恢复"


# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    t = TestDecideCascade()
    t.test_pass_action()
    t.test_retry_high_confidence_skips()
    t.test_retry_low_confidence_upgrades()
    t.test_abort_terminal()
    print("✅ cascade routing self-check passed")
    # ⚠️ 这里原来还调 `t2.test_pick_returns_agent_for_level()` /
    # `t2.test_fallback_chain_returns_list()` —— **这两个方法从来不存在**（现名是
    # `test_可用_agent_必须进链` / `test_没有可用_agent_返回空链`），脚本方式跑必当场
    # AttributeError；而 pytest 不走 `__main__`，所以它**早就悄悄坏了、没人发现**
    # （2026-09-14 审计读到即坐实）。方法现在还要吃 `monkeypatch` 夹具、脚本里给不了，
    # 索性去掉这一段——那两条用例交给 pytest 跑。
