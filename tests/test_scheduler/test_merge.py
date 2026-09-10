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
