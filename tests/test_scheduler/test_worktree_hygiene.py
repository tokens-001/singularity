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


class TestCleanupCoversProjectRepos:
    """🔴 **接线**：`cleanup_orphans()` 真的去遍历**每棵**仓库了吗（外派⑬ 报的缺口）。

    上面 `test_covers_project_repos` 钉的是**枚举函数** `_project_repo_roots()`
    本身 —— 而"清理时真的用了它"是另一回事：实测把 `cleanup_orphans` 里那句
    `for root in _project_repo_roots():` 改回只扫 `config.PROJECT_ROOT`，
    全文件照样绿，而生产上就是本文件开头写的那个事故（项目仓库的残留永不清理，
    攒到 50 个之后该项目每个任务静默降级成无沙箱）。

    变异：把 `cleanup_orphans` 里那句遍历改回只扫 `config.PROJECT_ROOT` → 红。
    """

    def test_项目仓库里的孤儿会被清掉(self, tmp_path, monkeypatch):
        from singularity.scheduler import config as cfg, project as proj_mod

        proot = tmp_path / "projects"
        proot.mkdir()
        prepo = proot / "alpha"
        prepo.mkdir()
        _git(prepo, "init", "-q")
        _git(prepo, "config", "user.email", "t@t")
        _git(prepo, "config", "user.name", "t")
        (prepo / "a.txt").write_text("x\n")
        _git(prepo, "add", "-A")
        _git(prepo, "commit", "-qm", "base")

        # 孤儿：worktree 目录在，但 git 不认得它（上次崩溃留下的那种）
        wtd = proot / ".alpha-worktrees"
        wtd.mkdir()
        orphan = wtd / "t-999"
        orphan.mkdir()
        (orphan / "junk.txt").write_text("残留\n")

        # 引擎仓库也要真存在：`cleanup_orphans` 最后那步还要在 PROJECT_ROOT 里跑
        # `git for-each-ref`（清 pending refs），目录不存在会 FileNotFoundError。
        engine = tmp_path / "engine"
        engine.mkdir()
        _git(engine, "init", "-q")

        monkeypatch.setattr(proj_mod, "get_projects_root", lambda: proot)
        monkeypatch.setattr(cfg, "PROJECT_ROOT", engine)

        cleaned = gw.cleanup_orphans()
        assert cleaned >= 1, "项目仓库里的孤儿没被清 —— 遍历那行是不是断回 PROJECT_ROOT 了？"
        assert not orphan.exists(), f"孤儿还在：{orphan}"


class TestWorktreesDirDoesNotMkdir:
    """🔴 **`_worktrees_dir()` 只算路径、不建目录**（2026-09-19）。

    这个函数原来带 `d.mkdir(parents=True, exist_ok=True)`，而**除了 `create()`
    之外的三个调用点全是只读的**（`_worktree.py` 清 worktree / 数上限、
    `orchestrator.py` 捞超时任务的现场），它们只用 `.glob()` / `.iterdir()`。
    带着 mkdir 走那三条路 ⇒ 给「本来没有 worktree 的仓库」凭空建出空目录。

    盘上已坐实的后果：项目被删之后 `repo_dir()` 走兜底分支（返回
    `.qidian/projects/<id>/repo`，且**明令绝不能 mkdir** —— 建出来的空目录让
    "项目文件自己消失"看起来像真的），某个只读调用点拿它喂进来 ⇒
    `.qidian/projects/<id>/` 下**只剩一个 `.repo-worktrees`**，
    兜底那句断言当场作废。

    变异：把 `d.mkdir(parents=True, exist_ok=True)` 加回 `_worktrees_dir()` → 红。
    """

    def test_只算路径不建目录(self, tmp_path):
        """`repo_dir()` 兜底给的那个路径喂进来，不许把它的父目录 mk 出来。"""
        # 形状照抄 project.py 兜底分支：<projects_dir>/<id>/repo，且**不存在**
        fake_root = tmp_path / "projects" / "1789475332592" / "repo"

        wtd = gw._worktrees_dir(fake_root)
        assert wtd == fake_root.parent / ".repo-worktrees", f"路径拼错了: {wtd}"

        # 只读用法：glob 一遍（`_worktree.py` / `orchestrator.py` 就是这么用的）
        assert list(wtd.glob("t*")) == []

        assert not fake_root.parent.exists(), (
            f"只读调用把 {fake_root.parent} 建出来了 —— "
            "`_worktrees_dir()` 里那行 mkdir 是不是又加回去了？"
        )

    def test_不建目录也不妨碍_create(self, repo):
        """另一半：去掉 mkdir 之后 `create()` 照样能建 —— **父目录由 git 自己建**。

        这是"删掉那行是安全的"的证据（2026-09-19 实测：`git worktree add` 指向一个
        父目录不存在的路径照样成功）。没这条的话，"不 mkdir"可能是在拿 create() 换。
        """
        assert not (repo.parent / f".{repo.name}-worktrees").exists()
        wt = gw.create("t-mkdir-probe", "E", repo_root=repo)
        assert wt.path.is_dir(), f"worktree 没建成：{wt.path}"
