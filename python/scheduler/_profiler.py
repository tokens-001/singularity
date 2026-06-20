"""内部模块 — 调度性能分析。

追踪每个任务的阶段耗时 (route/execute/validate/merge)，
检测性能瓶颈和卡死任务。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from . import config


@dataclass
class PerfSample:
    task_id: str
    level: str = ""
    route_ms: float = 0.0
    execute_ms: float = 0.0
    validate_ms: float = 0.0
    merge_ms: float = 0.0
    total_ms: float = 0.0
    tokens: int = 0
    ts: float = 0.0


class Profiler:
    def __init__(self, max_samples: int = 200):
        self._samples: list[PerfSample] = []
        self._max = max_samples
        self._path = config.QIDIAN_DIR / "perf_samples.json"
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._samples = [PerfSample(**s) for s in data[-self._max:]]
            except Exception as e:
                witness.heartbeat('_profiler', f'warn:{e}')

    def _save(self):
        config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
        data = [s.__dict__ for s in self._samples[-self._max:]]
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def record(self, task_id: str, level: str, route_ms: float,
               execute_ms: float, validate_ms: float, merge_ms: float,
               tokens: int = 0):
        total = route_ms + execute_ms + validate_ms + merge_ms
        s = PerfSample(task_id=task_id, level=level,
                       route_ms=route_ms, execute_ms=execute_ms,
                       validate_ms=validate_ms, merge_ms=merge_ms,
                       total_ms=total, tokens=tokens, ts=time.time())
        self._samples.append(s)
        if len(self._samples) > self._max * 2:
            self._samples = self._samples[-self._max:]
        self._save()

    def stats(self) -> dict:
        """汇总统计。"""
        if not self._samples:
            return {"count": 0}
        recent = self._samples[-100:]
        total = sum(s.total_ms for s in recent)
        avg_total = total / len(recent) if recent else 0
        levels = {}
        for s in recent:
            lv = s.level or "?"
            if lv not in levels:
                levels[lv] = {"count": 0, "avg_ms": 0, "total_ms": 0}
            levels[lv]["count"] += 1
            levels[lv]["total_ms"] += s.total_ms
        for lv in levels:
            levels[lv]["avg_ms"] = round(levels[lv]["total_ms"] / levels[lv]["count"], 1)
        phase_avg = {}
        for phase in ["route_ms", "execute_ms", "validate_ms", "merge_ms"]:
            vals = [getattr(s, phase) for s in recent if getattr(s, phase) > 0]
            phase_avg[phase] = round(sum(vals) / len(vals), 1) if vals else 0
        return {
            "count": len(recent),
            "avg_total_ms": round(avg_total, 1),
            "by_level": levels,
            "phase_avg_ms": phase_avg,
            "slowest_5": sorted(
                [{"task_id": s.task_id[-8:], "level": s.level, "total_s": round(s.total_ms/1000, 1),
                  "tokens": s.tokens} for s in recent[-20:]],
                key=lambda x: x["total_s"], reverse=True,
            )[:5],
        }


_profiler = Profiler()


def record_perf(task_id: str, level: str, route_ms: float,
                execute_ms: float, validate_ms: float, merge_ms: float,
                tokens: int = 0):
    _profiler.record(task_id, level, route_ms, execute_ms, validate_ms, merge_ms, tokens)


def get_profiler() -> Profiler:
    return _profiler


def get_perf_stats() -> dict:
    return _profiler.stats()
