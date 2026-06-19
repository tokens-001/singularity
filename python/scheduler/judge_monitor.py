"""
Judge Monitor — watches the Judge for bias and anomalies.

Inspired by HyperAgents' "偏差自动检测与修正" capability:
the system autonomously detects degraded behaviors (e.g. 99% pass rate)
and flags them for human attention.

This module is purely statistical — zero LLM calls.
Data persists to .qidian/judge_monitor.json (aggregates only, not raw snapshots).
"""

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("qidian.judge_monitor")


# ═══════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════

@dataclass
class JudgeSnapshot:
    """A single judge event record."""
    timestamp: float = 0.0
    task_type: str = "default"      # bugfix / feature / refactor / test / review / default
    model: str = ""                 # which model was being judged
    judge_pass: bool = False
    judge_score: float = 0.0
    judge_failure_mode: str = ""    # tool_loop / empty_output / json_error / semantic_error / ...
    judge_uncertain: bool = False
    template_id: str = ""           # from task_templates (bugfix/refactor/feature/test/review)


@dataclass
class JudgeAnomaly:
    """Detected anomalous pattern in judge behavior."""
    kind: str          # excessive_pass_rate | low_pass_rate | model_bias
    detail: str        # human-readable description
    threshold: float   # the threshold that was exceeded
    observed: float    # the observed value
    detected_at: float = 0.0


@dataclass
class JudgeModelCorrelation:
    """Per-model judge statistics."""
    model: str = ""
    total_judged: int = 0
    passes: int = 0
    score_sum: float = 0.0
    failure_modes: dict = field(default_factory=dict)  # {mode: count}
    disagreed_retries: int = 0  # judge pass/retry outcome disagreed

    @property
    def avg_score(self) -> float:
        if self.total_judged == 0:
            return 0.0
        return self.score_sum / self.total_judged

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "total_judged": self.total_judged,
            "passes": self.passes,
            "avg_score": round(self.avg_score, 3),
            "failure_modes": dict(self.failure_modes),
            "disagreed_retries": self.disagreed_retries,
        }


# ═══════════════════════════════════════════════════
# Judge Monitor Store
# ═══════════════════════════════════════════════════

class JudgeMonitorStore:
    """Tracks judge performance: pass rates, model bias, anomalies.

    Mirrors the pattern of ProfileStore — record() + get_stats() +
    check_anomalies() + save()/load().
    """

    SNAPSHOT_MAXLEN = 1000
    ANOMALY_MAXLEN = 50
    MIN_SAMPLES_FOR_ANOMALY = 10
    MIN_SAMPLES_FOR_BIAS = 5

    def __init__(self, store_path: Path | None = None):
        self._path = store_path
        # Ring buffer — not persisted, lost on restart (diagnostic only)
        self._snapshots: deque[JudgeSnapshot] = deque(maxlen=self.SNAPSHOT_MAXLEN)
        # Aggregate stats — persisted to JSON
        self._by_task_type: dict[str, dict] = {}   # {task_type: {total, passes, score_sum}}
        self._correlations: dict[str, JudgeModelCorrelation] = {}
        self._anomalies: list[JudgeAnomaly] = []
        self._last_updated: float = 0.0

    # ── Recording ──────────────────────────────────

    def record(self, task_type: str, model: str, verdict,
               template_id: str = "") -> None:
        """Record a judge event after each execution judge call.

        Called from _judge_and_profile() in orchestrator.py.
        Caller passes the execution_judge.JudgeVerdict object.
        """
        import time
        ts = time.time()

        # Extract verdict fields (works with both JudgeVerdict and dict-like)
        try:
            pass_ = bool(getattr(verdict, 'pass_', verdict.get('pass_', True)))
            score = float(getattr(verdict, 'score', verdict.get('score', 0.0)))
            fmode = str(getattr(verdict, 'failure_mode', verdict.get('failure_mode', '')))
            uncertain = bool(getattr(verdict, 'uncertain', verdict.get('uncertain', False)))
        except Exception:
            pass_ = True
            score = 0.0
            fmode = ""
            uncertain = False

        # Ring buffer snapshot
        snap = JudgeSnapshot(
            timestamp=ts,
            task_type=task_type,
            model=model,
            judge_pass=pass_,
            judge_score=score,
            judge_failure_mode=fmode,
            judge_uncertain=uncertain,
            template_id=template_id,
        )
        self._snapshots.append(snap)

        # Aggregate by task_type
        if task_type not in self._by_task_type:
            self._by_task_type[task_type] = {"total": 0, "passes": 0, "score_sum": 0.0}
        self._by_task_type[task_type]["total"] += 1
        self._by_task_type[task_type]["score_sum"] += score
        if pass_:
            self._by_task_type[task_type]["passes"] += 1

        # Aggregate by model
        if model not in self._correlations:
            self._correlations[model] = JudgeModelCorrelation(model=model)
        corr = self._correlations[model]
        corr.total_judged += 1
        corr.score_sum += score
        if pass_:
            corr.passes += 1
        if fmode:
            corr.failure_modes[fmode] = corr.failure_modes.get(fmode, 0) + 1

        self._last_updated = ts

        # Check anomalies periodically (every 10 records)
        if sum(d["total"] for d in self._by_task_type.values()) % 10 == 0:
            self.check_anomalies()

    # ── Statistics ─────────────────────────────────

    def get_stats(self) -> dict:
        """Return all stats for API consumption."""
        # Pass rates by task type
        pass_rates = {}
        for tt, d in sorted(self._by_task_type.items()):
            total = d["total"]
            if total > 0:
                rate = d["passes"] / total
            else:
                rate = 0.0
            pass_rates[tt] = {
                "total": total,
                "passes": d["passes"],
                "rate": round(rate, 3),
                "avg_score": round(d["score_sum"] / max(total, 1), 3),
            }

        # Model correlations
        model_corrs = {}
        for model, corr in sorted(self._correlations.items()):
            bias_flag = (
                corr.total_judged >= self.MIN_SAMPLES_FOR_BIAS
                and corr.avg_score < 0.3
            )
            model_corrs[model] = {
                **corr.to_dict(),
                "bias_flag": bias_flag,
            }

        # Score distribution (histogram bins)
        hist = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for snap in self._snapshots:
            s = snap.judge_score
            if s < 0.2:
                hist["0.0-0.2"] += 1
            elif s < 0.4:
                hist["0.2-0.4"] += 1
            elif s < 0.6:
                hist["0.4-0.6"] += 1
            elif s < 0.8:
                hist["0.6-0.8"] += 1
            else:
                hist["0.8-1.0"] += 1

        # Disagreement rate
        total = sum(c.total_judged for c in self._correlations.values())
        disagreed = sum(c.disagreed_retries for c in self._correlations.values())
        disagreement_rate = disagreed / max(total, 1)

        return {
            "pass_rates_by_type": pass_rates,
            "model_correlations": model_corrs,
            "anomalies": [self._anomaly_to_dict(a) for a in self._anomalies[-10:]],
            "score_distribution": hist,
            "disagreement_rate": round(disagreement_rate, 3),
            "total_judgments": total,
            "last_updated": self._last_updated,
        }

    # ── Anomaly Detection ──────────────────────────

    def check_anomalies(self) -> list[JudgeAnomaly]:
        """Run anomaly detection rules.

        Rules:
        1. pass_rate > 95% for a task_type with >= 10 samples → excessive_pass_rate
        2. pass_rate < 5% for a task_type with >= 10 samples → low_pass_rate
        3. model avg_score < 0.3 with >= 5 samples → model_bias
        """
        import time
        ts = time.time()
        new_anomalies = []

        # Rule 1 & 2: per-task-type pass rate anomalies
        for tt, d in self._by_task_type.items():
            total = d["total"]
            if total < self.MIN_SAMPLES_FOR_ANOMALY:
                continue
            rate = d["passes"] / total

            if rate > 0.95:
                existing = [a for a in self._anomalies
                            if a.kind == "excessive_pass_rate" and tt in a.detail]
                if not existing:
                    a = JudgeAnomaly(
                        kind="excessive_pass_rate",
                        detail=f"{tt}: pass rate {rate:.1%} over {total} judgments",
                        threshold=0.95,
                        observed=rate,
                        detected_at=ts,
                    )
                    self._anomalies.append(a)
                    new_anomalies.append(a)

            elif rate < 0.05 and total >= self.MIN_SAMPLES_FOR_ANOMALY:
                existing = [a for a in self._anomalies
                            if a.kind == "low_pass_rate" and tt in a.detail]
                if not existing:
                    a = JudgeAnomaly(
                        kind="low_pass_rate",
                        detail=f"{tt}: pass rate {rate:.1%} over {total} judgments",
                        threshold=0.05,
                        observed=rate,
                        detected_at=ts,
                    )
                    self._anomalies.append(a)
                    new_anomalies.append(a)

        # Rule 3: model bias
        for model, corr in self._correlations.items():
            if corr.total_judged < self.MIN_SAMPLES_FOR_BIAS:
                continue
            if corr.avg_score < 0.3:
                existing = [a for a in self._anomalies
                            if a.kind == "model_bias" and model in a.detail]
                if not existing:
                    a = JudgeAnomaly(
                        kind="model_bias",
                        detail=f"{model}: avg score {corr.avg_score:.2f} over {corr.total_judged} judgments",
                        threshold=0.3,
                        observed=corr.avg_score,
                        detected_at=ts,
                    )
                    self._anomalies.append(a)
                    new_anomalies.append(a)

        # Cap anomaly list
        if len(self._anomalies) > self.ANOMALY_MAXLEN:
            self._anomalies = self._anomalies[-self.ANOMALY_MAXLEN:]

        return new_anomalies

    def track_disagreement(self, model: str) -> None:
        """Call when retry with different model contradicts original judge verdict."""
        if model in self._correlations:
            self._correlations[model].disagreed_retries += 1

    def _anomaly_to_dict(self, a: JudgeAnomaly) -> dict:
        return {
            "kind": a.kind,
            "detail": a.detail,
            "threshold": a.threshold,
            "observed": round(a.observed, 3),
            "detected_at": a.detected_at,
        }

    # ── Persistence ────────────────────────────────

    def save(self, path: Path | None = None) -> None:
        """Persist aggregate stats to JSON (not raw snapshots)."""
        target = path or self._path
        if not target:
            return
        try:
            data = {
                "version": 1,
                "by_task_type": self._by_task_type,
                "correlations": {
                    m: c.to_dict() for m, c in self._correlations.items()
                },
                "anomalies": [self._anomaly_to_dict(a) for a in self._anomalies],
                "last_updated": self._last_updated,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            _log.warning("Failed to save judge monitor", exc_info=True)

    def load(self, path: Path | None = None) -> "JudgeMonitorStore":
        """Load aggregate stats from JSON."""
        target = path or self._path
        if not target or not target.exists():
            return self
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._by_task_type = data.get("by_task_type", {})
            self._anomalies = [
                JudgeAnomaly(
                    kind=a["kind"],
                    detail=a["detail"],
                    threshold=a["threshold"],
                    observed=a["observed"],
                    detected_at=a.get("detected_at", 0.0),
                )
                for a in data.get("anomalies", [])
            ]
            self._last_updated = data.get("last_updated", 0.0)
            # Rebuild correlations from dict
            self._correlations = {}
            for model, cd in data.get("correlations", {}).items():
                c = JudgeModelCorrelation(
                    model=model,
                    total_judged=cd.get("total_judged", 0),
                    passes=cd.get("passes", 0),
                    score_sum=cd.get("avg_score", 0.0) * cd.get("total_judged", 0),
                    failure_modes=cd.get("failure_modes", {}),
                    disagreed_retries=cd.get("disagreed_retries", 0),
                )
                self._correlations[model] = c
        except Exception:
            _log.warning("Failed to load judge monitor", exc_info=True)
        return self
