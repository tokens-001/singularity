"""merge.py 单元测试 — MergeRequest.to_dict/from_dict 序列化。"""

import pytest


class TestMergeRequest:
    def test_to_dict(self):
        from singularity.scheduler.merge import MergeRequest
        mr = MergeRequest(
            task_id="abc123", branch="refs/heads/wt-abc",
            base_ref="main", changed_files={"a.py", "b.py"},
            depends_on=["task1"], status="queued",
        )
        d = mr.to_dict()
        assert d["task_id"] == "abc123"
        assert d["branch"] == "refs/heads/wt-abc"
        assert set(d["changed_files"]) == {"a.py", "b.py"}
        assert d["status"] == "queued"

    def test_from_dict_full(self):
        from singularity.scheduler.merge import MergeRequest
        d = {
            "task_id": "abc", "branch": "ref",
            "base_ref": "main", "changed_files": ["x.py"],
            "depends_on": ["t1"], "status": "conflict",
        }
        mr = MergeRequest.from_dict(d)
        assert mr.task_id == "abc"
        assert mr.status == "conflict"
        assert mr.changed_files == {"x.py"}

    def test_from_dict_minimal(self):
        from singularity.scheduler.merge import MergeRequest
        mr = MergeRequest.from_dict({"task_id": "t1", "branch": "ref"})
        assert mr.task_id == "t1"
        assert mr.base_ref == ""
        assert mr.changed_files == set()

    def test_roundtrip(self):
        from singularity.scheduler.merge import MergeRequest
        mr = MergeRequest(
            task_id="t1", branch="ref", base_ref="main",
            changed_files={"f.py"}, depends_on=[], status="merged",
        )
        mr2 = MergeRequest.from_dict(mr.to_dict())
        assert mr2.task_id == mr.task_id
        assert mr2.changed_files == mr.changed_files


class TestParkedPath:
    def test_returns_path(self):
        from singularity.scheduler.merge import _parked_path
        p = _parked_path("task123")
        assert "task123.json" in str(p)


class TestDrainTermination:
    """drain() 的"避免死循环"判据。

    原判据 `len(results) >= len(self._queue)` 是错的：results 只在**合成功**后才增长，
    所以"一个都没合 + 有请求被依赖卡住"时它恒为 0，判据永不成立 → 队列原地转圈。
    实测 1 个依赖未满足的请求就能让 drain() 永不返回，而它挂在调度主循环第⑥步
    （orchestrator.py:197）—— 卡住 = 整个调度停摆（不派发、不回收、不合）。
    """

    def _req(self, tid, deps):
        from singularity.scheduler.merge import MergeRequest
        return MergeRequest(task_id=tid, branch="ref", base_ref="base",
                            changed_files=set(), depends_on=deps)

    def _drain_with_deadline(self, monkeypatch, reqs, seconds=3.0):
        """在守护线程里跑 drain，超时即判失败（不然测试自己就挂死了）。"""
        import threading
        from singularity.scheduler import merge as M
        warns = []
        # 打在 witness 模块上而不是 M.witness —— 后者在没 import witness 的版本里
        # 根本不存在，测试会以 AttributeError 变红，抓不住"死循环"这个真正的回归。
        monkeypatch.setattr("singularity.scheduler.witness.warn",
                            lambda *a, **k: warns.append(a))
        monkeypatch.setattr(M.MergeQueue, "_drain_one",
                            lambda self, req: M.MergeResult(task_id=req.task_id,
                                                            status="merged"))
        q = M.MergeQueue()
        for r in reqs:
            q.submit(r)
        box = {}
        th = threading.Thread(target=lambda: box.update(res=q.drain()), daemon=True)
        th.start()
        th.join(timeout=seconds)
        assert not th.is_alive(), "drain() 没返回 —— 死循环回归"
        return box["res"], q, warns

    def test_terminates_when_deps_unsatisfied(self, monkeypatch):
        res, q, warns = self._drain_with_deadline(monkeypatch, [self._req("t1", ["never"])])
        assert res == []
        assert len(q._queue) == 1, "被依赖卡住的请求应留在队列里，等下轮"
        assert any("drain_dep_blocked" in str(w) for w in warns), "静默跳过会让人以为队列空了"

    def test_terminates_when_all_blocked(self, monkeypatch):
        res, q, _ = self._drain_with_deadline(
            monkeypatch, [self._req("t1", ["never"]), self._req("t2", ["never"])])
        assert res == [] and len(q._queue) == 2

    def test_ready_ones_still_merge_alongside_blocked(self, monkeypatch):
        """被卡的不该挡住能合的 —— 有进展就重置计数。"""
        res, q, _ = self._drain_with_deadline(
            monkeypatch, [self._req("blocked", ["never"]), self._req("ready", [])])
        assert [r.task_id for r in res] == ["ready"]
        assert len(q._queue) == 1

    def test_依赖已成终态就别再等_别永久defer(self, monkeypatch):
        """🔴 **2026-09-17 真机坐实（一晚复现两次、干净重启后仍复现）**：
        依赖 `FAILED` 的请求会被**永久** defer，进而**静默死锁整条调度**。

        原判据 `t.status != TaskStatus.DONE` 要求依赖**全部 DONE**；而
        `FAILED`/`ROLLED_BACK` 也是终态、**永远到不了 DONE** ⇒ 请求永远满足不了依赖
        ⇒ 永远留在队列里。后果链（真机每一环都核过）：

          它对应的任务永远留在 `pending_batches`
          ⇒ 调度循环的睡觉条件 `not running_futures and not pending_batches` **恒 False**
          ⇒ **全速空转**（实测 **1731 条 `drain_dep_blocked` / 2 分钟**、进程吃 44 分钟 CPU）
          ⇒ 孤儿探测的 `live` 集合含它 ⇒ 判"有人管" ⇒ 跳过
          ⇒ `_strand_guard` 也不响（**没东西抛异常**，任务只是永远不被处理）

        ——**静默死锁**：不抛、不报、界面上任务 `running`、进程活着。

        依赖到了终态就意味着**它不会再变了**：成功的照常合，失败的按**降级合并**走
        （下游本来就允许降级运行，见 `tracker._any_dead_dep`）。
        """
        from singularity.scheduler import tracker as tk
        from singularity.scheduler.tracker import Task, TaskStatus
        monkeypatch.setattr(tk, "read_task",
                            lambda tid: Task(id=tid, description="d", status=TaskStatus.FAILED))
        res, q, _ = self._drain_with_deadline(monkeypatch, [self._req("t1", ["dep_failed"])])
        assert [r.task_id for r in res] == ["t1"], "依赖已经是终态(失败)了，不该永远等它"
        assert not q._queue, "合掉之后队列该空 —— 否则调用方的 pending_batches 永不清空"

    def test_依赖还在跑_就还得等(self, monkeypatch):
        """**别把修法改宽**：依赖还没到终态（还在跑）就该继续等。

        这条钉的是上面那条改动的**边界** —— 没有它，"终态"被改成"恒真"也不会有测试变红。
        """
        from singularity.scheduler import tracker as tk
        from singularity.scheduler.tracker import Task, TaskStatus
        monkeypatch.setattr(tk, "read_task",
                            lambda tid: Task(id=tid, description="d", status=TaskStatus.RUNNING))
        res, q, warns = self._drain_with_deadline(monkeypatch, [self._req("t1", ["dep_running"])])
        assert res == [] and len(q._queue) == 1, "依赖还在跑，不该合"
        assert any("drain_dep_blocked" in str(w) for w in warns)


class TestResolveWhenPrimitiveRaises:
    """`merge_ref` 抛了的时候，`resolve` 不许把冲突任务弄丢。

    ⚠️ 抛点是**实测的**（不是推的）：`repo_root` 指向的目录不存在 ⇒ `FileNotFoundError`
    —— 原语底下的 `_git_worktree._run` 只吞 `TimeoutExpired`，别的 `OSError` 直接冒。
    而 `resolve` 开头已经把 parked 记录 pop 掉 + 删了盘上的文件 ⇒ 不补回去的话，
    任务还是 `CONFLICT_HELD`，可 `conflicts()` 里**再也找不到它**。
    """

    def test_合并原语抛了_parked记录要补回去(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        from singularity.scheduler import merge as merge_mod
        from singularity.scheduler.merge import MergeQueue, MergeRequest

        monkeypatch.setattr(config, "PARKED_DIR", tmp_path)
        monkeypatch.setattr(merge_mod.tracker, "transition", lambda *a, **k: None)
        warns: list[str] = []
        monkeypatch.setattr(merge_mod.witness, "warn",
                            lambda scope, msg, **kw: warns.append(msg))

        mq = MergeQueue()
        req = MergeRequest(task_id="t1", branch="refs/heads/wt-t1", base_ref="main")
        mq._park(req, [], reason="先 park 进去")

        def _boom(*a, **k):
            raise FileNotFoundError("[Errno 2] 项目目录不存在")

        monkeypatch.setattr(merge_mod, "merge_ref", _boom)

        res = mq.resolve("t1", "manual")

        assert res.status == "failed", res           # 契约：返回失败结果，不往上抛
        assert [r.task_id for r in mq.conflicts()] == ["t1"], \
            "parked 记录没补回去 ⇒ 这个冲突任务从 conflicts() 蒸发了，谁也没法再解它"
        assert (tmp_path / "t1.json").exists(), "盘上的 parked 文件也没了，重启更捞不回来"
        assert any("resolve_merge_ref_failed" in w for w in warns), f"炸了没出声: {warns}"


class TestResolveLandsTheStatus:
    """`resolve` 解完之后**状态要真的落下来** —— 否则任务永久卡 `conflict_held`。

    🔴 2026-09-17 真机（一晚卡住两次）：
      · `_mark_merged` **只改内存里那几个字段、不 `tracker.transition`**（而 `_park` 是**会**的）
        ⇒ **进得去、出不来**；合并**在 git 里真发生了**，任务状态还停在 `conflict_held`；
      · 而"没有 parked 记录"那条**连 transition 都没有** ⇒ 任务**永久挂死**，
        `/api/conflicts` 一直列着它、阶段永远推不进 `integrating`；
      · **而且没有任何 API 能把任务从 `conflict_held` 挪出来**（`retry` 只收 FAILED/ROLLED_BACK、
        `update` 只能改 description、`cancel` 只能标 FAILED —— 那是假的）。
      那轮是**手改任务 json** 才解开的。
    """

    def _mq(self, monkeypatch, tmp_path, *, status, has_record=True, merge_ok=True):
        from singularity.scheduler import config
        from singularity.scheduler import merge as merge_mod
        from singularity.scheduler.merge import MergeQueue, MergeRequest
        from singularity.scheduler.tracker import Task
        from singularity.scheduler._git_worktree import MergeResult as GitMR

        monkeypatch.setattr(config, "PARKED_DIR", tmp_path)
        seen: list = []
        monkeypatch.setattr(merge_mod.tracker, "transition",
                            lambda tid, st, **k: seen.append((tid, st)))
        monkeypatch.setattr(merge_mod.tracker, "read_task",
                            lambda tid: Task(id=tid, description="d", status=status))
        monkeypatch.setattr(merge_mod, "merge_ref",
                            lambda *a, **k: GitMR(ok=merge_ok, merged_ref="abc123"))
        mq = MergeQueue()
        if has_record:
            mq._park(MergeRequest(task_id="t1", branch="refs/heads/wt-t1", base_ref="main"),
                     [], reason="先 park 进去")
        seen.clear()          # 只关心 resolve 之后那次 transition
        return mq, seen

    def test_解成功要落_DONE(self, monkeypatch, tmp_path):
        from singularity.scheduler.tracker import TaskStatus
        mq, seen = self._mq(monkeypatch, tmp_path, status=TaskStatus.CONFLICT_HELD)
        res = mq.resolve("t1", "manual")
        assert res.status == "merged", res
        assert seen == [("t1", TaskStatus.DONE)], (
            "合成功了却不落 DONE ⇒ 任务永远停在 conflict_held，而且没有 API 能救它 —— "
            f"实际 transition 了 {seen}")

    def test_合成功要把_parked_记录删掉(self, monkeypatch, tmp_path):
        from singularity.scheduler.tracker import TaskStatus
        mq, _ = self._mq(monkeypatch, tmp_path, status=TaskStatus.CONFLICT_HELD)
        mq.resolve("t1", "manual")
        assert not (tmp_path / "t1.json").exists(), \
            "合掉了还留着 parked 记录 ⇒ conflicts() 会一直列出一个已经合完的'冲突'"

    def test_没有记录时_要给终态别让它永久挂着(self, monkeypatch, tmp_path):
        from singularity.scheduler.tracker import TaskStatus
        mq, seen = self._mq(monkeypatch, tmp_path, status=TaskStatus.CONFLICT_HELD,
                            has_record=False)
        res = mq.resolve("t1", "manual")
        assert res.status == "failed"
        assert seen == [("t1", TaskStatus.FAILED)], (
            "找不到记录就什么都不做 ⇒ 任务永久卡 conflict_held（这正是真机那次）")

    def test_没有记录但已经_DONE_不许降级(self, monkeypatch, tmp_path):
        """⚠️ **边界**：记录可能只是"上一次已经成功解掉了"（那条路会 transition(DONE) 再删盘）。
        这时再调一次 resolve **不能把交付过的任务标成 FAILED**。
        （同 `_strand_guard` 的规矩：只在它还停在某个中间态时才改。）
        """
        from singularity.scheduler.tracker import TaskStatus
        mq, seen = self._mq(monkeypatch, tmp_path, status=TaskStatus.DONE, has_record=False)
        mq.resolve("t1", "manual")
        assert seen == [], (
            "任务已经是 DONE，resolve 却把它改掉了 —— 这是把交付过的任务降级")
