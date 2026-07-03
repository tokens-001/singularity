"""route_learner.py 单元测试 — LearnerStats.record() EWMA + RouteLearner 排序。"""

import pytest


class TestLearnerStats:
    def test_record_success_updates_ewma(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        s.record(success=True, elapsed_ms=100, tokens=50)
        assert s.success_count == 1
        assert s.failure_count == 0
        assert s.sample_count == 1
        # EWMA: 0.2*1 + 0.8*0.5 = 0.6
        assert abs(s.ewma_success_rate - 0.6) < 0.001

    def test_record_failure_drops_ewma(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        s.record(success=False)
        assert s.success_count == 0
        assert s.failure_count == 1
        # EWMA: 0.2*0 + 0.8*0.5 = 0.4
        assert abs(s.ewma_success_rate - 0.4) < 0.001

    def test_hedge_weight_success_up(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        s.record(success=True)
        assert s.hedge_weight > 1.0  # 1.0 * 1.1 = 1.1

    def test_hedge_weight_failure_down(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        s.record(success=False)
        assert s.hedge_weight < 1.0  # 1.0 * 0.9 = 0.9

    def test_hedge_weight_clamped_max(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        for _ in range(30):
            s.record(success=True)
        assert s.hedge_weight <= 10.0

    def test_hedge_weight_clamped_min(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        for _ in range(30):
            s.record(success=False)
        assert s.hedge_weight >= 0.1

    def test_multiple_records_accumulate(self):
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        for i in range(5):
            s.record(success=i % 2 == 0, elapsed_ms=100, tokens=50)
        assert s.sample_count == 5
        assert s.total_elapsed_ms == 500
        assert s.total_tokens == 250

    def test_ewma_converges(self):
        """连续成功 → EWMA 趋近 1。"""
        from singularity.scheduler.route_learner import LearnerStats
        s = LearnerStats(task_type="bugfix", model="claude", level="any")
        for _ in range(20):
            s.record(success=True)
        assert s.ewma_success_rate > 0.95


class TestRouteLearner:
    def test_unknown_model_weight_is_one(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        assert rl.get_weight("bugfix", "unknown") == 1.0

    def test_unknown_model_success_rate_is_half(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        assert rl.get_success_rate("bugfix", "unknown") == 0.5

    def test_record_then_retrieve(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        rl.record("bugfix", "claude", "any", success=True)
        assert rl.get_weight("bugfix", "claude") > 1.0

    def test_rank_candidates_sorts_by_weight(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        rl.record("bugfix", "gpt-4", "any", success=True)    # weight up >1
        rl.record("bugfix", "claude", "any", success=False)  # weight down <1
        # deepseek 未记录 weight=1.0, 排在 claude(<1) 前面
        ranked = rl.rank_candidates("bugfix", ["claude", "gpt-4", "deepseek"])
        assert ranked[0] == "gpt-4"  # highest weight = success
        assert ranked[1] == "deepseek"  # 1.0 > claude's <1.0
        assert ranked[2] == "claude"

    def test_rank_candidates_cold_start_preserves_order(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        ranked = rl.rank_candidates("bugfix", ["claude", "gpt-4"])
        assert ranked == ["claude", "gpt-4"]

    def test_rank_respects_top_n(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        for m in ["a", "b", "c", "d"]:
            rl.record("fix", m, "any", success=(m == "a"))  # a wins
        ranked = rl.rank_candidates("fix", ["a", "b", "c", "d"], top_n=2)
        assert len(ranked) == 2

    def test_get_stats_returns_dict(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        rl.record("bugfix", "claude", "any", success=True, elapsed_ms=100, tokens=50)
        stats = rl.get_stats()
        assert "bugfix::claude" in stats
        assert stats["bugfix::claude"]["samples"] == 1

    def test_to_dict_serializable(self):
        from singularity.scheduler.route_learner import RouteLearner
        rl = RouteLearner()
        rl.record("feature", "gpt-4", "any", success=True)
        d = rl.to_dict()
        assert "feature::gpt-4" in d
