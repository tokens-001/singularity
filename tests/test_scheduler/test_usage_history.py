"""按天用量历史（usage_daily.json）— 折叠算法 + 范围整形。

**本文件里最重要的两条**：
① `TestMidnightRollover` —— 它抓的是"只重算今天那格"这个错误设计：
   `_daily` 是最近 500 条、**不按天过滤**，只重算今天会让昨天那批行每天过午夜被重算一次。
② `TestLegacyRowStillLoads` —— `UsageRecord(**r)` 的兼容守卫（同 test_token_budget.py）。
"""

import json
import time
from datetime import date, timedelta

import pytest

from singularity.scheduler import config
from singularity.scheduler import _token_budget as tb
from singularity.scheduler._token_budget import TokenBudget, history, _day_key


def _fresh() -> TokenBudget:
    """新建实例。conftest 的 autouse fixture 已把 QIDIAN_DIR 指到 tmp_path，
    而 _path/_history_path 在 __init__ 里现算 → 自动隔离。
    ⚠️ 不能用模块单例 `tb._budget`：它在 import 时就绑定了真实 .qidian/。"""
    return TokenBudget()


def _bind(monkeypatch, b: TokenBudget) -> None:
    """history() 读的是模块单例 `_budget` —— 换成本次测试的新实例。
    （这正是 API handler 必须函数内 import 的原因。）"""
    monkeypatch.setattr(tb, "_budget", b)


def _ts(day: str, hour: int = 12, minute: int = 0) -> float:
    return time.mktime(time.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M"))


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class TestMidnightRollover:
    """⚠️ 核心回归：折叠必须**按天各自归位**，不能只重算"今天"。

    错误设计（只重算 days[today]）：00:01 的第一条记录会把昨天那批行也算进今天
    → 每过一次午夜重复计一次。这条测试就是为抓它而写的。
    """

    def test_yesterday_not_folded_into_today(self):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 100, ts=_ts("2026-09-10", 23))
        assert b._rollup()["2026-09-10"]["tokens"] == 100

        b.record("p", "", "t2", "m", "any", 7, ts=_ts("2026-09-11", 1))

        rolled = b._rollup()
        assert rolled["2026-09-10"]["tokens"] == 100, "昨天的量被算进今天了"
        assert rolled["2026-09-11"]["tokens"] == 7, "今天的量不该含昨天"

    def test_rollover_across_many_days(self):
        b = _fresh()
        for i, day in enumerate(["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]):
            b.record("p", "", f"t{i}", "m", "any", 10 ** i, ts=_ts(day, 12))
        rolled = b._rollup()
        assert [rolled[d]["tokens"] for d in
                ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")] == [1, 10, 100, 1000]


class TestIdempotentFold:
    def test_folding_three_times_is_stable(self):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 100, ts=_ts("2026-09-11", 10))
        b.record("p", "", "t2", "m2", "any", 50, ts=_ts("2026-09-11", 11))
        a = b._rollup()
        b2 = json.loads(json.dumps(a))
        assert b._rollup() == a
        assert b._rollup() == b2

    def test_fold_does_not_lose_stored_days_when_daily_is_empty(self):
        # 盘上有历史、内存里没有（重启后 _daily 被裁掉）→ 不能把历史折没了
        b = _fresh()
        b._days = {"2026-01-01": {"tokens": 999, "tasks": 1, "elapsed_s": 0.0,
                                  "max_elapsed_s": 0.0, "models": {"m": 999}, "hours": [0] * 24}}
        assert b._rollup()["2026-01-01"]["tokens"] == 999


class TestBackfill:
    """老数据零迁移：token_usage.json 一个字节不动，首次读取就进桶。"""

    def _seed(self, rows):
        (config.QIDIAN_DIR / "token_usage.json").write_text(
            json.dumps({"daily": rows}), encoding="utf-8")

    def test_legacy_rows_appear_without_migration(self, monkeypatch):
        self._seed([{
            "project_id": "p1", "project_name": "proj", "task_id": "t1",
            "model": "m", "level": "any", "tokens": 1000,
            "cost_est": 0.187594,          # 老代码写进去的假费用，必须被忽略
            "ts": _ts("2026-09-11", 9),
        }])
        b = _fresh()          # 只构造，不调用任何迁移函数
        _bind(monkeypatch, b)

        h = history("all")
        assert h["totals"]["tokens"] == 1000
        assert h["totals"]["tasks"] == 1

    def test_legacy_row_still_loads(self):
        """`UsageRecord(**r)` 兼容守卫：删字段会让历史行抛 TypeError 被吞掉 → 历史整份消失。"""
        self._seed([{
            "project_id": "p", "project_name": "", "task_id": "t",
            "model": "m", "level": "any", "tokens": 5,
            "cost_est": 0.5, "ts": time.time(),
        }])
        b = _fresh()
        assert len(b._daily) == 1, "带 cost_est 的遗留行必须能载入"

    def test_row_without_elapsed_key_still_loads(self):
        # elapsed_s 是后加的字段，老行没有这个键 —— 有默认值，必须能载入
        self._seed([{
            "project_id": "p", "project_name": "", "task_id": "t",
            "model": "m", "level": "any", "tokens": 5, "cost_est": 0.0, "ts": time.time(),
        }])
        b = _fresh()
        assert b._daily[0].elapsed_s == 0.0


class TestRetention:
    def test_keeps_newest_400_days(self):
        b = _fresh()
        d0 = date(2020, 1, 1)
        b._days = {
            (d0 + timedelta(days=i)).isoformat(): {
                "tokens": i + 1, "tasks": 1, "elapsed_s": 0.0, "max_elapsed_s": 0.0,
                "models": {}, "hours": [0] * 24,
            } for i in range(500)
        }
        rolled = b._rollup()
        assert len(rolled) == 400
        assert min(rolled) == (d0 + timedelta(days=100)).isoformat(), "最旧的没被丢掉"
        assert max(rolled) == (d0 + timedelta(days=499)).isoformat(), "最新的被丢错了"

    def test_history_reports_earliest_for_truncated_all(self, monkeypatch):
        b = _fresh()
        d0 = date(2020, 1, 1)
        b._days = {
            (d0 + timedelta(days=i)).isoformat(): {
                "tokens": 1, "tasks": 1, "elapsed_s": 0.0, "max_elapsed_s": 0.0,
                "models": {}, "hours": [0] * 24,
            } for i in range(500)
        }
        _bind(monkeypatch, b)
        # "全部"被截断时必须报出真实起点，页面要标注"统计自 …"，否则就是在撒谎
        assert history("all")["earliest"] == (d0 + timedelta(days=100)).isoformat()


class TestUnpricedInvariant:
    def test_unpriced_model_cost_is_none_not_zero(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "paid", "any", 1_000_000, ts=time.time())
        b.record("p", "", "t2", "free_unknown", "any", 1_000_000, ts=time.time())
        _bind(monkeypatch, b)
        from singularity.scheduler import model_prices
        model_prices.set_price("paid", 0.20)

        h = history("all")
        by = {m["model"]: m for m in h["models"]}
        assert by["paid"]["cost"] == 0.2
        assert by["free_unknown"]["cost"] is None, "未配单价必须是 None，不是 0"
        assert h["totals"]["unpriced_models"] == ["free_unknown"]
        assert h["totals"]["cost"] == 0.2, "总额只该含已定价的"

    def test_price_filled_later_reprices_whole_history(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 1_000_000, ts=_ts(_days_ago(3), 12))
        _bind(monkeypatch, b)
        assert history("all")["models"][0]["cost"] is None

        from singularity.scheduler import model_prices
        model_prices.set_price("m", 0.5)
        assert history("all")["models"][0]["cost"] == 0.5, "读时算钱: 补价后历史一起变对"


class TestRangeSlicing:
    def test_dense_ascending_days(self, monkeypatch):
        """日历口径：本周=周一起、本月=1号起、全部=有记录的第一天起。"""
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 42, ts=time.time())
        _bind(monkeypatch, b)

        t = date.today()
        for rng, expect_start in (
            ("week", t - timedelta(days=t.weekday())),   # 周一
            ("month", t.replace(day=1)),
        ):
            h = history(rng)
            dates = [d["date"] for d in h["days"]]
            assert dates[0] == expect_start.isoformat(), f"{rng} 起始日不对"
            assert dates == sorted(dates), "必须升序"
            assert dates[-1] == _day_key(time.time()), "最后一天必须是今天"
            # 稠密无洞
            assert len(dates) == (t - expect_start).days + 1

    def test_all_starts_at_earliest(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 5, ts=_ts(_days_ago(5), 12))
        _bind(monkeypatch, b)
        h = history("all")
        assert h["days"][0]["date"] == h["earliest"]

    def test_bad_range_raises(self):
        with pytest.raises(ValueError):
            history("bogus")

    def test_days_are_zero_filled(self, monkeypatch):
        """只有今天有量 → 本月其余的日子都要补零出现，不能缺格。"""
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 7, ts=time.time())
        _bind(monkeypatch, b)
        h = history("month")
        assert len(h["days"]) == date.today().day
        assert sum(1 for d in h["days"] if d["tokens"] == 0) == date.today().day - 1


class TestEmptyStore:
    def test_no_files_at_all(self, monkeypatch):
        b = _fresh()
        _bind(monkeypatch, b)
        h = history("all")
        assert h["totals"]["tokens"] == 0
        assert h["totals"]["tasks"] == 0
        # 一条记录都没有 → "累计至今"就只到今天这一天（没有更早的起点）
        assert len(h["days"]) == 1
        assert h["days"][0]["date"] == _day_key(time.time())
        # 0 点是个合法时刻 —— 没数据必须是 None，不能冒充"高峰在 0 点"
        assert h["activity"]["peak_hour"] is None
        assert h["activity"]["peak_day"] is None

    def test_month_range_is_dense_even_with_no_data(self, monkeypatch):
        """本月是日历口径 —— 就算没数据也要铺满整月到今天的格子。"""
        b = _fresh()
        _bind(monkeypatch, b)
        h = history("month")
        assert len(h["days"]) == date.today().day

    def test_corrupt_history_file_does_not_break(self, monkeypatch):
        (config.QIDIAN_DIR / "usage_daily.json").write_text("{ not json", encoding="utf-8")
        b = _fresh()
        _bind(monkeypatch, b)
        assert history("all")["totals"]["tokens"] == 0


class TestDayKeyIsLocal:
    def test_local_day_boundary(self):
        """锁住 time.localtime 惯例。改成 UTC 的话，本地 00:30 会被算进前一天。"""
        assert _day_key(_ts("2026-09-11", 23, 30)) == "2026-09-11"
        assert _day_key(_ts("2026-09-12", 0, 30)) == "2026-09-12"

    def test_hours_bucket_uses_local_hour(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 10, ts=_ts(_days_ago(0), 15))
        rolled = b._rollup()
        assert rolled[_days_ago(0)]["hours"][15] == 10


class TestStreakDefinition:
    def test_counts_back_when_today_is_zero(self, monkeypatch):
        b = _fresh()
        for k in (1, 2, 3):
            b.record("p", "", f"t{k}", "m", "any", 10, ts=_ts(_days_ago(k), 12))
        _bind(monkeypatch, b)
        # 今天还没用量不算断，否则每天早上打开都是"0 天"
        assert history("all")["activity"]["current_streak"] == 3

    def test_today_counts_when_nonzero(self, monkeypatch):
        b = _fresh()
        for k in (0, 1):
            b.record("p", "", f"t{k}", "m", "any", 10, ts=_ts(_days_ago(k), 12))
        _bind(monkeypatch, b)
        assert history("all")["activity"]["current_streak"] == 2

    def test_gap_breaks_streak(self, monkeypatch):
        b = _fresh()
        for k in (0, 1, 3, 4):        # 第 2 天缺失 → 断
            b.record("p", "", f"t{k}", "m", "any", 10, ts=_ts(_days_ago(k), 12))
        _bind(monkeypatch, b)
        h = history("all")
        assert h["activity"]["current_streak"] == 2
        assert h["activity"]["longest_streak"] == 2


class TestDurationPartial:
    def test_zero_elapsed_when_not_reported(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 100, ts=time.time())
        _bind(monkeypatch, b)
        h = history("all")
        assert h["activity"]["elapsed_s"] == 0.0
        assert h["activity"]["max_elapsed_s"] == 0.0

    def test_elapsed_sums_and_max(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 100, elapsed_s=30.0, ts=time.time())
        b.record("p", "", "t2", "m", "any", 100, elapsed_s=90.0, ts=time.time())
        _bind(monkeypatch, b)
        h = history("all")
        assert h["activity"]["elapsed_s"] == 120.0
        assert h["activity"]["max_elapsed_s"] == 90.0

    def test_shaping_does_not_divide_by_zero(self, monkeypatch):
        b = _fresh()
        _bind(monkeypatch, b)
        history("all")   # 全零不抛
        history("all")
        history("all")


class TestConsistencyWithTodayView:
    """历史与今日两个视图的口径必须一致 —— 同一页上两个不一样的数字看着就像 bug。"""

    def test_task_and_token_counts_match(self, monkeypatch):
        b = _fresh()
        now = time.time()
        b.record("p", "", "t1", "m", "any", 100, ts=now)
        b.record("p", "", "t2", "m", "any", 250, ts=now)
        _bind(monkeypatch, b)

        h = history("all")
        assert h["totals"]["tokens"] == b.daily_total
        assert h["totals"]["tasks"] == sum(m["tasks"] for m in b.per_model_usage())


class TestPersistedFile:
    def test_record_writes_daily_file(self):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 123, ts=time.time())
        p = config.QIDIAN_DIR / "usage_daily.json"
        assert p.exists(), "日存文件没落盘"
        data = json.loads(p.read_text())
        assert data["v"] == 1
        assert data["days"][_day_key(time.time())]["tokens"] == 123

    def test_saved_file_round_trips(self):
        b1 = _fresh()
        b1.record("p", "", "t1", "m", "any", 55, ts=_ts("2026-09-11", 10))
        b2 = _fresh()          # 重新加载
        assert b2._rollup()["2026-09-11"]["tokens"] == 55


class TestConfiguredModelsAlwaysListed:
    """配置里有的模型**都要出现**，哪怕一次没用过。

    只列"花过钱的"会让你分不清"没跑过 / 跑失败了 / 账号欠费" ——
    用户配了 7 个只看到 1 个，第一反应就是"统计漏了"。
    """

    def _seed_models(self, *mids):
        from singularity.scheduler import api_store
        from singularity.scheduler import config as cfg
        (cfg.QIDIAN_DIR / "models_custom.json").write_text(
            json.dumps({m: {"id": m, "provider": "deepseek", "display": m,
                            "recommended_for": ["any"]} for m in mids}), encoding="utf-8")

    def test_unused_configured_model_appears_with_zero(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "used-model", "any", 1000, ts=time.time())
        self._seed_models("used-model", "never-used-model")
        _bind(monkeypatch, b)

        by_name = {m["model"]: m for m in history("all")["models"]}
        assert "never-used-model" in by_name, "配了但没用过的模型必须也列出来"
        assert by_name["never-used-model"]["tokens"] == 0
        assert by_name["never-used-model"]["used"] is False
        assert by_name["used-model"]["used"] is True

    def test_unused_model_is_not_in_unpriced_warning(self, monkeypatch):
        """没用过的模型没产生费用 —— 列进"未配置单价"警告是噪声。"""
        b = _fresh()
        b.record("p", "", "t1", "used-unpriced", "any", 1000, ts=time.time())
        self._seed_models("used-unpriced", "never-used-unpriced")
        _bind(monkeypatch, b)

        assert history("all")["totals"]["unpriced_models"] == ["used-unpriced"]

    def test_reports_provider_status_not_just_available(self, monkeypatch):
        """要报供应商状态原文 —— is_available 有"半开"机制，欠费的账号也返回 True，
        页面上写"可用"而用户需要看到的是"配额耗尽"。"""
        b = _fresh()
        self._seed_models("glm-like")
        _bind(monkeypatch, b)

        row = history("all")["models"][0]
        assert "provider_status" in row and "provider" in row
        assert "api_available" not in row, "别报那个会误导的布尔值"

    def test_unused_models_sort_after_used(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "zzz-used", "any", 1000, ts=time.time())
        self._seed_models("aaa-unused", "zzz-used")
        _bind(monkeypatch, b)

        names = [m["model"] for m in history("all")["models"]]
        # 用过的排前面，即使字母序在后面
        assert names.index("zzz-used") < names.index("aaa-unused")

    def test_broken_model_config_still_returns_history(self, monkeypatch):
        """取配置失败不该让整页打不开 —— 退化成"只列用过的"。"""
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 1000, ts=time.time())
        _bind(monkeypatch, b)
        (config.QIDIAN_DIR / "models_custom.json").write_text("{坏 json", encoding="utf-8")

        h = history("all")
        assert h["totals"]["tokens"] == 1000


class TestTodayRange:
    def test_today_is_a_single_day(self, monkeypatch):
        b = _fresh()
        b.record("p", "", "t1", "m", "any", 42, ts=time.time())
        b.record("p", "", "t2", "m", "any", 99, ts=_ts(_days_ago(3), 12))
        _bind(monkeypatch, b)

        h = history("today")
        assert len(h["days"]) == 1, "当天就该只有一格"
        assert h["days"][0]["date"] == _day_key(time.time())
        assert h["totals"]["tokens"] == 42, "不该把前几天的算进来"


class TestNoTruncationOnBusyDays:
    """单日记录多的时候不能少算。

    以前 `_daily` 是 `[-500:]` 的定长窗口 —— 一天分派超过 500 次，
    当天更早的行会被挤掉，当天的量随之少算（截断，不是编造，但确实少报）。
    改成按天保留后没这个问题。
    """

    def test_busy_day_is_not_truncated(self, monkeypatch):
        b = _fresh()
        now = time.time()
        n = 800
        for i in range(n):
            b.record("p", "", f"t{i}", "m", "any", 10, ts=now)
        _bind(monkeypatch, b)

        assert b.daily_total == n * 10, "当天的量被截断了"
        h = history("today")
        assert h["totals"]["tokens"] == n * 10
        assert h["totals"]["tasks"] == n

    def test_old_rows_are_pruned_but_history_kept(self, monkeypatch):
        """两天前的原始行会被清掉（历史已在 _days 里），但统计不受影响。"""
        b = _fresh()
        b.record("p", "", "old", "m", "any", 777, ts=_ts(_days_ago(5), 12))
        b.record("p", "", "new", "m", "any", 111, ts=time.time())
        assert b.daily_total == 111, "过期的原始行不该算进今天"
        _bind(monkeypatch, b)
        # 但历史里那 777 还在
        assert history("all")["totals"]["tokens"] == 777 + 111


class TestRouteLearnerRejectsEmptyModel:
    """model 为空的样本要丢掉。

    任务被取消/分解/冲突时 `disp_result` 是 None，调用方拿不到模型名就传了空串。
    这种样本学不到"哪个模型好"（键退化成 `type::`），只会在统计里积脏账 ——
    实测真积过一条"0 成功 15 失败"的。
    """

    def test_empty_model_is_dropped(self, monkeypatch):
        from singularity.scheduler import route_learner as rl
        monkeypatch.setattr(rl, "_LEARNER_PATH", config.QIDIAN_DIR / "route_learner.json")
        learner = rl.RouteLearner()
        learner.record(task_type="default", model="", level="any", success=False)
        assert learner._stats == {}, "空模型样本不该进统计"

    def test_named_model_still_records(self, monkeypatch):
        from singularity.scheduler import route_learner as rl
        monkeypatch.setattr(rl, "_LEARNER_PATH", config.QIDIAN_DIR / "route_learner.json")
        learner = rl.RouteLearner()
        learner.record(task_type="default", model="m", level="any", success=True)
        assert "default::m" in learner._stats
