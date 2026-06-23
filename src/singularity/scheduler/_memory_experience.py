"""_memory_experience.py — T1: 经验归档 + 失败模式识别 + 跨项目知识迁移.

Extracted from memory.py. Uses _memory_io for storage primitives.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from singularity.scheduler._memory_io import _MEMORY_DIR, ensure_dir, read_json, write_json

_EXPERIENCES_PATH = _MEMORY_DIR / "experiences.json"
_FAILURE_PATTERNS_PATH = _MEMORY_DIR / "failure_patterns.json"


@dataclass
class ExperienceRecord:
    """单次任务执行经验。"""
    task_id: str
    description: str
    status: str           # done / failed / rolled_back
    route_level: str      # E / E+ / D
    model: str            # 实际执行模型
    elapsed_ms: float = 0
    tokens: int = 0
    failure_mode: str = ""  # 失败模式标签
    files_changed: list[str] = field(default_factory=list)
    pattern_id: str = ""    # 匹配到的已知模式 id
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description[:200],
            "status": self.status,
            "route_level": self.route_level,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "tokens": self.tokens,
            "failure_mode": self.failure_mode,
            "files_changed": self.files_changed[:20],
            "pattern_id": self.pattern_id,
            "timestamp": self.timestamp or time.time(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceRecord":
        return cls(**{k: d.get(k, "") if k in ("description","status","route_level","model","failure_mode","pattern_id") else
                      d.get(k, []) if k == "files_changed" else d.get(k, 0)
                      for k in ["task_id","description","status","route_level","model","elapsed_ms","tokens","failure_mode","files_changed","pattern_id","timestamp"]})


def archive_experience(task_id: str, description: str, status: str, route_level: str,
                       model: str = "", elapsed_ms: float = 0, tokens: int = 0,
                       failure_mode: str = "", files_changed: list[str] = None) -> None:
    """归档单次任务执行经验。orchestrator 在任务完成后调用。"""
    rec = ExperienceRecord(
        task_id=task_id, description=description, status=status,
        route_level=route_level, model=model, elapsed_ms=elapsed_ms,
        tokens=tokens, failure_mode=failure_mode,
        files_changed=files_changed or [], timestamp=time.time(),
    )
    if status in ("failed", "rolled_back") and failure_mode:
        rec.pattern_id = _match_failure_pattern(failure_mode)
    existing = list(read_json(_EXPERIENCES_PATH) or [])
    existing.append(rec.to_dict())
    if len(existing) > 1000:
        existing = existing[-1000:]
    write_json(_EXPERIENCES_PATH, existing)


def _match_failure_pattern(failure_reason: str) -> str:
    patterns = _load_failure_patterns()
    reason_lower = failure_reason.lower()
    for p in patterns:
        for kw in p.get("keywords", []):
            if kw.lower() in reason_lower:
                p["count"] = p.get("count", 0) + 1
                p["last_seen"] = time.time()
                _save_failure_patterns(patterns)
                return p["id"]
    pid = f"fp_{len(patterns)+1:03d}"
    patterns.append({
        "id": pid, "keywords": _extract_keywords(failure_reason),
        "count": 1, "first_seen": time.time(), "last_seen": time.time(),
    })
    _save_failure_patterns(patterns)
    return pid


def _extract_keywords(text: str) -> list[str]:
    import re
    from collections import Counter
    words = re.findall(r"[a-zA-Z_]{3,}", text)
    return [w for w, _ in Counter(words).most_common(5)]


def _load_failure_patterns() -> list[dict]:
    return list(read_json(_FAILURE_PATTERNS_PATH) or [])


def _save_failure_patterns(data: list[dict]) -> None:
    write_json(_FAILURE_PATTERNS_PATH, data)


def analyze_failures(limit: int = 20) -> dict:
    """分析近期失败模式，返回频次排序的失败原因。"""
    experiences = list(read_json(_EXPERIENCES_PATH) or [])
    failed = [e for e in experiences if e.get("status") in ("failed", "rolled_back")]
    if not failed:
        return {"total_failures": 0, "patterns": [], "summary": "无近期失败记录"}
    patterns = _load_failure_patterns()
    patterns.sort(key=lambda p: -p.get("count", 0))
    recent = failed[-limit:]
    return {
        "total_failures": len(failed),
        "recent_count": len(recent),
        "top_patterns": [{
            "id": p["id"], "count": p["count"],
            "keywords": p.get("keywords", []),
            "last_seen": p.get("last_seen", 0),
        } for p in patterns[:5] if p.get("count", 0) > 0],
        "summary": f"最近 {len(recent)} 次失败, {len(patterns)} 种模式",
    }


def find_similar_across_projects(description: str, min_score: float = 0.3, limit: int = 5) -> list[dict]:
    """跨项目知识迁移：在经验库中搜索相似成功案例。"""
    experiences = list(read_json(_EXPERIENCES_PATH) or [])
    if not experiences:
        return []
    successful = [e for e in experiences if e.get("status") == "done"]
    if not successful:
        return []
    desc_lower = description.lower()
    scored = []
    for e in successful:
        edesc = (e.get("description", "") or "").lower()
        if not edesc:
            continue
        d_words = set(desc_lower.split())
        e_words = set(edesc.split())
        if not d_words or not e_words:
            continue
        jaccard = len(d_words & e_words) / len(d_words | e_words)
        if jaccard >= min_score:
            scored.append({
                "task_id": e.get("task_id", ""),
                "description": e.get("description", "")[:120],
                "model": e.get("model", ""),
                "route_level": e.get("route_level", ""),
                "elapsed_ms": e.get("elapsed_ms", 0),
                "score": round(jaccard, 3),
                "files": e.get("files_changed", [])[:5],
            })
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def get_experience_stats() -> dict:
    """获取经验库统计概览。"""
    experiences = list(read_json(_EXPERIENCES_PATH) or [])
    if not experiences:
        return {"total": 0}
    statuses = {}
    for e in experiences:
        s = e.get("status", "?")
        statuses[s] = statuses.get(s, 0) + 1
    models = {}
    for e in experiences:
        m = e.get("model", "?")
        models[m] = models.get(m, 0) + 1
    return {
        "total": len(experiences),
        "by_status": statuses,
        "by_model": dict(sorted(models.items(), key=lambda x: -x[1])[:10]),
    }
