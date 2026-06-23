"""内部模块 — 调度闭环数据类型。

共享数据结构，不依赖其他新模块 (叶子模块)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from singularity.scheduler.tracker import TaskStatus

try:
    from .merge import MergeQueue, MergeRequest
except ImportError:
    MergeQueue = None  # type: ignore
    MergeRequest = None  # type: ignore

# 分解深度安全上限 (防无限递归。达到上限时提示用户手工处理，不限死)
_MAX_DEPTH = 6

# SSE 事件队列 — 跨模块共享，消除 orchestrator→_planner→dispatcher→orchestrator 循环导入
_pending_sse_events: list[dict] = []


@dataclass
class RunContext:
    batch_id: str
    snapshot_ref: str
    worktree_base: str = ""
    merge_queue: "Optional[MergeQueue]" = None


@dataclass
class BatchOutput:
    """worker 线程的纯返回值, 不含任何 tracker 写操作 (修复 #7)。"""
    ok: bool
    task_id: str = ""
    dispatch_result: object = None
    term_reason: str = ""
    validation: object = None
    merge_request: "Optional[MergeRequest]" = None
    planner_decomposed: bool = False
    planner_subtasks: list = field(default_factory=list)  # worker 分解结果, 主线程直接用
    pre_search_skipped: bool = False
    pre_search_reason: str = ""
    pre_search_top_decisions: list = field(default_factory=list)
    pre_search_memory: dict = field(default_factory=dict)
    pre_search_code_context: str = ""
    tool_events: list = field(default_factory=list)   # 工具调用事件 [{tool,status,time,...}]
    turn_count: int = 0                                # 实际推理轮次


class _SnapProxy:
    """轻量快照代理 — 给 validator 读文件用的只读视图。"""
    def __init__(self, ref: str, worktree_path: str = ""):
        self.ref = ref
        self.worktree_path = worktree_path
