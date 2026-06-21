from __future__ import annotations

import json
import time
from pathlib import Path

from . import config
from . import tracker


def _heartbeat_dir() -> Path:
    d = config.QIDIAN_DIR / "heartbeats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hb_path(task_id: str, agent_level: str) -> Path:
    return _heartbeat_dir() / f"{task_id}_{agent_level}.json"


def heartbeat(task_id: str, agent_level: str, status: str = "running", detail: str = "") -> None:
    """写入心跳。异常时 status="error" + detail。"""
    p = _hb_path(task_id, agent_level)
    payload = {"task_id": task_id, "level": agent_level, "last_beat": time.time(), "status": status}
    if detail:
        payload["detail"] = detail[:2000]
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_terminal_heartbeat(p: Path, tid: str) -> bool:
    """清理终态任务的心跳文件。返回 True 表示已清理。"""
    task_file = tracker._tasks_dir() / f"{tid}.json"
    if not task_file.exists():
        try: p.unlink()
        except OSError: pass
        return True
    try:
        data = json.loads(task_file.read_text(encoding="utf-8"))
        if data.get("status") in ("done", "failed", "rolled_back"):
            try: p.unlink()
            except OSError: pass
            return True
    except (json.JSONDecodeError, OSError):
        pass
    return False


def check_stalled(timeout_seconds: float = 600) -> list[str]:
    now = time.time()
    stalled: list[str] = []
    for p in _heartbeat_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            try: p.unlink()  # 损坏的心跳文件直接清理
            except OSError: pass
            continue
        tid = data.get("task_id", "")
        if tid and _cleanup_terminal_heartbeat(p, tid):
            continue
        last = data.get("last_beat", 0)
        if now - last > timeout_seconds:
            if tid:
                stalled.append(tid)
    return stalled


def _count_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in tracker._tasks_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = data.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
    return counts


def _heartbeat_task_levels() -> dict[str, int]:
    """{level: 有心跳文件的任务数}。跳过终态/已删除任务的残留心跳。"""
    loads: dict[str, int] = {}
    for p in _heartbeat_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            try: p.unlink()  # 损坏的心跳文件直接清理
            except OSError: pass
            continue
        tid = data.get("task_id", "")
        if tid and _cleanup_terminal_heartbeat(p, tid):
            continue
        lvl = data.get("level", "?")
        loads[lvl] = loads.get(lvl, 0) + 1
    return loads


def _timing_stats() -> tuple[list[float], list[float]]:
    """返回 (pending 等待秒数列表, done 完成秒数列表)。

    done 完成时间 = updated_at - created_at。
    """
    now = time.time()
    pending_waits: list[float] = []
    done_durations: list[float] = []
    for p in tracker._tasks_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = data.get("status", "")
        created = data.get("created_at", 0)
        if st == tracker.TaskStatus.PENDING.value and created:
            pending_waits.append(now - created)
        elif st == tracker.TaskStatus.DONE.value:
            done_durations.append(data.get("updated_at", created) - created)
    return pending_waits, done_durations


def _fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec/60:.1f}min"
    return f"{sec/3600:.2f}h"


def _fmt_avg(values: list[float]) -> str:
    return _fmt_duration(sum(values) / len(values)) if values else "--"


def status(agents: dict | None = None) -> str:
    counts = _count_by_status()
    pending = counts.get(tracker.TaskStatus.PENDING.value, 0)
    failed = counts.get(tracker.TaskStatus.FAILED.value, 0)
    done = counts.get(tracker.TaskStatus.DONE.value, 0)
    loads = _heartbeat_task_levels()  # 含清理逻辑，必须先于 running 计算
    running = sum(loads.values())

    pending_waits, done_durations = _timing_stats()
    avg_wait = _fmt_avg(pending_waits)
    avg_done = _fmt_avg(done_durations)

    lines = [
        "## 奇点调度状态",
        f"- 队列中 (pending): {pending}",
        f"- 运行中 (heartbeat): {running}",
        f"- 失败 (failed): {failed}",
        f"- 已完成 (done): {done}",
        f"- 平均等待时间 (pending): {avg_wait}",
        f"- 平均完成时间 (done): {avg_done}",
        "",
        "### 各 agent level 负载",
    ]

    if agents:
        for level, cfgs in agents.items():
            model = cfgs[0].get("model", "") if cfgs else ""
            n = loads.get(level, 0)
            lines.append(f"- {level}: {n} 个任务在跑{(' (' + model + ')') if model else ''}")
    else:
        lines.append("- -- (未传 agents, 不展示 level 负载)")

    # token 统计
    token_totals = _token_stats()
    if token_totals:
        lines += ["", "### Token 消耗"]
        total_all = sum(token_totals.values())
        lines.append(f"- 总计: {_fmt_tokens(total_all)}")
        for lvl, tokens in sorted(token_totals.items()):
            lines.append(f"- {lvl}: {_fmt_tokens(tokens)}")

    return "\n".join(lines)


def _token_stats() -> dict[str, int]:
    """从 trace 文件汇总各层 token 消耗。"""
    from . import config as _cfg
    totals: dict[str, int] = {}
    for p in (_cfg.TRACE_DIR / ".." / "traces").glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tc = data.get("token_count", 0) or 0
        route = data.get("route", {}) or {}
        lvl = route.get("level", "?")
        totals[lvl] = totals.get(lvl, 0) + tc
    return totals


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)
