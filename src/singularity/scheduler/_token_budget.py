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


def _row_cost(tokens: int, model: str, prices: dict[str, float]) -> float | None:
    """一行用量的费用 (USD)。**该模型没配单价就返回 None，绝不兜底。**

    返回 None 而不是 0.0 是刻意的：0.0 会被前端渲染成一个看着可信的 $0.00，
    那正是本次要消灭的"编造金额"。调用方必须把 None 如实显示成"未配置价格"。
    """
    p = prices.get(model)
    return None if p is None else round(tokens / 1_000_000 * p, 6)


class TokenBudget:
    """全局 token 预算管理器。"""

    def __init__(self):
        self._path = config.QIDIAN_DIR / "token_usage.json"
        self._daily: list[UsageRecord] = []
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
               model: str, level: str, tokens: int):
        # 写盘时**不算钱**。费用一律在读时用真实单价现算（见 _row_cost）——
        # 这样用户填上单价的瞬间，之前记的所有账也自动变对，不需要任何数据迁移。
        rec = UsageRecord(
            project_id=project_id, project_name=project_name,
            task_id=task_id, model=model, level=level,
            tokens=tokens, ts=time.time(),
        )
        with _LOCK:
            self._daily.append(rec)
            if len(self._daily) > 500:
                self._daily = self._daily[-500:]
            self._save()

    def set_budget(self, daily: float = 0.0, monthly: float = 0.0):
        self._budget_daily = daily
        self._budget_monthly = monthly
        self._save()

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
                  model: str = "", level: str = "", tokens: int = 0):
    """记录一次 token 消耗。调度循环在 dispatch 完成后调用。"""
    if tokens > 0:
        _budget.record(project_id, project_name, task_id, model, level, tokens)


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
