"""P4 预算真实化：项目**累计**花费 + 80% / 100% 两档。

背景：`token_budget_total`（项目预算）此前全仓**没有真正的消费点** —— 只在
API/CLI 显示层被读过。根子之一是"**分子根本不存在**"：`per_project_usage` 只算
**今天**，跨天的项目永远到不了线。

这里钉三件事：
  1. 按天折叠的桶要带**项目 × 模型**两级（只有项目不够，算钱要按模型查单价）
  2. 累计值 = 跨天求和（同日项目应与"今日"一致，这是最容易写错的一步）
  3. 80% 告警、100% 停
"""
import pytest

from singularity.scheduler import _token_budget as tb


def _rec(pid, model, tokens, ts=1000.0):
    return tb.UsageRecord(project_id=pid, model=model, tokens=tokens,
                          ts=ts, level="any")


def _bucket(**kw):
    base = {"tokens": 0, "tasks": 0, "elapsed_s": 0.0, "max_elapsed_s": 0.0,
            "models": {}, "hours": [0] * 24}
    base.update(kw)
    return base


class TestBucketKeepsProjects:
    def test_groups_by_project_and_model(self):
        b = tb._bucket([_rec("p1", "m1", 100), _rec("p1", "m1", 50),
                        _rec("p1", "m2", 7), _rec("p2", "m1", 9)])
        assert b["projects"]["p1"] == {"m1": 150, "m2": 7}
        assert b["projects"]["p2"] == {"m1": 9}

    def test_bump_merges_projects_per_key_by_max(self):
        a = _bucket(projects={"p1": {"m1": 100}})
        new = _bucket(projects={"p1": {"m1": 150, "m2": 5}})
        assert tb._bump(a, new)["projects"]["p1"] == {"m1": 150, "m2": 5}

    def test_bump_tolerates_old_bucket_without_projects(self):
        """老 usage_daily.json 里没这个键 —— 不能炸，也不能把新数据吞掉。"""
        out = tb._bump(_bucket(), _bucket(projects={"p1": {"m1": 5}}))
        assert out["projects"] == {"p1": {"m1": 5}}


class TestProjectSpendTotal:
    def test_sums_across_days_and_prices_each_model(self, monkeypatch):
        b = tb.TokenBudget()
        monkeypatch.setattr(b, "_rollup", lambda: {
            "2026-09-11": {"projects": {"p1": {"deepseek-flash": 1_000_000}}},
            "2026-09-12": {"projects": {"p1": {"glm-5.3-flash": 1_000_000}}},
        })
        monkeypatch.setattr(tb.model_prices, "load_prices",
                            lambda: {"deepseek-flash": 0.48, "glm-5.3-flash": 0.25})
        assert b.project_spend_total("p1") == 0.73, "跨天要累加，不是只算最后一天"

    def test_unknown_project_is_zero(self, monkeypatch):
        b = tb.TokenBudget()
        monkeypatch.setattr(b, "_rollup", lambda: {"2026-09-12": {"projects": {}}})
        assert b.project_spend_total("nope") == 0.0

    def test_unpriced_model_counts_as_zero_not_crash(self, monkeypatch):
        """没配单价的模型 → 不计入（下限），不许炸。"""
        b = tb.TokenBudget()
        monkeypatch.setattr(b, "_rollup", lambda: {
            "2026-09-12": {"projects": {"p1": {"fusion(a,b)": 1_000_000}}}})
        monkeypatch.setattr(tb.model_prices, "load_prices", lambda: {})
        assert b.project_spend_total("p1") == 0.0


class TestProjectBudgetState:
    @pytest.mark.parametrize("spent,budget,expect", [
        (0.0, 5.0, ""),        # 0%
        (3.9, 5.0, ""),        # 78% —— 不到线
        (4.0, 5.0, "warn"),    # 80%
        (4.9, 5.0, "warn"),
        (5.0, 5.0, "stop"),    # 100%
        (6.0, 5.0, "stop"),    # 超了也是 stop
    ])
    def test_thresholds(self, monkeypatch, spent, budget, expect):
        monkeypatch.setattr(tb._budget, "project_spend_total", lambda pid: spent)
        level, got, msg = tb.project_budget_state("p1", budget)
        assert level == expect
        assert (msg != "") == (expect != ""), "有档位就得有话说"
        assert got == spent

    def test_no_budget_means_no_check(self, monkeypatch):
        """没配预算（0 / None）→ 不检查、不报。别把"没配"当成"没花钱"。"""
        monkeypatch.setattr(tb._budget, "project_spend_total",
                            lambda pid: pytest.fail("没配预算就不该去查花费"))
        for b in (0.0, None):
            assert tb.project_budget_state("p1", b)[0] == ""
