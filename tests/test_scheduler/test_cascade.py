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

    def test_retry_high_confidence_needs_failure_kind_ok(self):
        """分数够高但 failure_kind 不是 ok → **不许** cascade_accept。

        光看 `conf >= 0.75` 会被"形状"骗过去：`post_execution_hook` 的基线是 0.5，
        长输出 +0.1、含 "passed" +0.15 = **正好 0.75**。审查层扣的那点分（软警告 -0.1）
        能被这些无关的加分项抵回来，于是"这一版有问题"照样被接受合并，
        而那轮承诺的软修（soft_quality）永远轮不到（2026-09-19 外派评审核出）。
        """
        task = self._make_task()
        validation = val_mod.ValidationReport(
            verdict="需改进", action="retry", confidence=0.85,
            evidence={"issues": ["soft"]}, unverified=[]
        )
        # 对照：同一个分数，failure_kind=ok 时才接受
        action, result = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(), {"warnings": [], "failure_kind": "ok", "confidence": 0.85}
        )
        assert action == "return" and result.ok is True

        action, _ = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(),
            {"warnings": ["软伤"], "failure_kind": "soft_quality", "confidence": 0.85}
        )
        assert action == "continue", "failure_kind 非 ok 却走了 cascade_accept —— 软修被跳过"

    # ── 测试失败时，**失败输出必须进反馈**（2026-09-28）────────────────────
    #
    # 🔴 来历（`round-20260928j` 真机）：T1 连试 3 次，三次判词一模一样，最后失败。
    # 查下来它的重试反馈只有三样：`evidence`（validate.py 对产出的判词）·
    # `质量警告: tests failed (pytest): tests failed: 退出码 1（失败个数没能从输出里数出来）`·
    # `失败类型: test_failure` —— **没有测试名、没有文件、没有断言、没有输出**，
    # 而**要改的东西全在那里**。⇒ 三个回合全在盲改，一个任务烧 435,832 token。
    # ⚠️ **完整输出本来就躺在 `quality["test_result"]` 里**（`_review.py` 那句
    # `quality["test_result"] = test_result  # 供 supervisor._check_artifact 复用`），
    # 只是**没有人把它送去给要改代码的那个人** —— 和本仓 09-28 那两条
    # （`data_model` / `estimated_files`）**逐字同形：不是没声明，是没送到。**

    真机输出 = (
        ".......F......................                                           [100%]\n"
        "=================================== FAILURES ===================================\n"
        "________________ test_common_options_may_precede_the_subcommand ________________\n"
        "tests/test_cli_parse_args.py:145: in test_common_options_may_precede_the_subcommand\n"
        "    assert args.format == \"tsv\"\n"
        "E   AssertionError: assert 'json' == 'tsv'\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_cli_parse_args.py::test_common_options_may_precede_the_subcommand\n"
        "1 failed, 29 passed in 0.73s\n")

    def _retry_with_test_result(self, tr):
        """跑一次 retry 分支，返回喂给模型的反馈文本。"""
        task = self._make_task()
        validation = val_mod.ValidationReport(
            verdict="需改进", action="retry", confidence=0.5,
            evidence={"issues": ["x"]}, unverified=[])
        action, feedback = _decide_cascade(
            task, "any", 1, validation, self._make_disp(), [], None,
            ["E_model1"], set(),
            {"warnings": ["tests failed (pytest): tests failed: 退出码 1（失败个数没能从输出里数出来）"],
             "failure_kind": "test_failure", "confidence": 0.2,
             "test_result": tr})
        assert action == "continue", "这一支应当是重试"
        return feedback

    def test_测试失败时反馈里要有运行输出(self):
        """判据：**哪条、为什么**都得在反馈里 —— 那正是模型改代码要照的东西。"""
        fb = self._retry_with_test_result(
            {"passed": False, "runner": "pytest", "exit_code": 1, "failures": 1,
             "output": self.真机输出})
        assert "test_common_options_may_precede_the_subcommand" in fb, \
            f"反馈里没有失败的测试名 —— 模型只能盲改：{fb[:300]}"
        assert "assert 'json' == 'tsv'" in fb, "反馈里没有断言 —— 说不出为什么失败"

        # 🔴 **非要一段"比上限长"的输入不可**（2026-09-28 变异复核当场抓到）：
        # 上面那份真机输出才 500 来字符，而反馈那一段的上限是 1500
        # ⇒ `out[:1500]` 返回的就是**整串**，**取头和取尾结果一模一样**
        # ⇒ 把实现改成 `output[:1500]`，上面两条断言**照样绿**。
        # ⚠️ **同一个坑这一晚踩了两次**（`test_validator` 那条"取尾巴"也是这个病）
        # —— 判据是：**凡是断言"取的是尾"，输入就必须比阈值长**，
        # 否则它测的是"这段被留下了"，不是"留下的是尾"。
        # ⚠️ 哨兵必须放在**开头**、且唯一 —— 一开始我断言的是
        # `"noise line" not in fb2`，**那是错的**：掐头留尾本来就会带上一截尾巴上的噪声。
        # 能判别"取的是头还是尾"的，只有"**开头那个东西在不在**"。
        long_out = "头部的哨兵_取尾就该被切掉\n" + ("noise line\n" * 400) + self.真机输出
        fb2 = self._retry_with_test_result(
            {"passed": False, "runner": "pytest", "exit_code": 1, "failures": 1,
             "output": long_out})
        assert "assert 'json' == 'tsv'" in fb2, "尾巴被丢了 = 取的是头"
        assert "头部的哨兵_取尾就该被切掉" not in fb2, "头部的哨兵还在 = 取的是头不是尾"

    def test_测试通过时不许往反馈里塞输出(self):
        """反方向：**没失败就别塞**。

        否则每一轮正常重试都会拖着一段几千字符的运行输出（那是真金白银的 context）。
        同族判据：`test_架构没给_context_时不许硬塞一行假的` —— 有就说、没有就别编。
        """
        fb = self._retry_with_test_result(
            {"passed": True, "runner": "pytest", "output": self.真机输出})
        assert "测试失败输出" not in fb, "测试是过的，却把输出塞进了反馈"
        assert "assert 'json' == 'tsv'" not in fb

    def test_没有test_result时不许炸(self):
        """`quality` 里没有 test_result（没跑到测试那一步）时，反馈照旧要给出来。"""
        fb = self._retry_with_test_result(None)
        assert "失败类型: test_failure" in fb, "反馈整段没了 —— 比少一段更坏"

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
