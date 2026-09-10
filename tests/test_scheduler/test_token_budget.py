"""_token_budget.py 单元测试 — 费用读时现算 + 绝不编造金额。

背景（2026-09-11）：费用原来在**写盘时**用一张写死的价目表算好存进 cost_est，
配 `rates.get(model, 0.50)` 兜底 —— 用户实际在用的 deepseek-v4-flash 不在表里，
界面上那 $0.19 是拿假的 $0.50/百万 算出来的。

改成读时用真实单价现算后，本文件锁住两件事：
  ① 没配单价 → 返回 None，**不是** 0（0 会被前端渲染成看着可信的 $0.00）
  ② 配上单价 → 连**已经记过的**历史数据也立刻算对（不需要迁移）
"""

import json

from singularity.scheduler import config, model_prices
from singularity.scheduler._token_budget import TokenBudget


def _fresh() -> TokenBudget:
    """新建一个干净实例。构造时读 config.QIDIAN_DIR（conftest 已隔离到 tmp_path）。"""
    return TokenBudget()


class TestNoFabrication:
    def test_unpriced_model_reports_none_not_zero(self):
        b = _fresh()
        b.record("p1", "proj", "t1", "some-model", "any", 1_000_000)

        row = b.per_model_usage()[0]
        # is None 而非 == 0 —— 这才抓得住日后有人偷偷加回默认费率
        assert row["cost"] is None, "未配单价的模型必须返回 None，不能给数字"
        assert b.daily_cost == 0.0
        assert b.unpriced_models == ["some-model"]

    def test_daily_cost_sums_only_priced_models(self):
        b = _fresh()
        b.record("p1", "proj", "t1", "priced", "any", 1_000_000)
        b.record("p1", "proj", "t2", "unpriced", "any", 5_000_000)
        model_prices.set_price("priced", 0.20)

        assert b.daily_cost == 0.2, "只该算进已定价的那条"
        assert b.unpriced_models == ["unpriced"]

    def test_by_project_cost_excludes_unpriced(self):
        b = _fresh()
        b.record("p1", "proj", "t1", "priced", "any", 1_000_000)
        b.record("p1", "proj", "t2", "unpriced", "any", 9_000_000)
        model_prices.set_price("priced", 0.20)

        assert b.per_project_usage()[0]["cost"] == 0.2
        assert b.per_project_usage()[0]["tokens"] == 10_000_000


class TestSelfHealing:
    """读时现算的核心收益：补上单价，历史数据自动变对，零迁移。"""

    def test_historical_rows_recompute_after_price_is_set(self):
        b = _fresh()
        b.record("p1", "proj", "t1", "m", "any", 375_188)
        assert b.per_model_usage()[0]["cost"] is None

        model_prices.set_price("m", 0.28)

        # 没有重新 record，没有迁移 —— 同一批数据现在算对了
        # daily_cost 汇总保留 4 位，单模型行保留 6 位（亚分位费用要看得见）
        assert b.daily_cost == round(375_188 / 1e6 * 0.28, 4)
        assert b.per_model_usage()[0]["cost"] == round(375_188 / 1e6 * 0.28, 6)

    def test_clearing_price_hides_cost_again(self):
        b = _fresh()
        b.record("p1", "proj", "t1", "m", "any", 1_000_000)
        model_prices.set_price("m", 0.14)
        assert b.daily_cost == 0.14

        model_prices.set_price("m", None)
        assert b.daily_cost == 0.0
        assert b.per_model_usage()[0]["cost"] is None


class TestPoisonedHistory:
    """老代码写进 token_usage.json 的是编造的费用，必须被彻底忽略。"""

    def _seed(self, rows: list[dict]):
        (config.QIDIAN_DIR / "token_usage.json").write_text(
            json.dumps({"daily": rows}), encoding="utf-8")

    def test_stored_cost_est_is_ignored(self):
        import time
        self._seed([{
            "project_id": "p1", "project_name": "proj", "task_id": "t1",
            "model": "deepseek-v4-flash", "level": "any",
            "tokens": 375_188,
            "cost_est": 0.187594,          # ← 老代码算出来的假值
            "ts": time.time(),
        }])
        b = _fresh()

        # 报 None，而不是把那个假值原样透出来
        assert b.per_model_usage()[0]["cost"] is None
        assert b.daily_cost == 0.0, "存盘的 cost_est 必须完全不参与计算"

    def test_stored_cost_est_is_recomputed_when_price_known(self):
        import time
        self._seed([{
            "project_id": "p1", "project_name": "proj", "task_id": "t1",
            "model": "m", "level": "any", "tokens": 1_000_000,
            "cost_est": 0.187594, "ts": time.time(),
        }])
        model_prices.set_price("m", 0.30)
        b = _fresh()

        assert b.daily_cost == 0.30, "该用真实单价重算，而不是沿用存的假值"

    def test_legacy_rows_with_cost_est_still_load(self):
        """载入兼容守卫：cost_est 列必须留在 UsageRecord 上。

        _load() 走 UsageRecord(**r)；一旦删掉该字段，历史行会抛 TypeError
        被 except 吞掉，self._daily 变空 → 用户整份用量历史静默消失。
        """
        import time
        self._seed([{
            "project_id": "p1", "project_name": "proj", "task_id": "t1",
            "model": "m", "level": "any", "tokens": 100,
            "cost_est": 0.5, "ts": time.time(),
        }])
        b = _fresh()
        assert len(b._daily) == 1, "带 cost_est 的历史行必须能载入"
        assert b.daily_total == 100


class TestBudgetWarning:
    def test_warning_flags_unpriced_models(self):
        b = _fresh()
        b.set_budget(daily=1.0)
        b.record("p1", "proj", "t1", "priced", "any", 8_000_000)
        b.record("p1", "proj", "t2", "unpriced", "any", 9_000_000)
        model_prices.set_price("priced", 0.10)   # $0.80 → 80% > 70%

        w = b.budget_warning
        assert "日预算已用" in w
        # 没配单价的没算进去 → 这个百分比是下限，必须说清楚
        assert "未配置价格" in w

    def test_no_suffix_when_everything_priced(self):
        b = _fresh()
        b.set_budget(daily=1.0)
        b.record("p1", "proj", "t1", "priced", "any", 8_000_000)
        model_prices.set_price("priced", 0.10)

        w = b.budget_warning
        assert "日预算已用" in w
        assert "未配置价格" not in w
