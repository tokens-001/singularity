"""内部模块 — worktree 生命周期管理。

创建/清理/加锁/解锁/git ref 锚定。叶子模块，不依赖其他新模块。
"""

from __future__ import annotations

import os
import stat
import subprocess as _sp
from singularity.scheduler import config
from singularity.scheduler import tracker as _tracker
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
        witness.warn('worktree', f'anchor_ref {_tracker.short_id(task_id)}: {r.stderr[:100]}')
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


def cleanup_task_artifacts(task_id: str, repo_root) -> int:
    """清任务衍生残留 (patch/snapshot/worktree/标记/累计用量)，不动任务本体 json。返回删除数。

    从 _api_tasks 下沉到此，供 orchestrator 终态清理复用（避免循环 import）。

    🔴 **这里不碰锚定 ref**（2026-09-18 改）。原来最后一步是 `_release_ref` ——
    而那个 ref 的语义**只有一种读法**，写在 `_api_tasks.salvageable_refs` 的 docstring 里：

        成功合并那条路会 `_release_ref` 删掉它 ⇒ **ref 还在 = 这个任务有可打捞的产物**

    也就是说 **释放 = 断言"这个任务的产物已经安全进项目仓了"**。而"清临时残留"跟
    "产物进没进仓"是两件毫不相干的事，混在一起就是**把断言藏在清垃圾里**。

    实测代价（2026-09-18 02:1x）：删 21 个任务 ⇒ **3 个"判失败但产物还在"的锚当场没了**
    （`1789658497832` / `1789658497834` / `1789662504534`，靠 git 还没 gc 才按 SHA 捞回来）。
    ⚠️ **同一形状不止删任务这一处**：超时 / worker 异常 / 合并冲突那三条路也松过手 ——
    而它们恰恰是"产物在、只是没进仓"最多的地方（09-18 那天 62 次被 240s 掐断全走这条）。
    ⇒ 释放改由**知道产物落没落地**的调用方显式做，见 `_api_tasks.task_delete` /
    `task_retry` / `orchestrator._drain_pending`。
    """
    from singularity.scheduler import witness
    from singularity.scheduler._git_worktree import _worktrees_dir
    deleted = 0

    def _rm(p) -> None:
        nonlocal deleted
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except Exception as e:
            witness.warn('_api', f'del:{e}')

    # E+ patch 暂存 (.md)
    _rm(config.PATCH_DIR / f"{task_id}.md")
    _rm(config.PATCH_DIR / f"{task_id}_plan.md")

    # 取消/暂停标记 (超时协作中断可能残留, 未消费则误判后续 retry)
    _rm(config.CANCEL_DIR / f"{task_id}.json")
    _rm(config.PAUSE_DIR / f"{task_id}.json")

    # 执行中的累计用量 —— 只在"超时那条路"被读走，正常跑完的任务不会消费它。
    # 不清就是每个任务留一个孤儿文件（§59 的账是补上了，但垃圾别留下）。
    _rm(config.PARTIAL_USAGE_DIR / f"{task_id}.json")

    # snapshot ({ts}_{task_id}.json)
    for p in config.SNAPSHOT_DIR.glob(f"*_{task_id}.json"):
        _rm(p)

    # worktree ({task_id}_{level} 目录) — 复用 cleanup 处理 git 元数据/孤儿/权限
    try:
        for wt_path in _worktrees_dir(repo_root).glob(f"{task_id}_*"):
            if wt_path.is_dir():
                wt_cleanup(Worktree(path=wt_path, name=wt_path.name,
                                    baseline_ref="", repo_root=repo_root))
                if not wt_path.exists():
                    deleted += 1
    except Exception as e:
        witness.warn('_api', f'wt_del:{e}')

    # ⚠️ 锚定 ref **不在这里释放** —— 见本函数 docstring。调用方要释放得自己显式调。
    return deleted


_MAX_WORKTREES = 50

def _maybe_create_worktree(task_id: str, level: str, agent_cfg: dict, snapshot_ref: str = "", repo_root=None):
    from . import witness  # 函数内 import 会让名字整个作用域变局部, 必须在 try 之前
    if agent_cfg.get("sandbox") != "worktree":
        return None
    # worktree 数量上限检查
    try:
        from ._git_worktree import _worktrees_dir
        wtd = _worktrees_dir(repo_root)
        count = len(list(wtd.iterdir())) if wtd.exists() else 0
        if count >= _MAX_WORKTREES:
            # 走 warn 不走 heartbeat：heartbeat 的第二参数是 agent_level，把消息塞进去会被
            # 存成一个伪 level（status 还是 "running"），任务到终态时又被清理逻辑 unlink
            # —— 等于没记，还让状态面板的"运行中"虚高。
            # 而且这里是**真降级**：拿不到 worktree → 调用方直接用仓库根跑（_exec.py:317，无沙箱）。
            witness.warn("worktree",
                         f"worktree_limit_reached:{count}>={_MAX_WORKTREES},"
                         f"unsandboxed:{_tracker.short_id(task_id)}"[:200])
            return None
    except Exception as e:
        witness.warn('_worktree', f'{e}')
    try:
        return wt_create(task_id, level, base_ref=snapshot_ref, repo_root=repo_root)  # 修复 #8
    except Exception as e:  # noqa: BLE001
        # 原来这里是裸 `return None` —— 连一条 warn 都没有。这是**真降级**:
        # 调用方 _exec.py 拿不到 wt 就用仓库根当 cwd(无沙箱), 且 _exec 的
        # `elif wt:` 不成立 → 不构造 MergeRequest → 改动落在真仓库、不进合并队列。
        # 最常走的触发路径: 残留 worktree 目录 → `git worktree add` rc=128。
        witness.warn("worktree", f"wt_create_failed:{type(e).__name__}:{e}:unsandboxed:{_tracker.short_id(task_id)}"[:200])
        return None


def _cleanup_wt(wt) -> None:
    from . import witness  # 原缺: except 分支调 witness 会 NameError, 把"清理失败"变成任务崩溃
    if wt is None:
        return
    _unlock_wt(wt)
    try:
        wt_cleanup(wt)
    except Exception as e:
        witness.warn('_worktree', f'{e}')


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


