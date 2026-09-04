"""内部模块 — worktree 生命周期管理。

创建/清理/加锁/解锁/git ref 锚定。叶子模块，不依赖其他新模块。
"""

from __future__ import annotations

import os
import stat
import subprocess as _sp
from singularity.scheduler import config
from singularity.scheduler._git_worktree import (
    Worktree, create as wt_create, cleanup as wt_cleanup,
    merge_back as wt_merge_back, commit_wt, changed_files_between,
)

try:
    from .merge import MergeRequest
except ImportError:
    MergeRequest = None  # type: ignore


def _build_merge_request(task, branch_ref: str, base_ref: str, repo_root=None) -> "MergeRequest":
    changed = set(changed_files_between(base_ref, branch_ref, repo_root=repo_root))
    deps = list(task.depends_on) if task.depends_on else []
    return MergeRequest(
        task_id=task.id, branch=branch_ref, base_ref=base_ref,
        changed_files=changed, depends_on=deps,
        repo_root=str(repo_root) if repo_root else "",
    )


def _anchor_ref(task_id: str, commit_sha: str, repo_root=None) -> bool:
    """给悬空 commit 打锚定 ref, 防 git gc 回收。返回是否成功。"""
    import subprocess as _sp
    root = repo_root or config.PROJECT_ROOT
    ref = f"refs/qidian/pending/{task_id}"
    r = _sp.run(
        ["git", "update-ref", ref, commit_sha],
        cwd=str(root), capture_output=True, timeout=15,
    )
    if r.returncode != 0:
        from singularity.scheduler import witness
        witness.heartbeat('worktree', f'warn:anchor_ref {task_id[:8]}: {r.stderr[:100]}')
        return False
    return True


def _release_ref(task_id: str, repo_root=None) -> bool:
    """清理锚定 ref。返回是否成功。"""
    import subprocess as _sp
    root = repo_root or config.PROJECT_ROOT
    ref = f"refs/qidian/pending/{task_id}"
    r = _sp.run(
        ["git", "update-ref", "-d", ref],
        cwd=str(root), capture_output=True, timeout=15,
    )
    if r.returncode != 0:
        # 可能 ref 已不存在（被 gc 或已释放），不算错误
        return False
    return True


_MAX_WORKTREES = 50

def _maybe_create_worktree(task_id: str, level: str, agent_cfg: dict, snapshot_ref: str = "", repo_root=None):
    if agent_cfg.get("sandbox") != "worktree":
        return None
    # worktree 数量上限检查
    try:
        from ._git_worktree import _worktrees_dir
        wtd = _worktrees_dir()
        count = len(list(wtd.iterdir())) if wtd.exists() else 0
        if count >= _MAX_WORKTREES:
            from . import witness
            witness.heartbeat(task_id, f"worktree_limit:{count}>={_MAX_WORKTREES}")
            return None
    except Exception as e:
        witness.heartbeat('_worktree', f'warn:{e}')
    try:
        return wt_create(task_id, level, base_ref=snapshot_ref, repo_root=repo_root)  # 修复 #8
    except Exception:  # noqa: BLE001
        return None


def _cleanup_wt(wt) -> None:
    if wt is None:
        return
    _unlock_wt(wt)
    try:
        wt_cleanup(wt)
    except Exception as e:
        witness.heartbeat('_worktree', f'warn:{e}')


def _lock_wt(wt: Worktree) -> None:
    """只读锁: 文件 r--r--r--, 目录 r-xr-xr-x (防遍历但可进入子路径)。"""
    if wt is None:
        return
    import stat, subprocess as _sp
    r = _sp.run(["git", "ls-files"], cwd=str(wt.path), capture_output=True, text=True)
    if r.returncode != 0:
        return
    for f in r.stdout.strip().splitlines():
        fp = wt.path / f
        try:
            if fp.is_dir():
                fp.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0555
            elif fp.is_file():
                fp.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0444
        except OSError:
            pass


def _unlock_wt(wt: Worktree) -> None:
    """解锁: 文件 rw-r--r--, 目录 rwxr-xr-x。"""
    if wt is None:
        return
    import stat, subprocess as _sp
    r = _sp.run(["git", "ls-files"], cwd=str(wt.path), capture_output=True, text=True)
    if r.returncode != 0:
        return
    for f in r.stdout.strip().splitlines():
        fp = wt.path / f
        try:
            if fp.is_dir():
                fp.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0755
            elif fp.is_file():
                fp.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 0644
        except OSError:
            pass


