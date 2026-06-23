"""witness.py 单元测试 — _fmt_duration / _fmt_avg 纯格式化函数。"""

import pytest


class TestFmtDuration:
    def test_seconds(self):
        from singularity.scheduler.witness import _fmt_duration
        assert _fmt_duration(30) == "30s"

    def test_minutes(self):
        from singularity.scheduler.witness import _fmt_duration
        assert _fmt_duration(120) == "2.0min"

    def test_hours(self):
        from singularity.scheduler.witness import _fmt_duration
        assert _fmt_duration(7200) == "2.00h"

    def test_boundary_60s(self):
        from singularity.scheduler.witness import _fmt_duration
        assert "min" in _fmt_duration(60)

    def test_boundary_3600s(self):
        from singularity.scheduler.witness import _fmt_duration
        assert "h" in _fmt_duration(3600)

    def test_zero(self):
        from singularity.scheduler.witness import _fmt_duration
        assert _fmt_duration(0) == "0s"


class TestFmtAvg:
    def test_nonempty(self):
        from singularity.scheduler.witness import _fmt_avg
        avg = _fmt_avg([100, 200, 300])
        assert "200s" in avg or "min" in avg  # depends on values

    def test_empty(self):
        from singularity.scheduler.witness import _fmt_avg
        assert _fmt_avg([]) == "--"
