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
