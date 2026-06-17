from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from . import config


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
    route_level: str = "E"
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
        return cls(**d)

    def compute_starvation(self) -> float:
        """刷新 starvation_score = (now-created_at)/3600 * (1+priority)。"""
        self.starvation_score = (time.time() - self.created_at) / 3600 * (1 + self.priority)
        return self.starvation_score


def _tasks_dir() -> Path:
    d = config.QIDIAN_DIR / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(task_id: str) -> Path:
    return _tasks_dir() / f"{task_id}.json"


def _write(task: Task) -> None:
    p = _path(task.id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, p)  # 同目录 rename 原子, crash 不损坏正式文件


def _read(task_id: str) -> Optional[Task]:
    p = _path(task_id)
    if not p.exists():
        return None
    return Task.from_dict(json.loads(p.read_text(encoding="utf-8")))


def _next_id() -> str:
    # 基于毫秒时间戳, 并查目录里已有最大 id 兜底, 保证唯一且单调
    base = int(time.time() * 1000)
    max_existing = base
    for p in _tasks_dir().glob("*.json"):
        try:
            max_existing = max(max_existing, int(p.stem))
        except ValueError:
            continue
    return str(max(max_existing, base) + 1)


def create(
    desc: str,
    priority: int = 0,
    depends_on: list[str] = None,
    parent_id: str = "",
    depth: int = 0,
) -> Task:
    """建任务。设了 parent_id → child 继承 parent.depth+1。"""
    now = time.time()
    # 有父任务时, depth 从父继承 (parent.depth + 1), 防无限递归分解
    if parent_id:
        parent = _read(parent_id)
        if parent is not None:
            depth = parent.depth + 1
    task = Task(
        id=_next_id(),
        description=desc,
        priority=priority,
        depends_on=list(depends_on or []),
        created_at=now,
        updated_at=now,
        depth=depth,
    )
    _write(task)
    return task


def transition(task_id: str, new_status: TaskStatus, **kwargs) -> Optional[Task]:
    task = _read(task_id)
    if task is None:
        return None
    task.status = new_status
    for k, v in kwargs.items():
        if hasattr(task, k):
            setattr(task, k, v)
    task.updated_at = time.time()
    _write(task)
    return task


def _deps_satisfied(task: Task) -> bool:
    """depends_on 全部 DONE → True。"""
    for dep_id in task.depends_on:
        dep = _read(dep_id)
        if dep is None or dep.status != TaskStatus.DONE:
            return False
    return True


_DEAD_END = {TaskStatus.FAILED, TaskStatus.ROLLED_BACK, TaskStatus.CONFLICT_HELD}


def _any_dead_dep(task: Task) -> str:
    """检查是否有死路依赖 (建议 #7)。返回第一个死路 dep_id 或空串。"""
    for dep_id in task.depends_on:
        dep = _read(dep_id)
        if dep is not None and dep.status in _DEAD_END:
            return dep_id
    return ""


def _collect_ready_pending() -> list[Task]:
    """扫所有 pending 且依赖已完成的任务, 刷新 starvation_score。"""
    candidates: list[Task] = []
    for p in _tasks_dir().glob("*.json"):
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

    文件系统层面的"比较并交换": 单线程调度器下, _read→判定→_write 之间无竞争,
    os.replace 原子写保证 crash 不损坏。返回是否抢占成功。
    """
    task = _read(task_id)
    if task is None or task.status != expect_from:
        return False
    task.status = to
    for k, v in kwargs.items():
        if hasattr(task, k):
            setattr(task, k, v)
    task.updated_at = time.time()
    _write(task)
    return True


def ready_tasks(exclude: set[str] = None) -> list[Task]:
    """DAG 就绪判定: 扫 PENDING + ROUTED + BLOCKED, 返回可调度的。

    - depends_on 全 DONE → 就绪; BLOCKED 的转 ROUTED
    - depends_on 有未完成 → 标 BLOCKED, 不返回
    - exclude 里的 task_id 不返回 (防重复调度)
    - 按 (-priority, -starvation_score) 排
    """
    exclude = exclude or set()
    ready: list[Task] = []
    for p in _tasks_dir().glob("*.json"):
        try:
            task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if task.status not in _SCHEDULABLE:
            continue
        if task.id in exclude:
            continue
        if task.held:  # 人工扣留 → 跳过调度
            continue
        dead_dep = _any_dead_dep(task)
        if dead_dep:
            # 上游死路 → 本级连带 FAILED (建议 #7)
            task.status = TaskStatus.FAILED
            task.error = f"上游依赖 {dead_dep} 已失败/冲突/回滚, 本级连带失败"
            task.updated_at = time.time()
            _write(task)
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
    task = _read(parent_id)
    if task is None:
        return
    task.children = list(child_ids)
    task.updated_at = time.time()
    _write(task)


def maybe_complete_parent(parent_id: str) -> bool:
    """子任务全 DONE → parent DONE; 任一 FAILED → parent FAILED。

    返回是否触发了 parent 终态转换。
    """
    parent = _read(parent_id)
    if parent is None or not parent.children:
        return False
    statuses = []
    for cid in parent.children:
        c = _read(cid)
        if c is None:
            return False  # 子任务文件丢了, 不敢判
        statuses.append(c.status)
    if TaskStatus.FAILED in statuses:
        transition(parent_id, TaskStatus.FAILED, error="子任务失败, 父任务连带失败")
        return True
    if all(s == TaskStatus.DONE for s in statuses):
        transition(parent_id, TaskStatus.DONE)
        return True
    return False


def recover() -> int:
    count = 0
    for p in _tasks_dir().glob("*.json"):
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
