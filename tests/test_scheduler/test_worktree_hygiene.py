"""worktree 卫生：孤儿清理与 realpath 幂等（2026-09-11 审计）。

**孤儿永不清理**：`cleanup_orphans()` 只扫 `config.PROJECT_ROOT`（奇点自己），
而项目任务的 worktree 落在 `<projects_root>/.<项目名>-worktrees` —— 从不被清。
实测磁盘上有 9 月 7 日的残留一直没被扫到；按 `_MAX_WORKTREES` 的计数口径，
攒到 50 个之后该项目的**每个任务都会静默降级成无沙箱执行**。

**realpath 失配**：`create()` 的幂等判断是 `str(wt_path) == git 报的路径`，
而 git 报的是 realpath（macOS 上 `/var/...` → `/private/var/...`）。
失配 → 走 `worktree add` → rc=128 already exists → 被上层静默降级成无沙箱。

**在旧代码上会红、且红得对**（断言失败）：`test_idempotent_under_symlinked_path`
在旧代码上会因重复建 worktree 而失败。
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import _git_worktree as gw     # noqa: E402


def _git(repo: Path, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _worktree_entries(repo: Path) -> int:
    return _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ")


class TestIdempotentCreate:
    def test_idempotent_under_symlinked_path(self, repo):
        """同一 task 建两次只应有一个 worktree。

        pytest 的 tmp_path 在 macOS 上位于 `/private/var/...`，而传入的路径字符串
        可能是 `/var/...` —— 正好复现 realpath 失配。
        """
        gw.create("task1", "E", repo_root=repo)
        before = _worktree_entries(repo)
        gw.create("task1", "E", repo_root=repo)
        after = _worktree_entries(repo)
        assert after == before, f"重复建了 worktree（{before} → {after}）—— rc=128 那条静默降级路径"

    def test_realpath_is_the_comparison_key(self, repo):
        """锁住判据本身：git 报的路径与 realpath 同口径。"""
        import os
        wt = gw.create("task2", "E", repo_root=repo)
        listed = [l.split(maxsplit=1)[1] for l in
                  _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
                  if l.startswith("worktree ")]
        assert any(os.path.realpath(p) == os.path.realpath(str(wt.path)) for p in listed)


class TestCleanupOrphans:
    def test_removes_orphan_keeps_registered(self, repo):
        """孤儿目录被清掉，git 认得的 worktree 必须保住。"""
        live = gw.create("live", "E", repo_root=repo)
        wtd = repo.parent / f".{repo.name}-worktrees"
        orphan = wtd / "dead_E"
        orphan.mkdir(parents=True)
        (orphan / "junk.txt").write_text("残留")

        cleaned = gw._cleanup_worktree_dirs(repo)

        assert cleaned == 1
        assert not orphan.exists(), "孤儿没被清掉"
        assert live.path.exists(), "把在用的 worktree 也删了 —— 会毁掉正在跑的任务"

    def test_no_worktrees_dir_is_a_noop(self, repo):
        assert gw._cleanup_worktree_dirs(repo) == 0
        # 不能因为调用清理就凭空建出容器目录
        assert not (repo.parent / f".{repo.name}-worktrees").exists()

    def test_missing_repo_is_a_noop(self, tmp_path):
        assert gw._cleanup_worktree_dirs(tmp_path / "gone") == 0


class TestProjectRepoEnumeration:
    def test_covers_project_repos(self, tmp_path, monkeypatch):
        """_project_repo_roots 必须包含项目仓库，不只是奇点自己。"""
        from singularity.scheduler import project as proj_mod
        proot = tmp_path / "projects"
        proot.mkdir()
        for name in ("alpha", "beta"):
            d = proot / name
            d.mkdir()
            _git(d, "init", "-q")
        monkeypatch.setattr(proj_mod, "get_projects_root", lambda: proot)

        roots = gw._project_repo_roots()
        names = {r.name for r in roots}
        assert "alpha" in names and "beta" in names, f"项目仓库没被纳入清理范围: {names}"
