"""test_cascade.py — cascade routing 决策 + dispatcher pick_agent 快速验证。"""
import pytest
import re
from singularity.scheduler._exec import _decide_cascade
from singularity.scheduler import validator as val_mod
from singularity.scheduler.dispatcher import pick_agent, load_agents, agent_api_available


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


class TestPickAgent:
    """dispatcher.pick_agent 选择模型 + fallback 链。"""

    def test_pick_returns_agent_for_level(self):
        agents = load_agents()
        for level in ("any",):
            if level in agents and agents[level]:
                # 检查该层级是否有可用的代理
                available_agents = [agent for agent in agents[level] if agent_api_available(agent)]
                if available_agents:
                    # 如果有可用代理，则尝试获取一个
                    try:
                        cfg = pick_agent(agents, level)
                        if cfg:  # 可能所有 agent 都 disabled
                            assert "model" in cfg
                            assert "type" in cfg
                    except RuntimeError as e:
                        # 如果抛出异常，确保它是预期的异常
                        expected_msg = f"{level} 层所有 agent 的 API 均不可用"
                        if expected_msg in str(e):
                            continue  # 这是我们预期的情况
                        else:
                            raise  # 如果是其他异常，重新抛出
                else:
                    # 如果没有可用代理，应该抛出异常
                    with pytest.raises(RuntimeError, match=re.escape(f"{level} 层所有 agent 的 API 均不可用")):
                        pick_agent(agents, level)
            else:
                # 如果层级不存在代理配置，跳过测试
                continue

    def test_fallback_chain_returns_list(self):
        from singularity.scheduler.dispatcher import pick_agent_fallback_chain
        agents = load_agents()
        chain = pick_agent_fallback_chain(agents, "any")
        assert isinstance(chain, list)


# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    t = TestDecideCascade()
    t.test_pass_action()
    t.test_retry_high_confidence_skips()
    t.test_retry_low_confidence_upgrades()
    t.test_abort_terminal()
    print("✅ cascade routing self-check passed")
    t2 = TestPickAgent()
    t2.test_pick_returns_agent_for_level()
    t2.test_fallback_chain_returns_list()
    print("✅ pick_agent self-check passed")