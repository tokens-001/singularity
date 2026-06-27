"""内部模块 — Token 消耗追踪 & 预算管控。

实时追踪每个项目/每天的 token 消耗，支持预算上限和自动降级建议。
持久化: .qidian/token_usage.json
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from singularity.scheduler import config, witness


@dataclass
class UsageRecord:
    project_id: str = ""
    project_name: str = ""
    task_id: str = ""
    model: str = ""
    level: str = ""
    tokens: int = 0
    cost_est: float = 0.0  # 预估费用 (USD)
    ts: float = 0.0


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
                witness.heartbeat('_token_budget', f'warn:{e}')

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "daily": [r.__dict__ for r in self._daily[-500:]],
            "budget_daily": self._budget_daily,
            "budget_monthly": self._budget_monthly,
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def record(self, project_id: str, project_name: str, task_id: str,
               model: str, level: str, tokens: int):
        cost = self._estimate_cost(model, tokens)
        rec = UsageRecord(
            project_id=project_id, project_name=project_name,
            task_id=task_id, model=model, level=level,
            tokens=tokens, cost_est=cost, ts=time.time(),
        )
        self._daily.append(rec)
        if len(self._daily) > 500:
            self._daily = self._daily[-500:]
        self._save()

    def _estimate_cost(self, model: str, tokens: int) -> float:
        """按模型估算费用 (USD)。"""
        rates = {
            "deepseek-chat": 0.14, "deepseek-reasoner": 0.55,
            "glm-5-turbo": 0.30, "glm-5.2": 0.60,
            "gpt-5.5": 3.75, "gpt-5.5-pro": 10.0,
            "claude-opus-4-8": 15.0, "kimi-k2.7-code": 0.40,
        }
        rate = rates.get(model, 0.50)
        return round(tokens / 1_000_000 * rate, 6)

    def set_budget(self, daily: float = 0.0, monthly: float = 0.0):
        self._budget_daily = daily
        self._budget_monthly = monthly
        self._save()

    @property
    def daily_total(self) -> int:
        today = time.strftime("%Y-%m-%d")
        return sum(r.tokens for r in self._daily
                   if time.strftime("%Y-%m-%d", time.localtime(r.ts)) == today)

    @property
    def daily_cost(self) -> float:
        today = time.strftime("%Y-%m-%d")
        return round(sum(r.cost_est for r in self._daily
                         if time.strftime("%Y-%m-%d", time.localtime(r.ts)) == today), 4)

    @property
    def budget_warning(self) -> str:
        """预算告警: 空字符串=正常, 否则为告警消息。"""
        if self._budget_daily > 0:
            pct = self.daily_cost / self._budget_daily
            if pct > 0.9:
                return f"日预算已用 {pct*100:.0f}% (${self.daily_cost:.2f}/${self._budget_daily:.2f})，建议暂停D层任务"
            if pct > 0.7:
                return f"日预算已用 {pct*100:.0f}%，建议优先使用E层模型"
        return ""

    def per_project_usage(self) -> list[dict]:
        """按项目汇总 token 用量。"""
        today = time.strftime("%Y-%m-%d")
        by_project: dict[str, dict] = {}
        for r in self._daily:
            if time.strftime("%Y-%m-%d", time.localtime(r.ts)) != today:
                continue
            pid = r.project_id or "_unknown"
            if pid not in by_project:
                by_project[pid] = {"project_id": pid, "project_name": r.project_name,
                                   "tokens": 0, "cost": 0.0, "tasks": 0}
            by_project[pid]["tokens"] += r.tokens
            by_project[pid]["cost"] += r.cost_est
            by_project[pid]["tasks"] += 1
        return sorted(by_project.values(), key=lambda x: x["tokens"], reverse=True)

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


def get_budget() -> TokenBudget:
    return _budget


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
    }
