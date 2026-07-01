from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from singularity.scheduler import config

# 修复 P1-4: tracker 实际被两类线程并发写——后台 loop 线程 (orchestrator) 与
# Flask 请求线程 (_api 的 hold/retry/override/cancel)。transition/cas/ready_tasks
# 都是 read→modify→write 非原子, 裸跑会 lost-update。
# 一把可重入锁串行化所有 read-modify-write 区段, 配合 _write 的 os.replace 原子落盘,
# 即可消除竞态。RLock 允许 maybe_complete_parent→transition 这类同线程重入。
_LOCK = threading.RLock()


class TaskStatus(Enum):
    PENDING = "pending"
    ROUTED = "routed"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    # v3 并行调度新增
    DECOMPOSED = "decomposed"        # 子任务已入队, 等聚合
    BLOCKED = "blocked"              # 依赖未满足, 等前置 DONE
    CONFLICT_HELD = "conflict_held"  # merge 冲突, parking 等人


# 三个新状态都不进 _INFLIGHT (recover 不该重启它们):
#   BLOCKED 等依赖 / DECOMPOSED 等子任务 / CONFLICT_HELD 等人 —— 都不是"崩了要重跑"
# 三个新状态都不进 _TERMINAL (都还能流转出去):
#   BLOCKED→ROUTED / DECOMPOSED→DONE|FAILED / CONFLICT_HELD→DONE|FAILED
# 故两个集合维持原样, 仅 Enum 扩展。
_INFLIGHT = {TaskStatus.ROUTED, TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.VALIDATING}
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK}

# ready_tasks 扫描的状态: 等待调度的入口态
_SCHEDULABLE = {TaskStatus.PENDING, TaskStatus.ROUTED, TaskStatus.BLOCKED}


@dataclass
class Task:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    depends_on: list[str] = field(default_factory=list)
    route_level: str = "any"  # 两档后统一 "any" (E/E+/D 已废弃)
    route_gate: bool = False
    route_type: str = "default"
    snapshot_id: str = ""
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = 0.0
    updated_at: float = 0.0
    starvation_score: float = 0.0  # (now-created_at)/3600*(1+priority), 越大越饿
    children: list[str] = field(default_factory=list)  # 子任务 id 列表 (DAG 分解)
    depth: int = 0  # 分解深度, 防无限递归
    route_locked: bool = False  # planner 已指定层级 → 跳过 re-route (建议 #6)
    held: bool = False           # 人工扣留, 不进调度队列
    held_reason: str = ""        # 扣留原因
    project_id: str = ""         # 所属项目 ID (空=独立任务)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["status"] = TaskStatus(d.get("status", "pending"))
        # 旧数据兼容: 新字段缺失时用默认值
        d.setdefault("starvation_score", 0.0)
        d.setdefault("children", [])
        d.setdefault("depth", 0)
        d.setdefault("route_locked", False)
        d.setdefault("held", False)
        d.setdefault("held_reason", "")
        d.setdefault("project_id", "")
        return cls(**d)

    def compute_starvation(self) -> float:
        """刷新 starvation_score = (now-created_at)/3600 * (1+priority)。"""
        self.starvation_score = (time.time() - self.created_at) / 3600 * (1 + self.priority)
        return self.starvation_score


def tasks_dir() -> Path:
    d = config.QIDIAN_DIR / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(task_id: str) -> Path:
    return tasks_dir() / f"{task_id}.json"


def _write(task: Task) -> None:
    p = _path(task.id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, p)  # 同目录 rename 原子, crash 不损坏正式文件


def read_task(task_id: str) -> Optional[Task]:
    p = _path(task_id)
    if not p.exists():
        return None
    return Task.from_dict(json.loads(p.read_text(encoding="utf-8")))


_NEXT_ID_CACHE = 0

def _next_id() -> str:
    """基于毫秒时间戳, 缓存兜底防碰撞。O(1) 非 O(n) 全表扫描。_LOCK 保护并发。"""
    global _NEXT_ID_CACHE
    with _LOCK:
        base = int(time.time() * 1000)
        # 缓存过期时才扫一次全表 (时间戳进位或首次调用)
        if _NEXT_ID_CACHE <= base:
            max_existing = base
            for p in tasks_dir().glob("*.json"):
                try:
                    max_existing = max(max_existing, int(p.stem))
                except ValueError:
                    continue
            _NEXT_ID_CACHE = max(max_existing, base)
        _NEXT_ID_CACHE += 1
        return str(_NEXT_ID_CACHE)


def create(
    desc: str,
    priority: int = 0,
    depends_on: list[str] = None,
    parent_id: str = "",
    depth: int = 0,
) -> Task:
    """建任务。设了 parent_id → child 继承 parent.depth+1。校验 depends_on 引用有效性。"""
    now = time.time()
    # 有父任务时, depth 从父继承 (parent.depth + 1), 防无限递归分解
    if parent_id:
        parent = read_task(parent_id)
        if parent is not None:
            depth = parent.depth + 1
    # depends_on 校验: 过滤不存在的 task_id
    valid_deps = []
    for dep_id in (depends_on or []):
        if read_task(dep_id):
            valid_deps.append(dep_id)
    task = Task(
        id=_next_id(),
        description=desc,
        priority=priority,
        depends_on=valid_deps,
        created_at=now,
        updated_at=now,
        depth=depth,
    )
    _write(task)
    _invalidate_scan_cache()
    return task


def transition(task_id: str, new_status: TaskStatus, **kwargs) -> Optional[Task]:
    with _LOCK:
        task = read_task(task_id)
        if task is None:
            return None
        task.status = new_status
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = time.time()
        _write(task)
        _invalidate_scan_cache()
        return task


def _deps_satisfied(task: Task) -> bool:
    """depends_on 全部 DONE → True。"""
    for dep_id in task.depends_on:
        dep = read_task(dep_id)
        if dep is None or dep.status != TaskStatus.DONE:
            return False
    return True


_DEAD_END = {TaskStatus.FAILED, TaskStatus.ROLLED_BACK, TaskStatus.CONFLICT_HELD}


def _any_dead_dep(task: Task) -> str:
    """检查是否有死路依赖 (建议 #7)。返回第一个死路 dep_id 或空串。"""
    for dep_id in task.depends_on:
        dep = read_task(dep_id)
        if dep is not None and dep.status in _DEAD_END:
            return dep_id
    return ""


def _collect_ready_pending() -> list[Task]:
    """扫所有 pending 且依赖已完成的任务, 刷新 starvation_score。"""
    candidates: list[Task] = []
    for p in tasks_dir().glob("*.json"):
        try:
            task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if task.status != TaskStatus.PENDING:
            continue
        if not _deps_satisfied(task):
            continue
        task.compute_starvation()
        candidates.append(task)
    return candidates


def _sort_key(t: Task) -> tuple:
    """priority desc 优先; 同 priority 下 starvation_score desc (等最久的优先)。"""
    return (-t.priority, -t.starvation_score)


def list_pending() -> list[Task]:
    """所有就绪 pending 任务, 按 priority desc + starvation desc 排。"""
    candidates = _collect_ready_pending()
    candidates.sort(key=_sort_key)
    return candidates


def next_ready() -> Optional[Task]:
    candidates = _collect_ready_pending()
    if not candidates:
        return None
    candidates.sort(key=_sort_key)
    return candidates[0]


def cas(
    task_id: str,
    expect_from: TaskStatus,
    to: TaskStatus,
    **kwargs,
) -> bool:
    """compare-and-swap 原子抢占: 状态==expect_from 才转 to。

    文件系统层面的"比较并交换": _LOCK 串行化 _read→判定→_write (修复 P1-4),
    os.replace 原子写保证 crash 不损坏。返回是否抢占成功。
    """
    with _LOCK:
        task = read_task(task_id)
        if task is None or task.status != expect_from:
            return False
        task.status = to
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = time.time()
        _write(task)
        return True


# ponytail: TTL 缓存减少 glob 全表扫描, 写操作时清空
_TASK_SCAN_CACHE: dict = {"ts": 0, "tasks": []}


def _invalidate_scan_cache():
    _TASK_SCAN_CACHE["ts"] = 0


def ready_tasks(exclude: set[str] = None) -> list[Task]:
    """DAG 就绪判定: 扫 PENDING + ROUTED + BLOCKED, 返回可调度的。

    - depends_on 全 DONE → 就绪; BLOCKED 的转 ROUTED
    - depends_on 有未完成 → 标 BLOCKED, 不返回
    - exclude 里的 task_id 不返回 (防重复调度)
    - 按 (-priority, -starvation_score) 排
    """
    exclude = exclude or set()
    now = time.time()
    # 整段进锁 (修复 P1-4): 每个任务的 读→held判定→ROUTED/BLOCKED 写 必须原子,
    # 否则 Flask 线程的 hold/cancel 会被本扫描的 ROUTED 覆盖 (lost-update)。
    with _LOCK:
        # ponytail: 2s TTL 缓存, 命中时跳过 glob+json.loads
        if now - _TASK_SCAN_CACHE["ts"] < 2 and _TASK_SCAN_CACHE["tasks"]:
            all_tasks = _TASK_SCAN_CACHE["tasks"]
        else:
            all_tasks = []
            for p in tasks_dir().glob("*.json"):
                try:
                    all_tasks.append(Task.from_dict(json.loads(p.read_text(encoding="utf-8"))))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            _TASK_SCAN_CACHE["ts"] = now
            _TASK_SCAN_CACHE["tasks"] = all_tasks
        ready = []
        for task in all_tasks:
            if task.status not in _SCHEDULABLE:
                continue
            if task.id in exclude:
                continue
            if task.held:  # 人工扣留 → 跳过调度
                continue
            dead_dep = _any_dead_dep(task)
            if dead_dep:
                # 不级联失败 → 标记降级，任务继续跑。返工循环会修复。
                task.error = f"上游依赖 {dead_dep} 已失败 (降级运行)"
                task.updated_at = time.time()
                if task.status == TaskStatus.BLOCKED:
                    task.status = TaskStatus.ROUTED
                    _write(task)
                task.compute_starvation()
                ready.append(task)
                continue
            if _deps_satisfied(task):
                # 就绪的 PENDING/BLOCKED 都转 ROUTED (修复 #1: PENDING 不转则 CAS 必失败)
                if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                    task.status = TaskStatus.ROUTED
                    task.updated_at = time.time()
                    _write(task)
                task.compute_starvation()
                ready.append(task)
            else:
                # 依赖未满足 → 标 BLOCKED (仅 PENDING/ROUTED 转, 已 BLOCKED 不重复写)
                if task.status != TaskStatus.BLOCKED:
                    task.status = TaskStatus.BLOCKED
                    task.updated_at = time.time()
                    _write(task)
    ready.sort(key=_sort_key)
    return ready


def set_children(parent_id: str, child_ids: list[str]) -> None:
    """记录 parent 的子任务 id 列表 (DAG 分解后调)。"""
    with _LOCK:
        task = read_task(parent_id)
        if task is None:
            return
        task.children = list(child_ids)
        task.updated_at = time.time()
        _write(task)


def maybe_complete_parent(parent_id: str) -> bool:
    """子任务全 DONE → parent DONE; 任一 FAILED → parent FAILED。

    返回是否触发了 parent 终态转换。
    """
    with _LOCK:
        parent = read_task(parent_id)
        if parent is None or not parent.children:
            return False
        statuses = []
        for cid in parent.children:
            c = read_task(cid)
            if c is None:
                return False  # 子任务文件丢了, 不敢判
            statuses.append(c.status)
        if all(s == TaskStatus.DONE for s in statuses):
            transition(parent_id, TaskStatus.DONE)
            return True
        if any(s == TaskStatus.FAILED for s in statuses):
            transition(parent_id, TaskStatus.FAILED)
            return True
        return False


def recover() -> int:
    count = 0
    with _LOCK:
        for p in tasks_dir().glob("*.json"):
            try:
                task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if task.status in _INFLIGHT:
                task.retry_count += 1
                if task.retry_count >= task.max_retries:
                    task.status = TaskStatus.FAILED
                    task.error = f"recover: 重试 {task.retry_count} 次仍崩, 转 FAILED"
                else:
                    task.status = TaskStatus.PENDING
                task.updated_at = time.time()
                _write(task)
                count += 1
    return count


# ── DAG 结构分析 (拓扑路由前置) ────────────────────────────────

def dag_metrics() -> dict:
    """计算当前任务 DAG 的结构指标。

    返回:
      {"omega": ω, "delta": δ, "gamma": γ,
       "node_count": n, "edge_count": m,
       "components": c, "topology_hint": "parallel"|"sequential"|"mixed"}
    ω = 最大反链 (并行度上限, Dilworth)
    δ = 关键路径 (最长依赖链, 最小延迟)
    γ = 耦合密度 (|E| / max_possible_edges)
    """
    tasks = _load_all_tasks()
    if len(tasks) < 2:
        return {"omega": 1, "delta": 1, "gamma": 0.0,
                "node_count": len(tasks), "edge_count": 0,
                "components": 1, "topology_hint": "sequential"}

    # 用 task_id 建图，只考虑非终态任务
    active_ids = {t.id for t in tasks
                  if t.status not in _TERMINAL}
    if not active_ids:
        active_ids = {t.id for t in tasks}

    # 邻接表: u → [v] (v depends_on u, 所以 u 要先完成)
    adj: dict[str, list[str]] = {tid: [] for tid in active_ids}
    indeg: dict[str, int] = {tid: 0 for tid in active_ids}
    for t in tasks:
        if t.id not in active_ids:
            continue
        for dep_id in t.depends_on:
            if dep_id in active_ids:
                adj.setdefault(dep_id, []).append(t.id)
                indeg[t.id] = indeg.get(t.id, 0) + 1

    edge_count = sum(len(v) for v in adj.values())
    n = len(active_ids)
    max_edges = n * (n - 1) / 2
    gamma = edge_count / max_edges if max_edges > 0 else 0.0

    # δ: 最长路径 (DP on topological order)
    # 用 Kahn 做拓扑排序同时 DP
    indeg_copy = dict(indeg)
    queue = [tid for tid in active_ids if indeg_copy.get(tid, 0) == 0]
    dist: dict[str, int] = {tid: 1 for tid in active_ids}
    topo_order: list[str] = []

    while queue:
        u = queue.pop(0)
        topo_order.append(u)
        for v in adj.get(u, []):
            dist[v] = max(dist.get(v, 1), dist.get(u, 1) + 1)
            indeg_copy[v] -= 1
            if indeg_copy[v] == 0:
                queue.append(v)

    delta = max(dist.values()) if dist else 1

    # ω: 最大反链 ≈ 最大 BFS level 宽度
    # 用距离作 level，统计每层节点数
    level_counts: dict[int, int] = {}
    for tid, d in dist.items():
        level_counts[d] = level_counts.get(d, 0) + 1
    omega = max(level_counts.values()) if level_counts else 1

    # 连通分量数 (弱连通)
    visited: set[str] = set()
    undirected: dict[str, set[str]] = {tid: set() for tid in active_ids}
    for u in adj:
        for v in adj[u]:
            undirected.setdefault(u, set()).add(v)
            undirected.setdefault(v, set()).add(u)
    components = 0
    for tid in active_ids:
        if tid not in visited:
            components += 1
            stack = [tid]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                for nb in undirected.get(node, []):
                    if nb not in visited:
                        stack.append(nb)

    # 拓扑提示
    if omega >= 3 and gamma < 0.3:
        hint = "parallel"
    elif gamma > 0.6:
        hint = "mixed"
    else:
        hint = "sequential"

    return {
        "omega": omega,
        "delta": delta,
        "gamma": round(gamma, 4),
        "node_count": n,
        "edge_count": edge_count,
        "components": components,
        "topology_hint": hint,
    }


def _load_all_tasks() -> list[Task]:
    """加载所有任务 (供 DAG 分析)。"""
    tasks = []
    for p in tasks_dir().glob("*.json"):
        try:
            tasks.append(Task.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return tasks
