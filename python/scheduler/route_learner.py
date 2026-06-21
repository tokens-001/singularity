"""route_learner.py — 路由学习器 (T1)

EWMA 指标追踪 + Hedge 权重 + 冷启动 + 候选集加权。
集成到 router.py 的静态规则内，不破坏现有 RouteResult 接口。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

from . import config


@dataclass
class LearnerStats:
    """单个 (task_type × model) 的 EWMA 统计。"""
    task_type: str
    model: str
    level: str            # E / E+ / D
    success_count: int = 0
    failure_count: int = 0
    total_elapsed_ms: float = 0
    total_tokens: int = 0
    ewma_success_rate: float = 0.5   # EWMA 成功率 (α=0.2)
    ewma_quality: float = 0.5        # EWMA 质量分 (1 - failure_rate 平滑)
    hedge_weight: float = 1.0        # Hedge 算法权重
    last_updated: float = 0.0
    sample_count: int = 0

    def record(self, success: bool, elapsed_ms: float = 0, tokens: int = 0) -> None:
        """记录一次执行结果，更新 EWMA。"""
        alpha = 0.2  # EWMA 平滑系数
        outcome = 1.0 if success else 0.0
        self.ewma_success_rate = alpha * outcome + (1 - alpha) * self.ewma_success_rate
        quality = 1.0 if success else 0.0  # ponytail: 简化为二元
        self.ewma_quality = alpha * quality + (1 - alpha) * self.ewma_quality
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        self.total_elapsed_ms += elapsed_ms
        self.total_tokens += tokens
        self.sample_count += 1
        self.last_updated = time.time()
        # Hedge: 成功奖励，失败惩罚
        beta = 0.1
        self.hedge_weight *= (1 + beta) if success else (1 - beta)
        self.hedge_weight = max(0.1, min(10.0, self.hedge_weight))


@dataclass
class RouteLearner:
    """按任务类型和模型层级维护 EWMA 指标，供 router 加权选择。"""
    _stats: dict[str, LearnerStats] = field(default_factory=dict)

    def _key(self, task_type: str, model: str) -> str:
        return f"{task_type}::{model}"

    def record(self, task_type: str, model: str, level: str,
               success: bool, elapsed_ms: float = 0, tokens: int = 0) -> None:
        """任务执行完成后调用，更新模型在该任务类型下的统计。"""
        k = self._key(task_type, model)
        if k not in self._stats:
            self._stats[k] = LearnerStats(task_type=task_type, model=model, level=level)
        self._stats[k].record(success, elapsed_ms, tokens)

    def get_weight(self, task_type: str, model: str) -> float:
        """获取模型在任务类型下的 Hedge 权重。冷启动返回 1.0。"""
        k = self._key(task_type, model)
        if k in self._stats:
            return self._stats[k].hedge_weight
        return 1.0

    def get_success_rate(self, task_type: str, model: str) -> float:
        """获取 EWMA 成功率。冷启动返回 0.5。"""
        k = self._key(task_type, model)
        if k in self._stats:
            return self._stats[k].ewma_success_rate
        return 0.5

    def rank_candidates(self, task_type: str, candidates: list[str], top_n: int = 3) -> list[str]:
        """按 Hedge 权重对候选模型排序。冷启动时保持原序。"""
        if not self._stats:
            return candidates[:top_n]
        scored = [(m, self.get_weight(task_type, m)) for m in candidates]
        scored.sort(key=lambda x: -x[1])
        return [m for m, _ in scored[:top_n]]

    def get_stats(self) -> dict:
        """返回统计快照，供 CLI/API 使用。"""
        result = {}
        for k, s in self._stats.items():
            result[k] = {
                "task_type": s.task_type, "model": s.model, "level": s.level,
                "success": s.success_count, "failure": s.failure_count,
                "ewma_sr": round(s.ewma_success_rate, 3),
                "ewma_q": round(s.ewma_quality, 3),
                "hedge": round(s.hedge_weight, 3),
                "samples": s.sample_count,
                "avg_ms": round(s.total_elapsed_ms / max(1, s.sample_count)),
                "updated": s.last_updated,
            }
        return result

    def to_dict(self) -> dict:
        return {k: {"task_type": s.task_type, "model": s.model, "level": s.level,
                     "success_count": s.success_count, "failure_count": s.failure_count,
                     "ewma_success_rate": s.ewma_success_rate, "hedge_weight": s.hedge_weight,
                     "ewma_quality": s.ewma_quality, "sample_count": s.sample_count,
                     "total_elapsed_ms": s.total_elapsed_ms, "total_tokens": s.total_tokens,
                     "last_updated": s.last_updated}
                for k, s in self._stats.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "RouteLearner":
        rl = cls()
        for k, v in d.items():
            s = LearnerStats(
                task_type=v.get("task_type", ""), model=v.get("model", ""),
                level=v.get("level", ""),
                success_count=v.get("success_count", 0),
                failure_count=v.get("failure_count", 0),
                ewma_success_rate=v.get("ewma_success_rate", 0.5),
                hedge_weight=v.get("hedge_weight", 1.0),
                ewma_quality=v.get("ewma_quality", 0.5),
                sample_count=v.get("sample_count", 0),
                total_elapsed_ms=v.get("total_elapsed_ms", 0),
                total_tokens=v.get("total_tokens", 0),
                last_updated=v.get("last_updated", 0),
            )
            rl._stats[k] = s
        return rl


# ── 持久化 ──
_LEARNER_PATH = config.QIDIAN_DIR / "route_learner.json"


def load_learner() -> RouteLearner:
    """加载持久化的学习器状态。"""
    if _LEARNER_PATH.exists():
        try:
            data = json.loads(_LEARNER_PATH.read_text(encoding="utf-8"))
            return RouteLearner.from_dict(data.get("stats", {}))
        except (json.JSONDecodeError, KeyError):
            pass
    return RouteLearner()


def save_learner(learner: RouteLearner) -> None:
    """持久化学习器状态。"""
    config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    data = {"stats": learner.to_dict(), "updated_at": time.time()}
    _LEARNER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
