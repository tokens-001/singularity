"""回滚保护的 fail-closed —— 数据丢失回归测试（2026-09-11 审计）。

**这个 bug**：`_rollback_git` 里 `git stash push --include-untracked` 的**返回码不被检查**，
失败时照样往下走 `git checkout -- .` + `git clean -fd`。

stash 为什么失败：索引处于未合并态（`UU`）时，`git stash push` 报
`error: could not write index` 并 **rc=1**（index.lock 竞争、磁盘满同理）。

后果：本该被 stash 保护的**用户未跟踪文件被 `clean -fd` 直接删掉，且不在 stash 里 —— 不可恢复**。

`_do_merge` 早就改成了 fail-closed（主仓库脏就拒绝合并），只有回滚这条还是 fail-open。

**在旧代码上会红、且红得对**（断言失败）：旧代码不管 rc 就往下删，
`test_user_untracked_file_survives` 会因文件消失而失败。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import snapshot as snap_mod     # noqa: E402


def _git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)


def _init_conflicted_repo(tmp_path) -> Path:
    """造一个索引未合并(UU)的仓库 + 一个用户未跟踪文件。"""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    base = _git(r, "symbolic-ref", "--short", "HEAD").stdout.strip()

    (r / "f.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")

    _git(r, "checkout", "-q", "-b", "other")
    (r / "f.txt").write_text("other\n")
    _git(r, "commit", "-qam", "other")

    _git(r, "checkout", "-q", base)
    (r / "f.txt").write_text("mine\n")
    _git(r, "commit", "-qam", "mine")

    _git(r, "merge", "other")           # 必然冲突 → 索引 UU
    assert "UU" in _git(r, "status", "--porcelain").stdout, "夹具没造出未合并索引"

    (r / "USER_NOTES.txt").write_text("用户手写的重要笔记\n")   # 未跟踪
    return r


class TestRollbackStashGuard:
    def test_stash_really_fails_on_unmerged_index(self, tmp_path):
        """先确认前提出成立：这个状态下 stash push 确实失败。"""
        r = _init_conflicted_repo(tmp_path)
        res = _git(r, "stash", "push", "--include-untracked", "-m", "probe")
        assert res.returncode != 0, "前提不成立：这个夹具下 stash 竟然成功了"

    def test_rollback_aborts_and_keeps_user_file(self, tmp_path):
        """核心回归：stash 失败时回滚必须中止，用户未跟踪文件不能丢。"""
        r = _init_conflicted_repo(tmp_path)
        snap = snap_mod.Snapshot(id="s1", method="git", ref="",
                                 created_at=0, repo_root=str(r))

        ok = snap_mod._rollback_git(snap, r)

        assert ok is False, "stash 失败却没中止 —— 接下来会 clean -fd 删用户文件"
        assert (r / "USER_NOTES.txt").exists(), "用户的未跟踪文件被删了（且不在 stash 里，不可恢复）"

    def test_clean_case_still_rolls_back(self, tmp_path):
        """对照：干净可 stash 的情形照常回滚，别把正常路径也堵死。"""
        r = tmp_path / "clean"
        r.mkdir()
        _git(r, "init", "-q")
        _git(r, "config", "user.email", "t@t")
        _git(r, "config", "user.name", "t")
        (r / "f.txt").write_text("base\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "base")

        base_ref = _git(r, "rev-parse", "HEAD").stdout.strip()
        (r / "f.txt").write_text("改了\n")
        (r / "extra.txt").write_text("新文件\n")

        snap = snap_mod.Snapshot(id="s2", method="git", ref=base_ref,
                                 created_at=0, repo_root=str(r))
        ok = snap_mod._rollback_git(snap, r)

        assert ok is True
        assert (r / "f.txt").read_text() == "base\n", "没回到快照状态"
        assert not (r / "extra.txt").exists(), "agent 新建的文件该被清掉"
