"""_lifecycle.py — 记忆生命周期管理 (从 memory.py 提取)

hot(24h)/warm(7d)/cold(30d) 三级 + 自动清理。
通过参数注入避免对 memory.py 的循环导入。
"""

from __future__ import annotations
import time
from pathlib import Path


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


def lifecycle_stats(load_events, events_path: Path = None) -> dict:
    """记忆生命周期统计。load_events: () -> dict[str, EventNode]。"""
    events = load_events()
    tiers = {"hot": 0, "warm": 0, "cold": 0, "expired": 0}
    for tid, node in events.items():
        tiers[_get_age_tier(node.timestamp)] += 1
    total = len(events)
    result = {
        "total": total,
        **tiers,
        "hot_window_h": _HOT_WINDOW // 3600,
        "warm_window_d": _WARM_WINDOW // 86400,
        "cold_max_d": _COLD_MAX // 86400,
    }
    if events_path and events_path.exists():
        result["disk_bytes"] = events_path.stat().st_size
    return result


def prune_expired(load_events, load_edges, save_events, save_edges,
                  read_json, write_json, entity_idx_path: Path) -> int:
    """清理过期 (>30天) 事件和关联边。返回清理数。"""
    events = load_events()
    edges = load_edges()
    now = time.time()
    expired_ids = {tid for tid, node in events.items()
                   if now - node.timestamp > _COLD_MAX}
    if not expired_ids:
        return 0
    for tid in list(events.keys()):
        if tid in expired_ids:
            del events[tid]
    for edge_type in list(edges.keys()):
        edges[edge_type] = [(s, d, src) for s, d, src in edges[edge_type]
                           if s not in expired_ids and d not in expired_ids]
    save_events(events)
    save_edges(edges)
    entity_idx = read_json(entity_idx_path) or {}
    for fp in list(entity_idx.keys()):
        entity_idx[fp] = [tid for tid in entity_idx[fp] if tid not in expired_ids]
        if not entity_idx[fp]:
            del entity_idx[fp]
    write_json(entity_idx_path, entity_idx)
    return len(expired_ids)


def auto_maintain(load_events, load_edges, save_events, save_edges,
                  read_json, write_json, entity_idx_path: Path, events_path: Path = None) -> dict:
    """自动维护: 清理 + 统计。"""
    pruned = 0
    try:
        pruned = prune_expired(load_events, load_edges, save_events, save_edges,
                               read_json, write_json, entity_idx_path)
    except Exception as e:
        witness.heartbeat('_lifecycle', f'warn:{e}')
    stats = lifecycle_stats(load_events, events_path)
    stats["pruned"] = pruned
    return stats
