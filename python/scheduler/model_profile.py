"""model_profile.py — 模型画像模块

记录每个模型的历史表现：
- 哪种任务类型成功率最高
- 平均耗时、token 消耗
- 常见失败模式（tool-loop / 空输出 / JSON 错误）
- 熔断状态（连续失败自动暂停）

路由时参考画像，避免把任务发给对它不擅长的模型。
"""
from __future__ import annotations


import json
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import defaultdict

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════

@dataclass
class ModelStats:
    """单个模型的统计数据。"""
    model: str
    task_type: str
    total_attempts: int = 0
    successes: int = 0
    total_elapsed: float = 0.0
    total_tokens: int = 0
    failure_modes: dict = field(default_factory=dict)  # {mode: count}
    elo: float = 1500.0  # 初始 Elo = 1500
    last_updated: float = 0.0
    # 模式画像：按 (task_type, template_id) 追踪
    template_stats: dict = field(default_factory=dict)  # {template_id: {attempts, successes, total_tokens}}

    @property
    def success_rate(self) -> float:
        """贝叶斯平滑成功率：实际成功率 = (成功数 + 伪计数) / (总数 + 2×伪计数)。"""
        prior = 2  # 伪计数，避免小样本偏差
        if self.total_attempts == 0:
            return 0.5
        return (self.successes + prior) / (self.total_attempts + 2 * prior)

    @property
    def avg_elapsed(self) -> float:
        if self.total_attempts == 0:
            return 0.0
        return self.total_elapsed / self.total_attempts

    @property
    def top_failure_mode(self) -> str:
        if not self.failure_modes:
            return "unknown"
        return max(self.failure_modes, key=self.failure_modes.get)

    def to_dict(self) -> dict:
        return {
            "model": self.model, "task_type": self.task_type,
            "total": self.total_attempts, "successes": self.successes,
            "success_rate": round(self.success_rate, 3),
            "avg_elapsed": round(self.avg_elapsed, 1),
            "total_tokens": self.total_tokens,
            "elo": round(self.elo, 1),
            "failure_modes": self.failure_modes,
            "template_stats": dict(self.template_stats),
        }


@dataclass
class CircuitBreaker:
    """熔断器：连续失败 N 次后暂停该模型。"""
    model: str
    max_failures: int = 3
    cooldown_seconds: float = 300.0  # 冷却 5 分钟
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False  # True = 熔断中

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if self.consecutive_failures >= self.max_failures:
            self.is_open = True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.is_open = False

    def check_cooldown(self) -> bool:
        """检查是否已冷却完毕，可以尝试恢复。"""
        if not self.is_open:
            return True
        if time.time() - self.last_failure_time >= self.cooldown_seconds:
            self.is_open = False  # 半开状态，允许一次尝试
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "model": self.model, "open": self.is_open,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_remaining": max(0, self.cooldown_seconds - (time.time() - self.last_failure_time)),
        }


# ═══════════════════════════════════════════════
# Profile Store
# ═══════════════════════════════════════════════

class ProfileStore:
    """模型画像存储：按 (model, task_type) 索引。

    支持：
    - record(): 记录一次执行结果
    - update_elo(): 更新 Elo 评分（赢者加分、输者减分）
    - rank(): 返回某任务类型下模型排名
    - get_circuit_breaker(): 获取熔断器
    - save() / load(): JSON 持久化
    """

    K_FACTOR = 32  # Elo 更新系数

    def __init__(self, store_path: Optional[Path] = None):
        self._stats: dict[tuple, ModelStats] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._path = store_path

    # ── 记录 ──
    def record(self, model: str, task_type: str, success: bool,
               elapsed: float = 0.0, tokens: int = 0,
               failure_mode: str = "",
               template_id: str = "", max_turns: int = 0) -> None:
        key = (model, task_type)
        if key not in self._stats:
            self._stats[key] = ModelStats(model=model, task_type=task_type)
        s = self._stats[key]
        s.total_attempts += 1
        s.total_elapsed += elapsed
        s.total_tokens += tokens
        s.last_updated = time.time()

        # 模式画像：按 template_id 追踪
        if template_id:
            ts = s.template_stats
            if template_id not in ts:
                ts[template_id] = {"attempts": 0, "successes": 0, "total_tokens": 0}
            ts[template_id]["attempts"] += 1
            ts[template_id]["total_tokens"] += tokens
            if success:
                ts[template_id]["successes"] += 1

        if success:
            s.successes += 1
            self._get_breaker(model).record_success()
        else:
            if failure_mode:
                s.failure_modes[failure_mode] = s.failure_modes.get(failure_mode, 0) + 1
            self._get_breaker(model).record_failure()

    # ── Elo ──
    def update_elo(self, winner_model: str, loser_model: str, task_type: str) -> None:
        """Pairwise Elo 更新。"""
        wk = (winner_model, task_type)
        lk = (loser_model, task_type)
        if wk not in self._stats:
            self._stats[wk] = ModelStats(model=winner_model, task_type=task_type)
        if lk not in self._stats:
            self._stats[lk] = ModelStats(model=loser_model, task_type=task_type)

        wa = self._stats[wk]
        la = self._stats[lk]

        expected_w = 1.0 / (1.0 + 10 ** ((la.elo - wa.elo) / 400.0))
        expected_l = 1.0 / (1.0 + 10 ** ((wa.elo - la.elo) / 400.0))

        wa.elo = wa.elo + self.K_FACTOR * (1.0 - expected_w)
        la.elo = la.elo + self.K_FACTOR * (0.0 - expected_l)
        wa.last_updated = time.time()
        la.last_updated = time.time()

    # ── 查询 ──
    def rank(self, task_type: str, exclude_models: list[str] = None) -> list[ModelStats]:
        """返回某任务类型下模型排名（排除熔断模型）。"""
        exclude = set(exclude_models or [])
        candidates = []
        for (model, tt), s in self._stats.items():
            if tt != task_type:
                continue
            if model in exclude:
                continue
            breaker = self._breakers.get(model)
            if breaker and breaker.is_open and not breaker.check_cooldown():
                continue  # 熔断中
            candidates.append(s)
        # 按先成功率后 Elo 排序
        candidates.sort(key=lambda s: (s.success_rate, s.elo), reverse=True)
        return candidates

    def rank_by_pattern(self, task_type: str, template_id: str,
                        exclude_models: list[str] = None) -> list[dict]:
        """返回按 (task_type, template_id) 模式画像排序的模型列表。

        优先按 pattern-specific 成功率排序，
        如果某个模型在该 pattern 下样本数不足 3，回退到 task_type 整体成功率。

        Returns list of {model, success_rate, attempts, avg_tokens}.
        """
        MIN_PATTERN_SAMPLES = 3
        exclude = set(exclude_models or [])
        prior = 2  # 贝叶斯平滑伪计数

        results = []
        for (model, tt), s in self._stats.items():
            if tt != task_type:
                continue
            if model in exclude:
                continue
            breaker = self._breakers.get(model)
            if breaker and breaker.is_open and not breaker.check_cooldown():
                continue

            # 尝试从 pattern 统计计算成功率
            pat = s.template_stats.get(template_id)
            if pat and pat["attempts"] >= MIN_PATTERN_SAMPLES:
                rate = (pat["successes"] + prior) / (pat["attempts"] + 2 * prior)
                attempts = pat["attempts"]
                avg_tokens = pat["total_tokens"] / max(pat["attempts"], 1)
            else:
                # 回退到 task_type 整体成功率
                rate = s.success_rate
                attempts = s.total_attempts
                avg_tokens = s.total_tokens / max(s.total_attempts, 1)

            results.append({
                "model": model,
                "success_rate": round(rate, 3),
                "attempts": attempts,
                "avg_tokens": round(avg_tokens, 1),
                "elo": round(s.elo, 1),
                "from_pattern": bool(pat and pat["attempts"] >= MIN_PATTERN_SAMPLES),
            })

        results.sort(key=lambda r: (r["success_rate"], r["elo"]), reverse=True)
        return results

    def get(self, model: str, task_type: str) -> ModelStats:
        key = (model, task_type)
        if key not in self._stats:
            self._stats[key] = ModelStats(model=model, task_type=task_type)
        return self._stats[key]

    def _get_breaker(self, model: str) -> CircuitBreaker:
        if model not in self._breakers:
            self._breakers[model] = CircuitBreaker(model=model)
        return self._breakers[model]

    def get_circuit_breaker(self, model: str) -> CircuitBreaker:
        return self._get_breaker(model)

    # ── 持久化 ──
    def save(self, path: Optional[Path] = None) -> None:
        p = Path(path) if isinstance(path, str) else (path or self._path)
        if not p:
            return
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "stats": [s.to_dict() for s in self._stats.values()],
            "breakers": [b.to_dict() for b in self._breakers.values()],
        }
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def load(self, path: Optional[Path] = None) -> "ProfileStore":
        p = Path(path) if isinstance(path, str) else (path or self._path)
        if not p:
            return self
        p = Path(p)
        if not p.exists():
            return self
        try:
            data = json.loads(p.read_text())
            for sd in data.get("stats", []):
                key = (sd["model"], sd["task_type"])
                s = ModelStats(
                    model=sd["model"], task_type=sd["task_type"],
                    total_attempts=sd.get("total", 0),
                    successes=sd.get("successes", 0),
                    total_elapsed=sd.get("avg_elapsed", 0) * sd.get("total", 1),
                    total_tokens=sd.get("total_tokens", 0),
                    elo=sd.get("elo", 1500.0),
                    failure_modes=sd.get("failure_modes", {}),
                    template_stats=sd.get("template_stats", {}),
                )
                self._stats[key] = s
            for bd in data.get("breakers", []):
                cb = CircuitBreaker(
                    model=bd["model"],
                    consecutive_failures=bd.get("consecutive_failures", 0),
                    is_open=bd.get("open", False),
                )
                self._breakers[bd["model"]] = cb
        except (json.JSONDecodeError, OSError) as e:
            _log.warning(f"ProfileStore load failed: {e}")
        return self


    # ── API 查询 ──
    def summary(self) -> dict[str, dict]:
        """返回所有模型画像摘要，格式: {model/task_type: {success_rate, total, ...}}。"""
        result = {}
        for (model, tt), s in self._stats.items():
            key = f"{model}/{tt}"
            result[key] = {
                "model": model,
                "task_type": tt,
                "success_rate": round(s.success_rate, 3),
                "total": s.total_attempts,
                "successes": s.successes,
                "elo": round(s.elo, 1),
                "avg_elapsed": round(s.total_elapsed / max(s.total_attempts, 1), 2),
                "total_tokens": s.total_tokens,
                "failure_modes": dict(s.failure_modes),
            }
        return result

    def pattern_summary(self) -> list[dict]:
        """返回所有模型在所有模式下的排名摘要。"""
        result = []
        task_types = {tt for (_, tt) in self._stats}
        for tt in sorted(task_types):
            ranked = self.rank(tt)
            for s in ranked[:5]:
                result.append({
                    "model": s.model,
                    "task_type": tt,
                    "success_rate": round(s.success_rate, 3),
                    "total": s.total_attempts,
                    "elo": round(s.elo, 1),
                })
        return result


# ═══════════════════════════════════════════════
# 时间衰减
# ═══════════════════════════════════════════════

def apply_time_decay(stats: ModelStats, current_time: float = None,
                     half_life_days: float = 30.0) -> ModelStats:
    """应用时间衰减：30 天前的数据权重降 50%。"""
    if current_time is None:
        current_time = time.time()
    age_days = (current_time - stats.last_updated) / 86400.0 if stats.last_updated else 0
    if age_days <= 0:
        return stats
    decay = 0.5 ** (age_days / half_life_days)
    stats.total_attempts = max(1, int(stats.total_attempts * decay))
    stats.successes = int(stats.successes * decay)
    return stats
