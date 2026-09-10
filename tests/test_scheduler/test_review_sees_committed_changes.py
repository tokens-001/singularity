"""审查必须看得见"已被提交的改动" —— P0-1 的收口（2026-09-11 审计）。

**这个 bug**：worktree 路径下，改动在 `validate()` **之前**就被
`_exec._process_planner_or_merge` 里的 `commit_wt` 提交了。于是审查/QA/安全审计
用的裸 `git diff`（跟 HEAD 比）**恒为空** ——

- `_is_trivial_change` 判"0 行 < 50 行" → 单文件改动**全被当成小改动跳过审查**
- `multi_model_review(diff_only=True)` 拿到空 diff → **一个模型都不调**就返回
  `{"issues":[],"verdicts":[]}`，上层以为"审过且没问题"
- `security_audit_review` / `qa_acceptance_review` 拿到 `(无 diff)`

整层质量把关形同虚设。

修法：取 diff 时带上**执行前快照**的 ref（`validator._diff_base(snap)`），
由 `_exec` 传给 `run_post_exec_checks(base_ref=...)`。

**在旧代码上会红、且红得对**（断言失败）：`_is_trivial_change` 没有 base_ref 参数，
下面第一条会因"122 行的改动被判 trivial"而失败。
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import _review as rv          # noqa: E402
from singularity.scheduler import validator as val       # noqa: E402


def _git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def committed_change(tmp_path):
    """模拟 worktree 实况：改动**已被提交**，工作区干净。

    返回 (repo, snap_ref)。snap_ref = 改动前的 HEAD（等同 snapshot.take 的兜底）。
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def f():\n    return 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    snap_ref = _git(r, "rev-parse", "HEAD").stdout.strip()

    # agent 改完 → commit_wt 提交
    (r / "app.py").write_text(
        "def f():\n    return 1\n"
        + "\n".join(f"def g{i}():\n    return {i}\n" for i in range(40)))
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "worktree commit")
    assert not _git(r, "status", "--porcelain").stdout.strip(), "夹具要求工作区干净"
    return r, snap_ref


class TestTrivialJudgement:
    def test_big_change_is_not_trivial_with_base(self, committed_change):
        """带上基准后，40+ 行的改动不该再被判成"小改动"。"""
        r, base = committed_change
        assert rv._is_trivial_change(["app.py"], str(r), base) is False

    def test_without_base_it_looks_trivial(self, committed_change):
        """对照：不带基准（旧行为）时看起来是 0 行 → 恒判 trivial。

        这条断言的是**缺陷本身**，修复前后都通过 —— 它是"为什么必须带基准"的证据。
        """
        r, _ = committed_change
        assert rv._is_trivial_change(["app.py"], str(r)) is True

    def test_really_small_change_still_trivial(self, tmp_path):
        """对照：真的是小改动时仍然跳过（别把这条优化也堵死）。"""
        r = tmp_path / "small"
        r.mkdir()
        _git(r, "init", "-q")
        _git(r, "config", "user.email", "t@t")
        _git(r, "config", "user.name", "t")
        (r / "a.py").write_text("x = 1\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "base")
        base = _git(r, "rev-parse", "HEAD").stdout.strip()
        (r / "a.py").write_text("x = 2\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "tiny")
        assert rv._is_trivial_change(["a.py"], str(r), base) is True


class TestDiffCommand:
    def test_with_base_uses_it(self):
        assert rv._diff_cmd("abc123", "f.py") == ["git", "diff", "abc123", "f.py"]

    def test_without_base_falls_back(self):
        assert rv._diff_cmd("", "f.py") == ["git", "diff", "f.py"]


class TestPlumbing:
    def test_run_post_exec_checks_accepts_base_ref(self):
        import inspect
        assert "base_ref" in inspect.signature(rv.run_post_exec_checks).parameters

    def test_multi_model_review_accepts_base_ref(self):
        import inspect
        assert "base_ref" in inspect.signature(val.multi_model_review).parameters

    def test_exec_passes_the_snapshot_base(self):
        """调用方必须真的把快照 ref 传进去 —— 参数加了不传等于没修。"""
        import inspect
        from singularity.scheduler import _exec
        src = inspect.getsource(_exec.run)
        assert "base_ref=" in src, "run() 没把 base_ref 传给审查"
