from singularity.scheduler._memory_core import *  # noqa: F401,F403
from singularity.scheduler import config as sched_config
from singularity.scheduler import witness
from singularity.scheduler._types import _pending_sse_events
import json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict

__all__ = ['_COLD_MAX', '_HOT_WINDOW', '_INSIGHTS_PATH', '_WARM_WINDOW', '_get_age_tier', '_load_insights', '_save_insights', 'auto_maintain', 'get_insights', 'lifecycle_stats', 'prune_expired', 'rebuild_from_traces', 'stats', 'system2_extract']
# 维护
# ═══════════════════════════════════════════════════════════

def stats() -> dict:
    """各图统计。"""
    events = _load_events()
    edges = _load_edges()
    entity_idx: dict[str, list[str]] = _read_json(_ENTITY_IDX_PATH) or {}

    explicit_causal = sum(1 for _, _, s in edges.get("causal", []) if s == "explicit")
    inferred_causal = sum(1 for _, _, s in edges.get("causal", []) if s != "explicit")

    return {
        "events": len(events),
        "edges_semantic": len(edges.get("semantic", [])),
        "edges_temporal": len(edges.get("temporal", [])),
        "edges_causal_explicit": explicit_causal,
        "edges_causal_inferred": inferred_causal,
        "edges_entity": len(edges.get("entity", [])),
        "entity_files": len(entity_idx),
        "latent_candidates": len(find_candidate_latent_edges()),
    }


def rebuild_from_traces() -> int:
    """从 traces/ 重建全部索引 (清空重跑快通道)。"""
    from . import tracker as tracker_mod

    _ensure_dir()
    for path in [_EVENTS_PATH, _EDGES_PATH, _ENTITY_IDX_PATH]:
        path.write_text("{}", encoding="utf-8")

    count = 0
    trace_dir = sched_config.TRACE_DIR
    if not trace_dir.exists():
        return 0

    for trace_path in sorted(trace_dir.glob("*.json")):
        try:
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        task_id = trace_path.stem
        description = trace_data.get("task", "")
        changed_files = trace_data.get("changed_files", [])

        task_data = tracker_mod._read(task_id)
        depends_on = task_data.depends_on if task_data else []
        created_at = task_data.created_at if task_data else None

        index_task(
            task_id=task_id, description=description,
            changed_files=changed_files, depends_on=depends_on,
            created_at=created_at,
        )
        count += 1

    return count


# ── 记忆生命周期 (hot → warm → cold) ──────────────────────

# ── 记忆生命周期 (hot → warm → cold, ex _lifecycle.py) ──

_HOT_WINDOW = 24 * 3600       # 24小时
_WARM_WINDOW = 7 * 86400       # 7天
_COLD_MAX = 30 * 86400         # 30天


def _get_age_tier(timestamp: float) -> str:
    age = time.time() - timestamp
    if age < _HOT_WINDOW:
        return "hot"
    elif age < _WARM_WINDOW:
        return "warm"
    elif age < _COLD_MAX:
        return "cold"
    return "expired"


def lifecycle_stats() -> dict:
    """记忆生命周期统计。"""
    events = _load_events()
    tiers = {"hot": 0, "warm": 0, "cold": 0, "expired": 0}
    for _tid, node in events.items():
        tiers[_get_age_tier(node.timestamp)] += 1
    total = len(events)
    result = {
        "total": total, **tiers,
        "hot_window_h": _HOT_WINDOW // 3600,
        "warm_window_d": _WARM_WINDOW // 86400,
        "cold_max_d": _COLD_MAX // 86400,
    }
    if _EVENTS_PATH.exists():
        result["disk_bytes"] = _EVENTS_PATH.stat().st_size
    return result


def prune_expired() -> int:
    """清理过期 (>30天) 事件和关联边。返回清理数。"""
    events = _load_events()
    edges = _load_edges()
    now = time.time()
    expired_ids = {tid for tid, node in events.items()
                   if now - node.timestamp > _COLD_MAX}
    if not expired_ids:
        return 0
    for tid in list(events.keys()):
        if tid in expired_ids:
            del events[tid]
    for edge_type in list(edges.keys()):
        # ponytail: temporal/entity edges are 2-tuples, causal/experience are 3-tuples
        filtered = []
        for e in edges[edge_type]:
            if len(e) == 3:
                s, d, src = e
                if s not in expired_ids and d not in expired_ids:
                    filtered.append(e)
            else:
                s, d = e[0], e[1]
                if s not in expired_ids and d not in expired_ids:
                    filtered.append(e)
        edges[edge_type] = filtered
    _save_events(events)
    _save_edges(edges)
    entity_idx = _read_json(_ENTITY_IDX_PATH) or {}
    for fp in list(entity_idx.keys()):
        entity_idx[fp] = [tid for tid in entity_idx[fp] if tid not in expired_ids]
        if not entity_idx[fp]:
            del entity_idx[fp]
    _write_json(_ENTITY_IDX_PATH, entity_idx)
    return len(expired_ids)


def auto_maintain() -> dict:
    """自动维护: 清理 + 统计。"""
    pruned = 0
    try:
        pruned = prune_expired()
    except Exception as e:
        witness.heartbeat('memory', f'warn:lifecycle:{e}')
    stats = lifecycle_stats()
    stats["pruned"] = pruned
    return stats


# ═══════════════════════════════════════════════════════════
# DCPM System 2 — 夜间异步模式提取 (DCPM 2026 论文)
# ═══════════════════════════════════════════════════════════
# System 1 (现有): embedding 粗筛 + 单对 LLM 精判
# System 2 (新增): 空闲时异步批处理，提取跨任务模式
# ponytail: 分组统计 + 简单启发式，不做 LLM 批处理

_INSIGHTS_PATH = _MEMORY_DIR / "insights.json"


def system2_extract() -> dict:
    """DCPM System 2: 空闲时异步提取跨任务模式。

    分组维度: 任务类型 × 状态 × 层级
    提取:
      1. 成功模式 — 哪些模型/策略在特定任务类型上成功率高
      2. 失败模式 — 哪些失败模式反复出现
      3. 有效策略 — 从成功任务中提取共性

    返回: {"insights": [...], "added": N}
    ponytail: 纯统计分析，不调 LLM。需要时加 LLM 模式提取。
    """
    events = _load_events()
    if len(events) < 10:
        return {"insights": [], "added": 0, "reason": "insufficient_data"}

    # 分组
    successes: dict[str, list] = defaultdict(list)  # (type×level) → events
    failures: dict[str, list] = defaultdict(list)

    for tid, ev in events.items():
        if not isinstance(ev, dict):
            continue
        status = ev.get("attrs", {}).get("status", "") if isinstance(ev.get("attrs"), dict) else ""
        task_type = ev.get("attrs", {}).get("route_type", "default") if isinstance(ev.get("attrs"), dict) else "default"
        level = ev.get("attrs", {}).get("route_level", "any") if isinstance(ev.get("attrs"), dict) else "any"
        key = f"{task_type}×{level}"
        if status in ("done", "pass", "merged"):
            successes[key].append(tid)
        elif status in ("failed", "blocked"):
            failures[key].append(tid)

    insights = []
    # 1. 成功率统计（按分组）
    for key in set(list(successes.keys()) + list(failures.keys())):
        s_count = len(successes.get(key, []))
        f_count = len(failures.get(key, []))
        total = s_count + f_count
        if total >= 3:
            rate = s_count / total
            if rate >= 0.8:
                insights.append({
                    "type": "high_success_pattern",
                    "group": key,
                    "success_rate": round(rate, 2),
                    "total": total,
                    "summary": f"{key} 任务成功率 {rate:.0%} ({s_count}/{total})",
                })
            elif rate <= 0.3 and total >= 3:
                insights.append({
                    "type": "failure_hotspot",
                    "group": key,
                    "success_rate": round(rate, 2),
                    "total": total,
                    "summary": f"⚠️ {key} 任务失败率 {1-rate:.0%} ({f_count}/{total})，建议升级路由",
                })

    # 2. 去重：只保留与已有 insights 不同的
    existing = _load_insights()
    existing_summaries = {i.get("summary", "") for i in existing}
    new_insights = [i for i in insights if i["summary"] not in existing_summaries]

    if new_insights:
        _save_insights(existing + new_insights)

    return {"insights": new_insights, "added": len(new_insights),
            "groups_analyzed": len(successes) + len(failures)}


def _load_insights() -> list[dict]:
    return list(_read_json(_INSIGHTS_PATH) or [])


def _save_insights(data: list[dict]) -> None:
    _write_json(_INSIGHTS_PATH, data)


def get_insights(limit: int = 10) -> list[dict]:
    """获取最近的 System 2 洞察。"""
    all_insights = _load_insights()
    return all_insights[-limit:]


# ═══════════════════════════════════════════════════════════
