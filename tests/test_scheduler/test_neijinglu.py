"""neijinglu.py 单元测试 — _is_redundant_failure + format_report 纯逻辑。

ponytail: 不测 build_report (LLM调用) 和 save_trace (文件IO)。
"""

import json
import pytest


class TestFormatReport:
    def test_basic_format(self):
        from singularity.scheduler.neijinglu import format_report, DeliveryReport
        from singularity.scheduler.router import RouteResult
        from singularity.scheduler.executors.base import ExecutorResult
        from singularity.scheduler.validator import ValidationReport
        from singularity.scheduler.snapshot import Snapshot
        r = DeliveryReport(
            task="abc123: 修复登录",
            route=RouteResult(gate_required=False, task_type="bugfix"),
            executor_result=ExecutorResult(success=False, raw_output="错误输出"),
            validation=ValidationReport(verdict="阻断", action="abort",
                unverified=["测试不通过"]),
            snapshot=Snapshot(id="s1", method="git", ref="abc", created_at=0.0),
            final_status="blocked",
        )
        s = format_report(r)
        assert "abc123" in s
        assert "登录" in s
        assert "阻断" in s

    def test_pass_report(self):
        from singularity.scheduler.neijinglu import format_report, DeliveryReport
        from singularity.scheduler.router import RouteResult
        from singularity.scheduler.executors.base import ExecutorResult
        from singularity.scheduler.validator import ValidationReport
        from singularity.scheduler.snapshot import Snapshot
        r = DeliveryReport(
            task="t1: 添加功能",
            route=RouteResult(gate_required=False, task_type="feature"),
            executor_result=ExecutorResult(success=True, raw_output="完成"),
            validation=ValidationReport(verdict="通过", action="pass"),
            snapshot=Snapshot(id="s1", method="git", ref="abc", created_at=0.0),
            final_status="delivered",
        )
        s = format_report(r)
        assert "t1" in s or "pass" in s.lower()


class TestIsRedundantFailure:
    def _make_data(self, **kw):
        d = {
            "route": {"task_type": "bugfix"},
            "validation": {"verdict": "阻断", "action": "abort"},
        }
        d.update(kw)
        return d

    def test_pass_action_no_redundant(self, monkeypatch, tmp_path):
        """pass → 不判重。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config
        data = self._make_data()
        data["validation"] = {"verdict": "通过", "action": "pass"}

        _write_trace_files(tmp_path, [self._make_data() for _ in range(5)])
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert not _is_redundant_failure(data)

    def test_pass_verdict_no_redundant(self, monkeypatch, tmp_path):
        """verdict=pass → 不判重。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config
        data = self._make_data()
        data["validation"]["verdict"] = "pass"

        _write_trace_files(tmp_path, [self._make_data() for _ in range(5)])
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert not _is_redundant_failure(data)

    def test_no_trace_dir(self, monkeypatch, tmp_path):
        """trace 目录不存在 → False。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config
        p = tmp_path / "nonexistent"
        monkeypatch.setattr(config, "TRACE_DIR", p)
        assert not _is_redundant_failure(self._make_data())

    def test_not_redundant_single_failure(self, monkeypatch, tmp_path):
        """只有1条失败 → 不冗余。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config

        _write_trace_files(tmp_path, [self._make_data()])
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert not _is_redundant_failure(self._make_data())

    def test_redundant_three_same_failures(self, monkeypatch, tmp_path):
        """3 条相同失败 → 冗余。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config

        traces = [self._make_data() for _ in range(3)]
        _write_trace_files(tmp_path, traces)
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert _is_redundant_failure(self._make_data())

    def test_different_task_type_not_redundant(self, monkeypatch, tmp_path):
        """不同 task_type → 不冗余。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config

        traces = [
            {**self._make_data(), "route": {"task_type": "feature"}},
            {**self._make_data(), "route": {"task_type": "feature"}},
            {**self._make_data(), "route": {"task_type": "feature"}},
        ]
        _write_trace_files(tmp_path, traces)
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert not _is_redundant_failure(self._make_data())

    def test_corrupt_json_skipped(self, monkeypatch, tmp_path):
        """损坏的 JSON 文件被跳过，不影响判定。"""
        from singularity.scheduler.neijinglu import _is_redundant_failure
        from singularity.scheduler import config

        (tmp_path / "corrupt.json").write_text("not json")
        _write_trace_files(tmp_path, [self._make_data() for _ in range(3)])
        monkeypatch.setattr(config, "TRACE_DIR", tmp_path)
        assert _is_redundant_failure(self._make_data())


def _write_trace_files(d, traces):
    """向目录写入 trace JSON 文件。"""
    import time
    for i, t in enumerate(traces):
        p = d / f"trace_{i}_{int(time.time()*1000)}.json"
        p.write_text(json.dumps(t, ensure_ascii=False))
