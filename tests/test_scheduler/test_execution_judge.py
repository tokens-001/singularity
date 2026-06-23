"""execution_judge.py 单元测试 — _pre_check / _parse_verdict / should_retry 纯函数。

ponytail: 不测 LLM 调用路径（_llm_judge, _call_e_layer, fuse_outputs）。
"""

import pytest
import json


# ═══════════════════════════════════════════════════════════════
# _pre_check — 低成本预检
# ═══════════════════════════════════════════════════════════════

class TestPreCheck:
    def test_empty_output(self):
        """空字符串 → fail。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check("")
        assert r is not None
        assert not r.pass_
        assert r.failure_mode == "empty_output"

    def test_whitespace_only(self):
        """只有空白 → fail。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check("   \n  \t  ")
        assert r is not None
        assert r.failure_mode == "empty_output"

    def test_tool_loop_detected(self):
        """3+ tool-loop 标记 → fail。"""
        from singularity.scheduler.execution_judge import _pre_check
        output = '{"tool_calls": [...]} I need to read the file Let me search'
        r = _pre_check(output)
        assert r is not None
        assert r.failure_mode == "tool_loop"

    def test_tool_loop_below_threshold(self):
        """2 个标记 → 不触发（阈值为3）。"""
        from singularity.scheduler.execution_judge import _pre_check
        output = 'I need to read the file. Let me check the file.'
        r = _pre_check(output)
        assert r is None

    def test_one_tool_marker_passes(self):
        """单个标记 → 不触发。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check('Let me search this codebase')
        assert r is None

    def test_valid_json_output(self):
        """合法 JSON 输出 → None（通过预检）。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check('{"key": "value"}')
        assert r is None

    def test_invalid_json(self):
        """不合法 JSON → fail。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check('{"key": "value"')
        assert r is not None
        assert r.failure_mode == "json_error"

    def test_array_json_valid(self):
        """合法 JSON 数组 → None。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check('[1, 2, 3]')
        assert r is None

    def test_non_json_normal_output(self):
        """普通文本输出 → None。"""
        from singularity.scheduler.execution_judge import _pre_check
        r = _pre_check("这是正常的代码输出内容")
        assert r is None


# ═══════════════════════════════════════════════════════════════
# _parse_verdict — 解析 LLM 返回
# ═══════════════════════════════════════════════════════════════

class TestParseVerdict:
    def test_empty_raw(self):
        """空字符串 → fail + uncertain。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        v = _parse_verdict("")
        assert not v.pass_
        assert v.uncertain
        assert v.score == 0.0

    def test_none_raw(self):
        """None → fail。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        v = _parse_verdict(None)
        assert not v.pass_
        assert v.uncertain

    def test_valid_verdict_pass(self):
        """合法 pass verdict。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        raw = '{"pass": true, "score": 0.9, "reason": "完成得很好", "failure_mode": "ok", "uncertain": false}'
        v = _parse_verdict(raw)
        assert v.pass_
        assert v.score == 0.9
        assert v.reason == "完成得很好"
        assert v.failure_mode == "ok"
        assert not v.uncertain

    def test_valid_verdict_fail(self):
        """合法 fail verdict。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        raw = '{"pass": false, "score": 0.2, "reason": "未完成需求", "failure_mode": "semantic_error", "uncertain": true}'
        v = _parse_verdict(raw)
        assert not v.pass_
        assert v.score == 0.2
        assert v.failure_mode == "semantic_error"
        assert v.uncertain

    def test_malformed_json(self):
        """非法 JSON → fail + uncertain。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        v = _parse_verdict("这不是 JSON")
        assert not v.pass_
        assert v.uncertain

    def test_json_with_code_fence(self):
        """在 ```json ... ``` 中的 JSON → 应正确解析。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        raw = '```json\n{"pass": true, "score": 0.8, "reason": "通过", "failure_mode": "ok"}\n```'
        v = _parse_verdict(raw)
        assert v.pass_

    def test_missing_fields_default(self):
        """缺少字段 → 使用默认值。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        v = _parse_verdict('{"pass": true}')
        assert v.pass_
        assert v.score == 0.5  # 默认
        assert v.reason == ""  # 默认

    def test_json_with_noise(self):
        """LLM 在 JSON 前后加了废话 → try_parse_json 应提取。"""
        from singularity.scheduler.execution_judge import _parse_verdict
        raw = '分析如下：{"pass": false, "score": 0.1, "reason": "不行", "failure_mode": "context_insufficient"}以上是判断结果。'
        v = _parse_verdict(raw)
        assert not v.pass_
        assert v.failure_mode == "context_insufficient"


# ═══════════════════════════════════════════════════════════════
# should_retry — 纯决策
# ═══════════════════════════════════════════════════════════════

class TestShouldRetry:
    def test_pass_no_retry(self):
        """通过 → 不重试。"""
        from singularity.scheduler.execution_judge import should_retry, JudgeVerdict
        v = JudgeVerdict(pass_=True, score=0.9, reason="很好")
        assert not should_retry(v, 0)

    def test_retry_count_exceeded(self):
        """重试次数超限 → 不重试。"""
        from singularity.scheduler.execution_judge import should_retry, JudgeVerdict
        v = JudgeVerdict(pass_=False, score=0.3, reason="失败")
        assert not should_retry(v, 3, max_retries=3)

    def test_context_insufficient_no_retry(self):
        """上下文不足 → 不重试（重试无意义）。"""
        from singularity.scheduler.execution_judge import should_retry, JudgeVerdict
        v = JudgeVerdict(pass_=False, score=0.2, reason="缺少信息",
                         failure_mode="context_insufficient")
        assert not should_retry(v, 0)

    def test_normal_failure_should_retry(self):
        """普通失败 → 应重试。"""
        from singularity.scheduler.execution_judge import should_retry, JudgeVerdict
        v = JudgeVerdict(pass_=False, score=0.3, reason="失败",
                         failure_mode="semantic_error")
        assert should_retry(v, 0)

    def test_unknown_failure_mode_should_retry(self):
        """未知失败模式 → 应重试。"""
        from singularity.scheduler.execution_judge import should_retry, JudgeVerdict
        v = JudgeVerdict(pass_=False, score=0.1, reason="未知",
                         failure_mode="unknown")
        assert should_retry(v, 1)


# ═══════════════════════════════════════════════════════════════
# build_reflexion_feedback
# ═══════════════════════════════════════════════════════════════

class TestBuildReflexionFeedback:
    def test_contains_reason_and_mode(self):
        """反馈包含原因和失败模式。"""
        from singularity.scheduler.execution_judge import build_reflexion_feedback, JudgeVerdict
        v = JudgeVerdict(pass_=False, score=0.2, reason="输出不完整",
                         failure_mode="semantic_error")
        fb = build_reflexion_feedback(v)
        assert "上一轮结果不合格" in fb
        assert "输出不完整" in fb
        assert "semantic_error" in fb
        assert "请修正后重新输出" in fb
