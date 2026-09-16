"""merge.py — 多 worktree 产出合并队列 (v3 并行调度)

v2: 每个 task 的 worktree 执行完立刻 merge_back 到 main。
v3: 多 task 并行跑 → 产出进 MergeQueue → 串行 drain 到 main。
    main 永远只有一个写入者, 天然避免 git 级合并竞争。

drain 二层冲突检测:
  Layer 1 (快速路径): changed_files ∩ 已合文件集 为空 → 直接 merge_ref
  Layer 2 (精确探测): git merge-tree base main branch 预演
    干净 → merge_ref; 冲突 → park + tracker CONFLICT_HELD

线程安全: submit 多线程可调 (Lock), drain 只主线程调。
修复 #6: 依赖判定用 tracker.read_task(d).status==DONE, 不依赖 self._merged。
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
from singularity.scheduler import witness
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
    repo_root: str = ""     # 目标仓库根 (空=奇点仓库); 项目任务用项目 repo (修复 #1)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "branch": self.branch,
            "base_ref": self.base_ref,
            "changed_files": sorted(self.changed_files),
            "depends_on": self.depends_on, "status": self.status,
            "repo_root": self.repo_root,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergeRequest":
        return cls(
            task_id=d["task_id"], branch=d["branch"],
            base_ref=d.get("base_ref", ""),
            changed_files=set(d.get("changed_files", [])),
            depends_on=d.get("depends_on", []),
            status=d.get("status", "conflict"),
            repo_root=d.get("repo_root", ""),
        )


@dataclass
class MergeResult:
    task_id: str
    status: str             # merged | conflict | failed
    new_head: str = ""
    conflict_files: list[str] = field(default_factory=list)
    reason: str = ""        # 非空时说明真实失败原因 (修复 #3: conflict:[] 误报)


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
        """从磁盘恢复 parking 状态 (进程重启不丢失)。

        ⚠️ **两条静默的路都要出声**（2026-09-14 改）：原来 `if req.branch:` 没有 else、
        `except (...): pass` 也吞掉 ⇒ 一个半写/损坏的 parked 文件会让那个冲突任务
        **从 `conflicts()` 里凭空消失**，而 tracker 里它还是 `CONFLICT_HELD` ——
        于是"有个任务在等人解决冲突"这件事**盘上再也查不到**
        （"损坏和没有长得一样"，本仓反复踩的那个病）。
        """
        config.PARKED_DIR.mkdir(parents=True, exist_ok=True)
        for p in config.PARKED_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                req = MergeRequest.from_dict(d)
            except (json.JSONDecodeError, KeyError, OSError) as e:
                witness.warn("merge", f"parked_recover_failed:{p.name}:{type(e).__name__}"[:160],
                             key="parked_recover_failed")
                continue
            if req.branch:  # branch ref 必须还有效
                self._parked[req.task_id] = req
            else:
                # 没 branch 就没法重放这次合并 —— 但它是个**有人在等的冲突**，
                # 静默丢掉 = 任务蒸发。出声，至少让人知道去盘上找那个文件。
                witness.warn("merge", f"parked_no_branch:{req.task_id}"[:160],
                             key="parked_no_branch")

    def submit(self, req: MergeRequest) -> None:
        with self._lock:
            self._queue.append(req)

    def conflicts(self) -> list[MergeRequest]:
        return list(self._parked.values())

    def drain(self) -> list[MergeResult]:
        """串行合并队列, 返回每个 req 的最终结果。

        修复 #9: 结果在 drain 完成冲突判定后才生成。
        建议 #8: 依赖判定已由 ready_tasks() 门控 (dep DONE 才派发),
        本队列不再做 requeue 检查。_deps_satisfied 保留供防御性调用。
        """
        results: list[MergeResult] = []
        # 连续 defer 计数。原判据是 `len(results) >= len(self._queue)` —— 错的：
        # results 只在**合成功**后才增长，所以"一个都没合 + 有请求被依赖卡住"时
        # 它恒为 0，永远不成立 → 队列原地转圈。实测 1 个依赖未满足的请求就能
        # 让 drain() 永不返回，而它挂在调度主循环的第⑥步（orchestrator:197），
        # 卡住 = 整个调度停摆。
        # 正确判据：连着 defer 满一圈（次数 ≥ 队列长度）说明这一圈毫无进展。
        deferred = 0
        while self._queue:
            if deferred >= len(self._queue):
                # 全是依赖未满足 → 这轮不合它们，留到依赖 DONE 后的下一轮。
                # 必须留痕：静默跳过会让人以为"队列空了"。
                witness.warn("merge", f"drain_dep_blocked:{len(self._queue)}"[:80])
                break
            req = self._queue.popleft()
            # 防御性检查: 依赖任务未完成 → 延迟合并
            if not self._deps_satisfied(req):
                self._queue.append(req)  # 放回队尾
                deferred += 1
                continue
            deferred = 0                 # 有进展就重置，后面的请求还有机会
            results.append(self._drain_one(req))
        return results

    def _deps_satisfied(self, req: MergeRequest) -> bool:
        """依赖的 task **都到终态了**（不会再变）—— 不是"都 DONE"。

        🔴 **2026-09-17 真机坐实（一晚复现两次，重启后仍复现）**：原判据要求依赖全部 `DONE`，
        而 **`FAILED` / `ROLLED_BACK` 也是终态、永远到不了 `DONE`** ⇒ 只要某任务的依赖失败了，
        它的合并请求就**永远满足不了** ⇒ `drain()` 永远 defer（`_drain_pending` 上面那圈
        `if not self._deps_satisfied(req): 放回队尾`）⇒ **它永远留在 `pending_batches`**。

        完整后果链（每一环都核过）：
          `pending_batches` 非空，而两个工人都空闲（`running_futures` 空）
          ⇒ 调度循环的睡觉条件 `not running_futures and not pending_batches` **恒 False**
          ⇒ **全速空转**（实测 **1731 条 `drain_dep_blocked` / 2 分钟**、进程吃 44 分钟 CPU）
          ⇒ 孤儿探测的 `live` 集合**含它** ⇒ 判"有人管" ⇒ 跳过
          ⇒ `_strand_guard` 也不响（**没东西抛异常**，任务只是永远不被处理）
        = **静默死锁**：不抛、不报、界面上任务 `running`、进程活着、一切"正常"。

        ⚠️ `merge.py` 上面那段注释早就写过「**实测 1 个依赖未满足的请求就能让 `drain()`
        永不返回**……卡住 = 整个调度停摆」—— **但他们只修了内层**（让 `drain()` 自己别死循环），
        外层循环照旧把"队里有东西"当成"有活干"。**「修了一半」。**

        依赖是**终态**就意味着它不会再变了，别再等：成功的照常合，失败的按**降级合并**走
        （下游本来就允许降级运行，见 `tracker._any_dead_dep`）。
        ⚠️ `None`（任务文件不存在）**仍算没满足** —— 那是另一种形状，不在这条的射程里。
        """
        for d in req.depends_on:
            t = tracker.read_task(d)
            if t is None or not tracker.is_terminal(t.status):
                return False
        return True

    def _drain_one(self, req: MergeRequest) -> MergeResult:
        req.status = "merging"
        root = req.repo_root or str(config.PROJECT_ROOT)  # 修复 #1: 项目任务合进项目 repo

        # Layer 1 快速路径: changed_files 与已合文件集不重叠 → 直接合
        if not (req.changed_files & self._merged_files):
            mr = merge_ref(req.branch, onto=self.target_branch, repo_root=root)
            if mr.ok:
                return self._mark_merged(req, mr.merged_ref)
            if not mr.conflicts:
                return self._park(req, [], reason=mr.reason)

        # Layer 2 精确探测
        import subprocess
        ours_r = subprocess.run(
            ["git", "rev-parse", self.target_branch],
            cwd=root, capture_output=True, text=True,
        )
        ours = ours_r.stdout.strip()
        if not ours:
            return self._park(req, [], reason=f"无法解析 {self.target_branch}")

        clean, conflict_files = merge_tree_probe(req.base_ref, ours, req.branch, repo_root=root)
        if not clean:
            # 区分真冲突和命令错误 (dangling ref, bad object, etc.)
            if not conflict_files:
                req.status = "failed"
                return MergeResult(task_id=req.task_id, status="failed", conflict_files=[],
                                   reason=f"merge probe 命令错误 (ref 可能已过期: {req.branch[:8]})")
            return self._park(req, conflict_files)

        mr = merge_ref(req.branch, onto=self.target_branch, repo_root=root)
        if mr.ok:
            return self._mark_merged(req, mr.merged_ref)
        return self._park(req, mr.conflicts, reason=mr.reason)

    def _mark_merged(self, req: MergeRequest, new_head: str) -> MergeResult:
        """合成功了 —— **顺便把状态落了**（2026-09-17 真机改）。

        🔴 这里原来**只改内存里那几个字段，不 `tracker.transition`** —— 而 `_park` 是**会**
        transition 的 ⇒ **进得去、出不来**。那次真机的后果链：

          · `resolve(manual)` 返回 `{"status":"merged"}`、**合并在 git 里真发生了**，
            但任务状态还是 `conflict_held`；
          · 而 `resolve` 那一头已经 pop 掉 + **删了盘上的 parked 记录** ⇒ 再调只会回
            「无 parking 记录」⇒ **任务永久卡死**，`/api/conflicts` 还一直列着它；
          · 阶段推进的 `pending` 集合是「非 DONE/ROLLED_BACK/FAILED/DECOMPOSED」，
            **`conflict_held` 算 pending** ⇒ **项目永远推不进 `integrating`**；
          · **而且没有任何 API 能把任务从 `conflict_held` 挪出来**（`retry` 只收
            FAILED/ROLLED_BACK、`update` 只能改 description、`cancel` 只能标 FAILED）。
          ⇒ 那轮是**手改任务 json** 才解开的。

        状态落在这里、而不是留给调用方，是因为**两条路都走这个函数**
        （`_drain_one` 的自动合并 + `resolve` 的人工解锁）—— 放调用方**必漏一条**
        （`merge.py` 上面那条注释早写着同一句话："挂下游必漏一条"）。
        ⚠️ 调用方 `_drain_pending` 原来那句 `transition(DONE)` 已删，避免 DONE→DONE 重复推 SSE。
        """
        req.status = "merged"
        self._merged.add(req.task_id)
        self._merged_files |= req.changed_files
        tracker.transition(req.task_id, TaskStatus.DONE)
        # 合成功了 parking 记录就没用了 —— 留着只会让 `conflicts()` 列出一个**已经合掉**的"冲突"。
        # ⚠️ 删不掉要**出声**（静默 except 棘轮也会拦）：状态已经落成 DONE 了，
        # 删不掉只是让 `/api/conflicts` 多列一条**已经合完**的假冲突，不致命，但得让人看得见。
        try:
            _parked_path(req.task_id).unlink(missing_ok=True)
        except OSError as e:
            witness.warn("merge",
                         f"parked_unlink_failed:{req.task_id}:{type(e).__name__}"[:160],
                         key="parked_unlink_failed")
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
            reason=reason,
        )

    def resolve(self, task_id: str, strategy: str = "manual") -> MergeResult:
        """人工解决后重新合。strategy: manual(已手动改完) | abort(放弃)。

        ⚠️ **改动顺序：先算完，再删盘**（2026-09-17 真机改）。
        原来一进来就 `pop` + `unlink`，之后**不管走哪条路都不再把状态落回去** ——
        而"没有 parked 记录"那条**连 transition 都没有** ⇒ 任务**永久停在 `conflict_held`**。
        现在：成功的路走 `_mark_merged`（它负责 `transition(DONE)` + 删盘）；
        记录真没了 ⇒ **给个终态**（标 FAILED 并说清原因），而不是让它永远挂着。

        ⚠️ **`merge_ref` 是会抛的，抛了必须把 parked 记录补回去**（2026-09-14 改）。
        `merge_ref` 抛出去之后没人管 ⇒ 这个任务还是 `CONFLICT_HELD`，但 `conflicts()` 里
        **再也找不到它** —— 冲突凭空蒸发，人也没法再解它（"状态说有、盘上查不到"）。

        实测抛点（不是推的）：`repo_root` 指向的目录不存在 ⇒ `FileNotFoundError`
        —— 因为原语底下的 `_git_worktree._run` 只吞 `TimeoutExpired`，别的 `OSError` 直接冒。
        """
        req = self._parked.pop(task_id, None)
        if req is None:
            # 🔴 **没有记录也要给个终态**（2026-09-17 真机改）。
            # 原来这条**既不 transition 也不删盘**地直接返回 ⇒ 任务**永久停在 `conflict_held`**：
            # `/api/conflicts` 一直列着它、阶段永远推不动、而且**没有任何 API 能把它挪出来**
            # （`retry` 只收 FAILED/ROLLED_BACK、`update` 只能改 description）。
            # ⚠️ **但只在它还停在 `CONFLICT_HELD` 时才改**（同 `_strand_guard` 的规矩）：
            # 记录可能只是"**上一次已经成功解掉了**"—— 那条路现在会先 `transition(DONE)`
            # 再删盘，这种情况下任务已经是 DONE，**再标 FAILED 就是把交付过的任务降级**。
            t = tracker.read_task(task_id)
            if t is not None and t.status == TaskStatus.CONFLICT_HELD:
                tracker.transition(
                    task_id, TaskStatus.FAILED,
                    error="merge 冲突：找不到 parking 记录，没法重放这次合并（标失败，免得永久卡住）")
            return MergeResult(task_id=task_id, status="failed", conflict_files=["无 parking 记录"])

        if strategy == "abort":
            tracker.transition(task_id, TaskStatus.FAILED, error="merge 冲突, 人工放弃")
            try:
                _parked_path(task_id).unlink(missing_ok=True)
            except OSError:
                pass
            return MergeResult(task_id=task_id, status="failed")

        try:
            mr = merge_ref(req.branch, onto=self.target_branch,
                           repo_root=req.repo_root or str(config.PROJECT_ROOT))
        except Exception as e:          # noqa: BLE001 —— 原语底下什么都可能冒
            self._park(req, [], reason=f"合并原语抛了 {type(e).__name__}")
            witness.warn("merge", f"resolve_merge_ref_failed:{task_id}:{type(e).__name__}"[:160],
                         key="resolve_merge_ref_failed")
            # **返回**失败结果、不往上抛：本函数的契约就是"返回 MergeResult"
            # （连"没有 parked 记录"那条也是返回失败结果），调用方直接读 `.status`/`.reason`。
            return MergeResult(task_id=task_id, status="failed",
                               reason=f"合并原语抛了: {type(e).__name__}: {e}"[:200])
        if mr.ok:
            return self._mark_merged(req, mr.merged_ref)
        return self._park(req, mr.conflicts, reason="resolve 后仍冲突")
