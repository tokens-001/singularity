"""内部模块 — Token 消耗追踪 & 预算管控。

实时追踪每个项目/每天的 token 消耗，支持预算上限和自动降级建议。
持久化: .qidian/token_usage.json
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from singularity.scheduler import config, model_prices, witness
from singularity.scheduler._io import atomic_write_json

# record() 是"读内存列表→append→整份写盘"，两个线程同时进来会互相盖掉。
# _budget 是模块级单例（见文件尾），所以一把模块锁就够。
_LOCK = threading.RLock()


@dataclass
class UsageRecord:
    project_id: str = ""
    project_name: str = ""
    task_id: str = ""
    model: str = ""
    level: str = ""
    tokens: int = 0
    # ⚠️ 遗留列，**不要删**。2026-09-11 之前费用是写盘时算的，存的就是这个字段，
    # 而老代码用一张写死的价目表 + `rates.get(model, 0.50)` 兜底 → 存进去的多半是编的。
    # 现在改成读时用真实单价现算，不再写也不再读它 —— 但字段必须留着：
    # 下面 _load() 走 `UsageRecord(**r)`，删掉这个字段会让所有历史行抛 TypeError，
    # 被 except 吞掉后 self._daily 变空 → **用户整份用量历史静默消失**。
    cost_est: float = 0.0
    ts: float = 0.0
    # 任务执行耗时(秒)。有默认值 → 老行没有这个键也能载入（`UsageRecord(**r)` 只对多余键报错）。
    # ⚠️ 是**低估**：融合路径 `_dispatch_exec.py` 恒为 0、取消/分解/冲突路径 disp_result 为 None。
    # 所以 UI 只在求和 > 0 时才渲染"总使用时长"，绝不显示成 0.0h。
    elapsed_s: float = 0.0


def _row_cost(tokens: int, model: str, prices: dict[str, float]) -> float | None:
    """一行用量的费用 (USD)。**该模型没配单价就返回 None，绝不兜底。**

    返回 None 而不是 0.0 是刻意的：0.0 会被前端渲染成一个看着可信的 $0.00，
    那正是本次要消灭的"编造金额"。调用方必须把 None 如实显示成"未配置价格"。
    """
    p = prices.get(model)
    return None if p is None else round(tokens / 1_000_000 * p, 6)


# ═══════════════════════════════════════════════════════════════
# 按天历史（usage_daily.json）— 热力图 / 趋势 / 连续天数靠它
# ═══════════════════════════════════════════════════════════════

_MAX_DAYS = 400          # 保留最近 400 天（count 上限，与 _save 的 [-500:] 同形态）
_HOURS = 24


def _day_key(ts: float) -> str:
    """epoch → 本地日期键。

    **必须用 time.localtime（本地时区）** —— 全仓的日键惯例就是它
    （daily_total / _today_records / level_breakdown 都用这个）。改成 UTC 会让
    "今天"的边界整体平移，和侧边栏显示的"今日"对不上。有测试锁着。
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _bucket(rows: list["UsageRecord"]) -> dict:
    """一组记录 → 当天的聚合桶。

    ⚠️ **桶里每个字段都必须对行集合单调不减** —— 这是 `_bump` 用 max 合并的前提。
    谁要往这儿加"平均"、"最大间隔"这类非单调字段，max 合并立刻就是错的。
    """
    b = {"tokens": 0, "tasks": 0, "elapsed_s": 0.0, "max_elapsed_s": 0.0,
         "models": {}, "hours": [0] * _HOURS}
    for r in rows:
        b["tokens"] += r.tokens
        b["elapsed_s"] += r.elapsed_s
        b["max_elapsed_s"] = max(b["max_elapsed_s"], r.elapsed_s)
        m = r.model or "_unknown"
        b["models"][m] = b["models"].get(m, 0) + r.tokens
        b["hours"][time.localtime(r.ts).tm_hour] += r.tokens
        # 数**记录条数**，不是去重任务数 —— 与 per_model_usage / per_project_usage
        # 现有的 `+= 1` 口径保持一致。两边口径不同会让同一页上出现两个不一样的"任务数"，
        # 看着就像 bug。（已知同一个 task 偶有两条记录，那是另一个问题。）
        b["tasks"] += 1
    b["models"] = {k: v for k, v in sorted(b["models"].items())}
    return b


def _bump(old: dict | None, new: dict) -> dict:
    """两个桶按分量取 max 合并（不是相加 —— 见 _rollup 的理由）。"""
    if not old:
        return new
    hours = list(old.get("hours") or [])
    if len(hours) != _HOURS:
        hours = [0] * _HOURS
    out = {
        "tokens": max(old.get("tokens", 0), new["tokens"]),
        "tasks": max(old.get("tasks", 0), new["tasks"]),
        "elapsed_s": max(old.get("elapsed_s", 0.0), new["elapsed_s"]),
        "max_elapsed_s": max(old.get("max_elapsed_s", 0.0), new["max_elapsed_s"]),
        "models": {},
        "hours": hours,
    }
    for k, v in (old.get("models") or {}).items():
        out["models"][k] = v
    for k, v in new["models"].items():
        out["models"][k] = max(out["models"].get(k, 0), v)
    for i, v in enumerate(new["hours"]):
        out["hours"][i] = max(out["hours"][i], v)
    out["models"] = {k: v for k, v in sorted(out["models"].items())}
    return out


class TokenBudget:
    """全局 token 预算管理器。"""

    def __init__(self):
        self._path = config.QIDIAN_DIR / "token_usage.json"
        self._history_path = config.QIDIAN_DIR / "usage_daily.json"
        self._daily: list[UsageRecord] = []
        self._days: dict[str, dict] = {}
        self._budget_daily: float = 0.0
        self._budget_monthly: float = 0.0
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._daily = [UsageRecord(**r) for r in data.get("daily", [])]
                self._budget_daily = data.get("budget_daily", 0.0)
                self._budget_monthly = data.get("budget_monthly", 0.0)
            except Exception as e:
                witness.warn('_token_budget', f'{e}')
        if self._history_path.exists():
            try:
                h = json.loads(self._history_path.read_text())
                days = h.get("days", {})
                if isinstance(days, dict):
                    self._days = {str(k): v for k, v in days.items() if isinstance(v, dict)}
            except Exception as e:
                # 坏文件 = 历史暂时读不出来，但不该让记账整体挂掉 —— 内存里还能从 _daily 重建
                witness.warn('_token_budget', f'history_load:{e}')

    def _rollup(self) -> dict[str, dict]:
        """把盘上的历史桶与内存里的记录折叠成完整历史。**纯函数，不碰 I/O。**

        为什么是"**按天各自归位** + **分量取 max**"，而不是"过去以盘为准、今天重算"：
        `_daily` 是最近 500 条、**不按天过滤**。若只重算 `days[今天]`，那么每天 00:01
        的第一条记录会把**昨天那批行一起算进今天** —— 每过一次午夜就重复计一次。
        把每一行归到它自己那天就没这个问题。

        为什么 max 是对的：桶里每个字段对行集合都**单调不减**（非负 token/秒的求和、
        去重任务数、各模型/各小时计数）。盘上那格来自某个子集，现算的来自另一个子集，
        两者都 ⊆ 真值 → max 仍 ≤ 真值。而历史快照的最大值就是真值：`_daily` 是滚动窗口，
        某天的记录总有"全部都在窗口里"的那一刻（该天记录数 ≤ 500 时），那一刻的值被留住了。
        **已知上限**：单日记录 > 500 条时会低算（窗口装不下），这里不假装没有。

        这么折还白拿三件事：① 零迁移 —— 老 `token_usage.json` 一条不动，
        现有记录首次保存/首次读取就自动进桶；② 自愈 —— 两次写之间崩了，下次折叠补回来；
        ③ 读侧不依赖新写入 —— 老记录立刻可见。
        """
        days = {d: dict(b) for d, b in self._days.items()}
        grouped: dict[str, list[UsageRecord]] = {}
        for r in self._daily:
            grouped.setdefault(_day_key(r.ts), []).append(r)
        for d, rows in grouped.items():
            days[d] = _bump(days.get(d), _bucket(rows))
        return dict(sorted(days.items())[-_MAX_DAYS:])

    def _save_history(self) -> None:
        """折叠并落盘。**调用方必须已持有 _LOCK** —— 它要读 self._daily。"""
        rolled = self._rollup()
        atomic_write_json(self._history_path, {"v": 1, "days": rolled})
        self._days = rolled

    def _save(self):
        data = {
            "daily": [r.__dict__ for r in self._daily[-500:]],
            "budget_daily": self._budget_daily,
            "budget_monthly": self._budget_monthly,
        }
        # 原子写: 原来 write_text 直写，撕一次会让 _load 走 except 分支保持 self._daily=[]，
        # 而 _budget 是模块级单例 —— 那份空列表会一直留到进程重启，累积用量整份消失。
        atomic_write_json(self._path, data)

    def record(self, project_id: str, project_name: str, task_id: str,
               model: str, level: str, tokens: int,
               elapsed_s: float = 0.0, ts: float | None = None):
        # 写盘时**不算钱**。费用一律在读时用真实单价现算（见 _row_cost）——
        # 这样用户填上单价的瞬间，之前记的所有账也自动变对，不需要任何数据迁移。
        rec = UsageRecord(
            project_id=project_id, project_name=project_name,
            task_id=task_id, model=model, level=level,
            tokens=tokens, ts=time.time() if ts is None else ts,
            elapsed_s=elapsed_s or 0.0,
        )
        with _LOCK:
            self._daily.append(rec)
            if len(self._daily) > 500:
                self._daily = self._daily[-500:]
            self._save()
            # 折叠必须在锁内：_rollup 读 self._daily，而上一行刚改过它
            self._save_history()

    def set_budget(self, daily: float = 0.0, monthly: float = 0.0):
        # 必须持锁: 本方法由 Flask 请求线程调用，而 record() 在调度线程 ——
        # 两者都在"读 _daily → 重写整份文件"，不加锁会互相盖掉（丢更新）。
        # 加上日存之后 _days 也归这把锁管，更不能裸奔。
        with _LOCK:
            self._budget_daily = daily
            self._budget_monthly = monthly
            self._save()
            self._save_history()

    @property
    def daily_total(self) -> int:
        today = time.strftime("%Y-%m-%d")
        return sum(r.tokens for r in self._daily
                   if time.strftime("%Y-%m-%d", time.localtime(r.ts)) == today)

    def _today_records(self) -> list[UsageRecord]:
        today = time.strftime("%Y-%m-%d")
        return [r for r in self._daily
                if time.strftime("%Y-%m-%d", time.localtime(r.ts)) == today]

    @property
    def unpriced_models(self) -> list[str]:
        """今天用过、但没配单价的模型。这些模型的费用无法计算 —— 必须让用户看见。"""
        prices = model_prices.load_prices()
        return sorted({r.model or "_unknown" for r in self._today_records()
                       if (r.model or "_unknown") not in prices})

    @property
    def daily_cost(self) -> float:
        """今日费用。**只统计配了单价的模型** —— 是个下限，不是总数。"""
        prices = model_prices.load_prices()
        return round(sum(c for c in (_row_cost(r.tokens, r.model, prices)
                                     for r in self._today_records()) if c is not None), 4)

    @property
    def budget_warning(self) -> str:
        """预算告警: 空字符串=正常, 否则为告警消息。"""
        if self._budget_daily > 0:
            pct = self.daily_cost / self._budget_daily
            msg = ""
            if pct > 0.9:
                msg = f"日预算已用 {pct*100:.0f}% (${self.daily_cost:.2f}/${self._budget_daily:.2f})，建议暂停强力层任务"
            elif pct > 0.7:
                msg = f"日预算已用 {pct*100:.0f}%，建议优先使用廉价层模型"
            # 没配单价的模型不计入 daily_cost，所以这个百分比是**下限**。
            # 不说清楚的话，用户会以为还没到预算线，实际可能早超了。
            if msg and self.unpriced_models:
                msg += "（部分模型未配置价格，实际花费可能更高）"
            return msg
        return ""

    def per_project_usage(self) -> list[dict]:
        """按项目汇总今日 token 用量。cost 只统计配了单价的模型（下限）。"""
        prices = model_prices.load_prices()
        by_project: dict[str, dict] = {}
        for r in self._today_records():
            pid = r.project_id or "_unknown"
            if pid not in by_project:
                by_project[pid] = {"project_id": pid, "project_name": r.project_name,
                                   "tokens": 0, "cost": 0.0, "tasks": 0}
            by_project[pid]["tokens"] += r.tokens
            c = _row_cost(r.tokens, r.model, prices)
            if c is not None:
                by_project[pid]["cost"] += c
            by_project[pid]["tasks"] += 1
        for v in by_project.values():
            v["cost"] = round(v["cost"], 6)
        return sorted(by_project.values(), key=lambda x: x["tokens"], reverse=True)

    def per_model_usage(self) -> list[dict]:
        """按**模型**汇总今日 token 用量。

        这才是能拿来做决策的维度：切哪个模型贵、哪个模型吃 token 最多、
        预算要不要挪。原来的总量/按项目/按层级都回答不了"我该换谁"。

        带三列：tokens（绝对量）、share（占比）、cost（费用）——
        占比是关键，总量涨了到底是"活多了"还是"某个模型变贵了"只能靠它区分。

        **cost 是 `float | None`**：该模型没配单价时是 None，不是 0 ——
        前端必须如实显示"未配置价格"。渲染成 $0.00 就是在编造金额。
        """
        prices = model_prices.load_prices()
        by_model: dict[str, dict] = {}
        for r in self._today_records():
            m = r.model or "_unknown"
            if m not in by_model:
                by_model[m] = {"model": m, "tokens": 0, "cost": None, "tasks": 0,
                               "price": prices.get(m)}   # None = 未配置单价
            by_model[m]["tokens"] += r.tokens
            c = _row_cost(r.tokens, r.model, prices)
            if c is not None:
                by_model[m]["cost"] = round((by_model[m]["cost"] or 0.0) + c, 6)
            by_model[m]["tasks"] += 1
        total = sum(v["tokens"] for v in by_model.values()) or 1
        rows = sorted(by_model.values(), key=lambda x: x["tokens"], reverse=True)
        for v in rows:
            v["share"] = round(v["tokens"] / total, 4)
        return rows

    def per_model_by_level(self) -> list[dict]:
        """按 (模型 × 层级) 汇总 —— 回答"这个模型只在架构阶段贵，还是全程都贵"。"""
        acc: dict[tuple[str, str], dict] = {}
        for r in self._today_records():
            k = (r.model or "_unknown", r.level or "?")
            if k not in acc:
                acc[k] = {"model": k[0], "level": k[1], "tokens": 0, "tasks": 0}
            acc[k]["tokens"] += r.tokens
            acc[k]["tasks"] += 1
        return sorted(acc.values(), key=lambda x: x["tokens"], reverse=True)

    def level_breakdown(self) -> dict[str, int]:
        """按层级汇总 token。"""
        today = time.strftime("%Y-%m-%d")
        by_level: dict[str, int] = {}
        for r in self._daily:
            if time.strftime("%Y-%m-%d", time.localtime(r.ts)) != today:
                continue
            lv = r.level or "?"
            by_level[lv] = by_level.get(lv, 0) + r.tokens
        return by_level


# 全局单例
_budget = TokenBudget()


def record_tokens(project_id: str = "", project_name: str = "", task_id: str = "",
                  model: str = "", level: str = "", tokens: int = 0,
                  elapsed_s: float = 0.0):
    """记录一次 token 消耗。调度循环在 dispatch 完成后调用。"""
    if tokens > 0:
        _budget.record(project_id, project_name, task_id, model, level, tokens,
                       elapsed_s=elapsed_s)


def record_system_tokens(model: str, level: str, tokens: int,
                         elapsed_s: float = 0.0) -> None:
    """记录**不属于任何任务**的系统调用用量：观察者对话 / 任务分类 / 架构融合 /
    记忆整合 / 目标循环。

    这些调用一样花钱。之前只有"派任务去干活"那条路记账，于是统计只覆盖了
    全部 LLM 调用的一小部分 —— 你在 Chat 里聊的、建任务时做分类花的、
    多模型定稿花的，全都不进账。

    `level` 用来区分用途（`by_level` 就能按用途拆开看）；
    project_id/task_id 留空（这些调用本来就不属于某个项目）。
    """
    if tokens > 0:
        _budget.record("", "", "", model, level, tokens, elapsed_s=elapsed_s)


def get_usage_stats() -> dict:
    """供 API 查询的用量统计。"""
    b = _budget
    return {
        "daily_tokens": b.daily_total,
        "daily_cost": b.daily_cost,
        "budget_daily": b._budget_daily,
        "budget_monthly": b._budget_monthly,
        "warning": b.budget_warning,
        "by_project": b.per_project_usage(),
        "by_level": b.level_breakdown(),
        # 按模型才是能拿来做决策的维度：总量只告诉你"花了多少"，
        # 回答不了"该换谁 / 谁在吃预算"。
        "by_model": b.per_model_usage(),
        "by_model_level": b.per_model_by_level(),
        # 今天用过但没配单价的模型。非空 = 上面的 daily_cost 只是**下限**，
        # 前端据此在总额上标 "+"，别让用户以为那就是全部。
        "unpriced_models": b.unpriced_models,
    }


# 范围 → 天数（None = 全部，即盘上最早那天起）
# 日历口径，不是"最近 N 天"的滚动窗口 —— 起止日见 _range_start
_RANGES = ("all", "month", "week", "today")


def _streak(days: list[dict]) -> tuple[int, int]:
    """(当前连续, 最长连续)，单位"天"，按 tokens>0 计。

    **当前连续的定义**：从今天往回数连续非零天；**今天为 0 则从昨天往回数** ——
    否则每天早上打开都是一片 0，看着像断了。另一种定义（严格算今天）也说得通，
    所以这里写死并被测试锁住，不让代码和预期悄悄分叉。
    """
    longest = cur = 0
    for d in days:
        if d["tokens"] > 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    # 从末尾（今天）往回数
    i = len(days) - 1
    if i >= 0 and days[i]["tokens"] == 0:
        i -= 1          # 今天还没用量不算断
    now = 0
    while i >= 0 and days[i]["tokens"] > 0:
        now += 1
        i -= 1
    return now, longest


def _range_start(range_: str, today: str, earliest: str) -> str:
    """范围的起始日。**日历口径**，不是"最近 N 天"的滚动窗口。

    - `all`   → 累计至今：从有记录的第一天起
    - `month` → 本月：当月 1 号
    - `week`  → 本周：**周一**起（中文习惯；Python 的 weekday() 就是周一=0）
    - `today` → 当天：只有今天一格
    """
    from datetime import date as _date, timedelta as _td

    if range_ == "all":
        return earliest
    d = _date.fromisoformat(today)
    if range_ == "today":
        return today
    if range_ == "month":
        return d.replace(day=1).isoformat()
    if range_ == "week":
        return (d - _td(days=d.weekday())).isoformat()
    raise ValueError(f"range 只支持 {'/'.join(_RANGES)}")


def history(range_: str = "all") -> dict:
    """按范围的用量历史。纯函数（读 `_budget` 的内存态），无 Flask 上下文即可测。

    非法 range 抛 ValueError，由 handler 转 400。
    """
    from datetime import date as _date, timedelta as _td

    if range_ not in _RANGES:
        raise ValueError(f"range 只支持 {'/'.join(_RANGES)}")

    b = _budget
    rolled = b._rollup()                      # 含未落盘的内存记录 → 老数据零迁移可见
    stored = sorted(rolled.items())
    earliest = stored[0][0] if stored else _day_key(time.time())

    today = _day_key(time.time())
    start = _range_start(range_, today, earliest)

    # 稠密升序补零: 客户端不该自己算日历
    days: list[dict] = []
    cur = _date.fromisoformat(start)
    end = _date.fromisoformat(today)
    while cur <= end:
        k = cur.isoformat()
        bucket = rolled.get(k) or {}
        days.append({"date": k,
                     "tokens": bucket.get("tokens", 0),
                     "tasks": bucket.get("tasks", 0),
                     "elapsed_s": round(bucket.get("elapsed_s", 0.0), 1)})
        cur += _td(days=1)

    # 范围内按模型 + 读时算钱（价格一填，整段历史一起变对）
    prices = model_prices.load_prices()
    in_range = [k for k, _ in stored if start <= k <= today]
    tok: dict[str, int] = {}
    hours = [0] * _HOURS
    elapsed_all = 0.0
    max_elapsed = 0.0
    for k in in_range:
        bucket = rolled[k]
        elapsed_all += bucket.get("elapsed_s", 0.0)
        max_elapsed = max(max_elapsed, bucket.get("max_elapsed_s", 0.0))
        for m, v in (bucket.get("models") or {}).items():
            tok[m] = tok.get(m, 0) + v
        for i, v in enumerate(bucket.get("hours") or []):
            if i < _HOURS:
                hours[i] += v
    # 配置里的模型**全都要出现在统计里**，哪怕一次没用过。
    # 只列"花过钱的"会让你分不清"没跑过 / 跑失败了 / 配置有问题" ——
    # 配了 7 个只显示 1 个，看着就像统计漏了。
    # 报 **provider 状态原文**而不是 is_available 的布尔值：后者有"半开"机制
    # （冷却期后放行一次去探测），配额耗尽的账号也会返回 True ——
    # 那样页面上会写着"可用"，而用户真正需要看到的是"配额耗尽"。
    avail: dict[str, dict] = {}
    try:
        from singularity.scheduler import api_store, model_registry
        entries = model_registry.load_models()
        configured = list(model_registry._load_custom().keys())
        for mid in configured:
            tok.setdefault(mid, 0)
            prov = getattr(entries.get(mid), "provider", "") or ""
            entry = api_store.get(prov) if prov else None
            avail[mid] = {"provider": prov,
                          "provider_status": getattr(entry, "status", None) if entry else None}
    except Exception as e:
        # 取配置失败不该让整页打不开 —— 退化成"只列用过的"
        witness.warn("_token_budget", f"history_configured:{e}"[:120])

    total_tok = sum(tok.values()) or 1
    models = []
    # 用过的按量降序；没用过的排在后面
    for m, v in sorted(tok.items(), key=lambda x: (-x[1], x[0])):
        c = _row_cost(v, m, prices)
        # 不给"任务数"：逐日桶按模型只存了 token，没存各自的任务数。
        # 今天的表有任务数是因为那走的是原始行；这里没有就不编，宁可不显示这一列。
        info = avail.get(m) or {}
        models.append({"model": m, "tokens": v,
                       "share": round(v / total_tok, 4) if v else 0.0,
                       "cost": c, "price": prices.get(m),
                       "used": v > 0,
                       "provider": info.get("provider", ""),
                       "provider_status": info.get("provider_status")})
    # 只报"用过但没配单价"的 —— 没用过的模型没产生费用，列进警告是噪声
    unpriced = [m["model"] for m in models if m["used"] and m["cost"] is None]
    total_cost = round(sum(m["cost"] for m in models if m["cost"] is not None), 6)

    active = [d for d in days if d["tokens"] > 0]
    peak = max(days, key=lambda d: d["tokens"]) if active else None
    cur_streak, long_streak = _streak(days)
    peak_hour = None
    if any(hours):
        peak_hour = max(range(_HOURS), key=lambda i: hours[i])

    return {
        "range": range_,
        "earliest": earliest,      # "全部"最多回溯 400 天，页面必须标注起始日
        "days": days,
        "models": models,
        "totals": {
            "tokens": sum(d["tokens"] for d in days),
            "tasks": sum(d["tasks"] for d in days),
            "active_days": len(active),
            "cost": total_cost,
            "unpriced_models": unpriced,
        },
        "activity": {
            "peak_day": ({"date": peak["date"], "tokens": peak["tokens"]} if peak else None),
            "peak_hour": peak_hour,          # 无数据是 None，不是 0 —— 0 点也是合法时刻
            "current_streak": cur_streak,
            "longest_streak": long_streak,
            "elapsed_s": round(elapsed_all, 1),
            "max_elapsed_s": round(max_elapsed, 1),
        },
    }
