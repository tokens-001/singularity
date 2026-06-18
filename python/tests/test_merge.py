"""merge queue 集成测试 —— 验证 Layer 1/Layer 2 + parked 持久化。

Opus P0: 两任务改同文件过 MergeQueue.drain。
  ① 改不相交文件 → Layer 1 自动合
  ② 改同文件不同区域 → Layer 2 merge-tree 自动合
  ③ 改同文件同一行 → park + CONFLICT_HELD
  ④ 重启恢复 parked
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from scheduler.merge import MergeQueue, MergeRequest


@pytest.fixture
def mq():
    return MergeQueue(target_branch="main")


class TestMergeQueueDrain:
    """P0 地基: 并行 merge 实测。"""

    def test_layer1_disjoint_files(self, mq):
        """① 改不相交文件 → Layer 1 直接合, 不触发 merge-tree。"""
        import subprocess
        root = Path(__file__).resolve().parent.parent.parent
        saved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True,
        ).stdout.strip()

        # 创建两个分支, 各改不同文件
        def _make_branch(name: str, filename: str, content: str) -> str:
            subprocess.run(["git", "checkout", "-b", name, "main"], cwd=str(root),
                           capture_output=True)
            (root / filename).write_text(content)
            subprocess.run(["git", "add", filename], cwd=str(root), capture_output=True)
            subprocess.run(["git", "commit", "-m", f"test: {name}"], cwd=str(root),
                           capture_output=True)
            ref = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=str(root), capture_output=True)
            return ref

        try:
            ref_a = _make_branch("q-test-mq-a", "_mq_test_a.py", "# file A\n")
            ref_b = _make_branch("q-test-mq-b", "_mq_test_b.py", "# file B\n")
            base = subprocess.run(
                ["git", "merge-base", "main", ref_a], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()

            mq.submit(MergeRequest(
                task_id="test-L1-a", branch=ref_a, base_ref=base,
                changed_files={"_mq_test_a.py"},
            ))
            mq.submit(MergeRequest(
                task_id="test-L1-b", branch=ref_b, base_ref=base,
                changed_files={"_mq_test_b.py"},
            ))
            results = mq.drain()

            assert len(results) == 2
            assert results[0].status == "merged"
            assert results[1].status == "merged"

            # 文件应存在
            assert (root / "_mq_test_a.py").exists()
            assert (root / "_mq_test_b.py").exists()

        finally:
            # 回滚
            subprocess.run(["git", "reset", "--hard", saved_head], cwd=str(root),
                           capture_output=True)
            for b in ["q-test-mq-a", "q-test-mq-b"]:
                subprocess.run(["git", "branch", "-D", b], cwd=str(root),
                               capture_output=True)
            for f in ["_mq_test_a.py", "_mq_test_b.py"]:
                (root / f).unlink(missing_ok=True)

    def test_layer2_same_file_different_regions(self, mq):
        """② 改同文件不同区域 → Layer 2 merge-tree 自动合。"""
        import subprocess
        root = Path(__file__).resolve().parent.parent.parent
        saved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True,
        ).stdout.strip()

        base_file = (root / "_mq_shared.py")
        base_file.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")
        subprocess.run(["git", "add", "_mq_shared.py"], cwd=str(root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "test: base for L2"], cwd=str(root),
                       capture_output=True)

        def _make_branch(name: str, modify_line: int, new_text: str) -> str:
            subprocess.run(["git", "checkout", "-b", name, "main"], cwd=str(root),
                           capture_output=True)
            lines = base_file.read_text().splitlines()
            lines[modify_line - 1] = new_text
            base_file.write_text("\n".join(lines) + "\n")
            subprocess.run(["git", "add", "_mq_shared.py"], cwd=str(root),
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", f"test: {name}"], cwd=str(root),
                           capture_output=True)
            ref = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=str(root), capture_output=True)
            return ref

        try:
            ref_a = _make_branch("q-test-mq-l2a", 1, "LINE ONE MODIFIED")
            ref_b = _make_branch("q-test-mq-l2b", 5, "LINE FIVE MODIFIED")
            base = subprocess.run(
                ["git", "merge-base", "main", ref_a], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()

            # 两个都改 _mq_shared.py → Layer 1 有重叠 → 走 Layer 2
            mq.submit(MergeRequest(
                task_id="test-L2-a", branch=ref_a, base_ref=base,
                changed_files={"_mq_shared.py"},
            ))
            mq.submit(MergeRequest(
                task_id="test-L2-b", branch=ref_b, base_ref=base,
                changed_files={"_mq_shared.py"},
            ))
            results = mq.drain()

            assert len(results) == 2
            # 第一个 (改 line1+line5) 应合并成功
            assert results[0].status == "merged"
            # 第二个 (base line1=LINE ONE MODIFIED, branch 改 line5 → 也应为干净合并)
            assert results[1].status == "merged"

            # 文件应含两处修改
            content = base_file.read_text()
            assert "LINE ONE MODIFIED" in content
            assert "LINE FIVE MODIFIED" in content

        finally:
            subprocess.run(["git", "reset", "--hard", saved_head], cwd=str(root),
                           capture_output=True)
            for b in ["q-test-mq-l2a", "q-test-mq-l2b"]:
                subprocess.run(["git", "branch", "-D", b], cwd=str(root),
                               capture_output=True)
            for f in ["_mq_shared.py"]:
                (root / f).unlink(missing_ok=True)

    def test_layer2_same_line_conflict(self, mq):
        """③ 改同文件同一行 → park + CONFLICT_HELD。"""
        import subprocess
        root = Path(__file__).resolve().parent.parent.parent
        saved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True,
        ).stdout.strip()

        base_file = (root / "_mq_conflict.py")
        base_file.write_text("original line\n")
        subprocess.run(["git", "add", "_mq_conflict.py"], cwd=str(root),
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "test: base for conflict"], cwd=str(root),
                       capture_output=True)

        def _make_branch(name: str, content: str) -> str:
            subprocess.run(["git", "checkout", "-b", name, "main"], cwd=str(root),
                           capture_output=True)
            base_file.write_text(content)
            subprocess.run(["git", "add", "_mq_conflict.py"], cwd=str(root),
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", f"test: {name}"], cwd=str(root),
                           capture_output=True)
            ref = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=str(root), capture_output=True)
            return ref

        try:
            ref_a = _make_branch("q-test-mq-conflict-a", "version A\n")
            ref_b = _make_branch("q-test-mq-conflict-b", "version B\n")
            base = subprocess.run(
                ["git", "merge-base", "main", ref_a], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()

            mq.submit(MergeRequest(
                task_id="test-conflict-a", branch=ref_a, base_ref=base,
                changed_files={"_mq_conflict.py"},
            ))
            mq.submit(MergeRequest(
                task_id="test-conflict-b", branch=ref_b, base_ref=base,
                changed_files={"_mq_conflict.py"},
            ))
            results = mq.drain()

            assert len(results) == 2
            # 第一个合并成功
            assert results[0].status == "merged"
            # 第二个应冲突 parking
            assert results[1].status == "conflict"
            assert "_mq_conflict.py" in results[1].conflict_files

        finally:
            subprocess.run(["git", "reset", "--hard", saved_head], cwd=str(root),
                           capture_output=True)
            for b in ["q-test-mq-conflict-a", "q-test-mq-conflict-b"]:
                subprocess.run(["git", "branch", "-D", b], cwd=str(root),
                               capture_output=True)
            for f in ["_mq_conflict.py"]:
                (root / f).unlink(missing_ok=True)

    def test_parked_persist_and_recover(self, mq):
        """④ parked 持久化: 写入磁盘 → 新 MergeQueue 恢复。"""
        import subprocess
        root = Path(__file__).resolve().parent.parent.parent
        saved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True,
        ).stdout.strip()

        base_file = (root / "_mq_persist.py")
        base_file.write_text("original\n")
        subprocess.run(["git", "add", "_mq_persist.py"], cwd=str(root),
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "test: base for persist"], cwd=str(root),
                       capture_output=True)

        try:
            # 创建冲突分支
            subprocess.run(["git", "checkout", "-b", "q-test-persist", "main"],
                           cwd=str(root), capture_output=True)
            base_file.write_text("changed\n")
            subprocess.run(["git", "add", "_mq_persist.py"], cwd=str(root),
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "test: persist branch"], cwd=str(root),
                           capture_output=True)
            ref = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=str(root), capture_output=True)
            base = subprocess.run(
                ["git", "merge-base", "main", ref], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()

            # 先合并一次 → 应该是第一个, 成功 (无冲突)
            mq.submit(MergeRequest(
                task_id="test-persist-p1", branch=ref, base_ref=base,
                changed_files={"_mq_persist.py"},
            ))
            results = mq.drain()
            assert results[0].status == "merged"

            # 再创建另一个冲突分支
            subprocess.run(["git", "checkout", "-b", "q-test-persist2", "main"],
                           cwd=str(root), capture_output=True)
            base_file.write_text("also changed\n")
            subprocess.run(["git", "add", "_mq_persist.py"], cwd=str(root),
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "test: persist branch 2"], cwd=str(root),
                           capture_output=True)
            ref2 = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=str(root), capture_output=True)

            # 这个会产生冲突
            mq.submit(MergeRequest(
                task_id="test-persist-conflict", branch=ref2, base_ref=base,
                changed_files={"_mq_persist.py"},
            ))
            results2 = mq.drain()
            assert results2[0].status == "conflict"

            # 模拟重启: 新建 MergeQueue, 应恢复 parked
            mq2 = MergeQueue(target_branch="main")
            assert "test-persist-conflict" in mq2.parked_ids()

            # resolve
            result = mq2.resolve("test-persist-conflict", strategy="abort")
            assert result.status == "failed"

            # 确认清理
            mq3 = MergeQueue(target_branch="main")
            assert "test-persist-conflict" not in mq3.parked_ids()

        finally:
            subprocess.run(["git", "reset", "--hard", saved_head], cwd=str(root),
                           capture_output=True)
            for b in ["q-test-persist", "q-test-persist2"]:
                subprocess.run(["git", "branch", "-D", b], cwd=str(root),
                               capture_output=True)
            for f in ["_mq_persist.py"]:
                (root / f).unlink(missing_ok=True)
