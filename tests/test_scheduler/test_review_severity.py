"""审查裁决里的 severity 归一化。

背景：`_review.py` 拿 **LLM 返回**的 severity 跟字面量精确比（`== "critical"`）。
判官是模型，输出会在大小写/空格上飘 —— 一个字母的差别就让 critical 匹配不上、
被当成软信号**放行**。这正是这个仓库修过一轮的 fail-open（"审查发现问题但不拦"）。
"""
from pathlib import Path

from singularity.scheduler import _review


class TestSev:
    def test_normalizes_case_and_space(self):
        assert _review._sev({"severity": "Critical"}) == "critical"
        assert _review._sev({"severity": "  HIGH "}) == "high"
        assert _review._sev({"severity": "WARNING"}) == "warning"

    def test_missing_or_odd_is_empty(self):
        """缺失/非字符串归一成空串 —— 空串不匹配任何硬拦档，等价于"没给 severity"。"""
        assert _review._sev({}) == ""
        assert _review._sev(None) == ""
        assert _review._sev({"severity": None}) == ""
        assert _review._sev({"severity": 3}) == "3"

    def test_hard_block_matches_across_casing(self):
        """硬拦判定必须跨大小写生效 —— 这就是 fail-open 的入口。"""
        findings = [{"severity": "Critical", "description": "越权读取他人订单"},
                    {"severity": "medium", "description": "日志缺少脱敏"}]
        hard = [f for f in findings if _review._sev(f) in ("critical", "high")]
        assert len(hard) == 1 and hard[0]["description"] == "越权读取他人订单"


class TestSecurityGateUnchangedByCasing:
    """安全审计的硬拦判定必须跨大小写生效 —— 端到端，不只是 _sev 单测。

    原先拿 LLM 返回的 severity 跟字面量精确比，模型吐 "Critical" 就匹配不上 →
    归入软信号 → 不拦，真漏洞随代码合并。这里走真实的 run_post_exec_checks。
    """

    def _run(self, monkeypatch, tmp_path, findings):
        from types import SimpleNamespace
        from singularity.scheduler import _review as rv, validator as val, dispatcher as disp
        monkeypatch.setattr(rv.witness, "warn", lambda *a, **k: None)
        monkeypatch.setattr(val, "security_review", lambda *a, **k: {"issues": []})
        monkeypatch.setattr(val, "run_project_tests",
                            lambda *a, **k: {"passed": True, "runner": "pytest", "total": 1})
        monkeypatch.setattr(val, "security_audit_review",
                            lambda *a, **k: {"verdict": "needs_fix", "findings": findings})
        monkeypatch.setattr(disp, "load_agents", lambda: {})
        monkeypatch.setattr(disp, "_all_agents_list", lambda *_a: [])   # 空池 → 跳过多人审查
        # 空池会落到单模型 crossover_review，而它是 fail-closed 的（拿不到 reviewer
        # 就造一条 critical → retry）。要隔离安全门禁，这里必须给它一个干净结果。
        monkeypatch.setattr(val, "crossover_review",
                            lambda *a, **k: {"issues": [], "verdict": "pass", "summary": ""})
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        validation = SimpleNamespace(action="pass", unverified=[])
        quality = {"warnings": [], "confidence": 0.5, "quality_signals": {}}
        rv.run_post_exec_checks(
            validation=validation, quality=quality,
            exec_result=SimpleNamespace(raw_output=""),
            task=SimpleNamespace(project_id="", description="d"),
            agent_cfg={"model": "m1"}, level="any", cwd=str(tmp_path),
            changed=["a.py", "b.py"])
        return validation, quality

    def test_capitalized_critical_still_blocks(self, monkeypatch, tmp_path):
        v, q = self._run(monkeypatch, tmp_path,
                         [{"severity": "Critical", "description": "越权读取他人订单"}])
        assert v.action == "retry", f"大写 Critical 没拦住 → fail-open: {q['warnings']}"

    def test_padded_high_still_blocks(self, monkeypatch, tmp_path):
        v, _ = self._run(monkeypatch, tmp_path,
                         [{"severity": " HIGH ", "description": "SQL 注入"}])
        assert v.action == "retry"

    def test_medium_stays_soft(self, monkeypatch, tmp_path):
        """medium/low = 加固建议/设计不完整，不硬拦 —— 既有阈值语义别被误伤。"""
        v, q = self._run(monkeypatch, tmp_path,
                         [{"severity": "medium", "description": "日志缺脱敏"}])
        assert v.action == "pass"
        assert q["quality_signals"].get("security_soft") == 1


def test_review_never_compares_raw_llm_enums():
    """守着不变量：LLM 来的 severity / status 一律过 _norm 再比。

    这条防的是回归 —— 以后新增审查分支时又写回 `i.get("severity") == "critical"`
    或 `v.get("status") in ("fail",)`，在那个大小写下就是静默放行，测试全绿也看不出来。
    """
    src = Path(_review.__file__).read_text(encoding="utf-8")
    for raw in ('get("severity") ==', 'get("severity") in (',
                'get("status") ==', 'get("status") in ('):
        assert raw not in src, f"又出现了裸的 LLM 枚举精确比较（{raw}）—— 走 _norm() 归一化"


class TestQaStatusUnchangedByCasing:
    """QA 约束验收的 status 同样来自 LLM，同样会飘。"""

    def test_capitalized_fail_is_counted(self):
        """'Fail' 必须也算未满足 —— 不然约束没达标却放行。"""
        v = {"status": "Fail"}
        assert _review._norm(v.get("status")) in ("fail", "warning")

    def test_norm_handles_none(self):
        assert _review._norm(None) == ""
        assert _review._norm(" PASS ") == "pass"
