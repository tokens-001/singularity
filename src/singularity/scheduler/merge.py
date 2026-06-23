"""merge.py — 多 worktree 产出合并队列 (v3 并行调度)

v2: 每个 task 的 worktree 执行完立刻 merge_back 到 main。
v3: 多 task 并行跑 → 产出进 MergeQueue → 串行 drain 到 main。
    main 永远只有一个写入者, 天然避免 git 级合并竞争。

drain 二层冲突检测:
  Layer 1 (快速路径): changed_files ∩ 已合文件集 为空 → 直接 merge_ref
  Layer 2 (精确探测): git merge-tree base main branch 预演
    干净 → merge_ref; 冲突 → park + tracker CONFLICT_HELD

线程安全: submit 多线程可调 (Lock), drain 只主线程调。
修复 #6: 依赖判定用 tracker._read(d).status==DONE, 不依赖 self._merged。
修复 #9: 结果在 drain 完成冲突判定后才生成。
修复 #12: parked 状态持久化到 .qidian/parked/, 重启可恢复。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field

from singularity.scheduler import config
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler._git_worktree import merge_ref, merge_tree_probe


@dataclass
class MergeRequest:
    task_id: str
    branch: str             # worktree 分支的 commit ref (src)
    base_ref: str           # 三方合并的 base (批次快照 ref)
    changed_files: set[str] = field(default_factory=set)
    depends_on: list[str] = field(default_factory=list)
    status: str = "queued"  # queued | merging | merged | conflict | failed

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "branch": self.branch,
            "base_ref": self.base_ref,
            "changed_files": sorted(self.changed_files),
            "depends_on": self.depends_on, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergeRequest":
        return cls(
            task_id=d["task_id"], branch=d["branch"],
            base_ref=d.get("base_ref", ""),
            changed_files=set(d.get("changed_files", [])),
            depends_on=d.get("depends_on", []),
            status=d.get("status", "conflict"),
        )


@dataclass
class MergeResult:
    task_id: str
    status: str             # merged | conflict | failed
    new_head: str = ""
    conflict_files: list[str] = field(default_factory=list)


def _parked_path(task_id: str):
    return config.PARKED_DIR / f"{task_id}.json"


class MergeQueue:
    def __init__(self, target_branch: str = "main"):
        self.target_branch = target_branch
        self._queue: deque[MergeRequest] = deque()
        self._merged: set[str] = set()
        self._merged_files: set[str] = set()
        self._parked: dict[str, MergeRequest] = {}
        self._lock = threading.Lock()
        self._recover_parked()  # 重启恢复

    def _recover_parked(self) -> None:
        """从磁盘恢复 parking 状态 (进程重启不丢失)。"""
        config.PARKED_DIR.mkdir(parents=True, exist_ok=True)
        for p in config.PARKED_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                req = MergeRequest.from_dict(d)
                if req.branch:  # branch ref 必须还有效
                    self._parked[req.task_id] = req
            except (json.JSONDecodeError, KeyError, OSError):
                pass

    def submit(self, req: MergeRequest) -> None:
        with self._lock:
            self._queue.append(req)

    def conflicts(self) -> list[MergeRequest]:
        return list(self._parked.values())

    def parked_ids(self) -> list[str]:
        return list(self._parked.keys())

    def get_parked(self, task_id: str) -> "MergeRequest | None":
        return self._parked.get(task_id)

    def drain(self) -> list[MergeResult]:
        """串行合并队列, 返回每个 req 的最终结果。

        修复 #9: 结果在 drain 完成冲突判定后才生成。
        建议 #8: 依赖判定已由 ready_tasks() 门控 (dep DONE 才派发),
        本队列不再做 requeue 检查。_deps_satisfied 保留供防御性调用。
        """
        results: list[MergeResult] = []
        while self._queue:
            req = self._queue.popleft()
            # 防御性检查: 依赖任务未完成 → 延迟合并
            if not self._deps_satisfied(req):
                self._queue.append(req)  # 放回队尾
                if len(results) >= len(self._queue):
                    break  # 全都不满足依赖, 避免死循环
                continue
            result = self._drain_one(req)
            results.append(result)
        return results

    def _deps_satisfied(self, req: MergeRequest) -> bool:
        """依赖的 task 全部 DONE (防御性检查, 正常由 ready_tasks 门控保证)。"""
        for d in req.depends_on:
            t = tracker._read(d)
            if t is None or t.status != TaskStatus.DONE:
                return False
        return True

    def _drain_one(self, req: MergeRequest) -> MergeResult:
        req.status = "merging"

        # Layer 1 快速路径: changed_files 与已合文件集不重叠 → 直接合
        if not (req.changed_files & self._merged_files):
            mr = merge_ref(req.branch, onto=self.target_branch)
            if mr.ok:
                return self._mark_merged(req, mr.merged_ref)
            if not mr.conflicts:
                return self._park(req, [], reason=mr.reason)

        # Layer 2 精确探测
        import subprocess
        ours_r = subprocess.run(
            ["git", "rev-parse", self.target_branch],
            cwd=str(config.PROJECT_ROOT), capture_output=True, text=True,
        )
        ours = ours_r.stdout.strip()
        if not ours:
            return self._park(req, [], reason=f"无法解析 {self.target_branch}")

        clean, conflict_files = merge_tree_probe(req.base_ref, ours, req.branch)
        if not clean:
            # 区分真冲突和命令错误 (dangling ref, bad object, etc.)
            if not conflict_files:
                req.status = "failed"
                return MergeResult(task_id=req.task_id, status="failed", conflict_files=[],
                                   reason=f"merge probe 命令错误 (ref 可能已过期: {req.branch[:8]})")
            return self._park(req, conflict_files)

        mr = merge_ref(req.branch, onto=self.target_branch)
        if mr.ok:
            return self._mark_merged(req, mr.merged_ref)
        return self._park(req, mr.conflicts, reason=mr.reason)

    def _mark_merged(self, req: MergeRequest, new_head: str) -> MergeResult:
        req.status = "merged"
        self._merged.add(req.task_id)
        self._merged_files |= req.changed_files
        return MergeResult(task_id=req.task_id, status="merged", new_head=new_head)

    def _park(self, req: MergeRequest, conflicts: list[str], reason: str = "") -> MergeResult:
        req.status = "conflict"
        self._parked[req.task_id] = req
        # 持久化到磁盘, 进程重启可恢复
        try:
            config.PARKED_DIR.mkdir(parents=True, exist_ok=True)
            _parked_path(req.task_id).write_text(
                json.dumps(req.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        tracker.transition(
            req.task_id, TaskStatus.CONFLICT_HELD,
            error=f"merge 冲突 parking: {reason or conflicts}",
        )
        return MergeResult(
            task_id=req.task_id, status="conflict", conflict_files=conflicts,
        )

    def resolve(self, task_id: str, strategy: str = "manual") -> MergeResult:
        """人工解决后重新合。strategy: manual(已手动改完) | abort(放弃)。"""
        req = self._parked.pop(task_id, None)
        # 清理磁盘持久化
        try:
            _parked_path(task_id).unlink(missing_ok=True)
        except OSError:
            pass
        if req is None:
            return MergeResult(task_id=task_id, status="failed", conflict_files=["无 parking 记录"])

        if strategy == "abort":
            tracker.transition(task_id, TaskStatus.FAILED, error="merge 冲突, 人工放弃")
            return MergeResult(task_id=task_id, status="failed")

        mr = merge_ref(req.branch, onto=self.target_branch)
        if mr.ok:
            return self._mark_merged(req, mr.merged_ref)
        return self._park(req, mr.conflicts, reason="resolve 后仍冲突")
