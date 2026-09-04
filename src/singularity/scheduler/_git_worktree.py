from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from singularity.scheduler import config


@dataclass
class Worktree:
    path: Path
    name: str
    baseline_ref: str  # worktree 创建时的主仓库 HEAD
    repo_root: Path = None  # 所属仓库根 (修复 #1: cleanup 需从所属 repo 移除)


@dataclass
class MergeResult:
    ok: bool
    conflicts: list[str] = field(default_factory=list)
    merged_ref: str = ""
    reason: str = ""  # 失败原因: dirty_workspace / conflict / no_changes / error


_GIT_TIMEOUT = 60  # seconds — git operations should never hang indefinitely


def _run(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, -1, stdout="", stderr=f"timed out after {timeout}s")


def _git_dir() -> Path:
    return config.PROJECT_ROOT / ".git"


def _worktrees_dir() -> Path:
    d = config.QIDIAN_DIR / "worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _head_ref(repo_root: Path = None) -> str:
    root = repo_root or config.PROJECT_ROOT
    r = _run(["rev-parse", "HEAD"], root)
    return r.stdout.strip() if r.returncode == 0 else ""


def create(task_id: str, agent_level: str, base_ref: str = "", repo_root: Path = None) -> Worktree:
    root = repo_root or config.PROJECT_ROOT
    name = f"{task_id}_{agent_level}"
    wt_path = _worktrees_dir() / name

    # 幂等: 已存在同名 worktree → 返回已有的
    existing = _run(["worktree", "list", "--porcelain"], root)
    if existing.returncode == 0:
        for line in existing.stdout.splitlines():
            if line.startswith("worktree ") and line.split(maxsplit=1)[1] == str(wt_path):
                return Worktree(path=wt_path, name=name, baseline_ref=_head_ref(root), repo_root=root)

    baseline = base_ref if base_ref else _head_ref(root)
    # --detach 从指定 ref 建分离 worktree (未指定则当前 HEAD)
    r = _run(
        ["worktree", "add", "--detach", str(wt_path), baseline],
        root,
    )
    if r.returncode != 0:
        raise RuntimeError(f"worktree add 失败: {r.stderr.strip()}")

    return Worktree(path=wt_path, name=name, baseline_ref=baseline, repo_root=root)


def cleanup(wt: Worktree) -> bool:
    root = wt.repo_root or config.PROJECT_ROOT  # 修复 #1: 从所属 repo 移除, 否则项目 worktree 变孤儿
    # git worktree remove --force
    r = _run(["worktree", "remove", "--force", str(wt.path)], root)
    orphan = False
    if r.returncode != 0:
        stderr = (r.stderr or "").lower()
        # 孤儿 worktree: .git/worktrees/ 元数据丢了, git 不认
        if "not a working tree" in stderr:
            orphan = True
        else:
            # 兜底: prune 后再试
            _run(["worktree", "prune"], root)
            r = _run(
                ["worktree", "remove", "--force", str(wt.path)], root
            )
            if r.returncode != 0:
                stderr2 = (r.stderr or "").lower()
                if "not a working tree" in stderr2:
                    orphan = True
    if orphan:
        # 元数据丢了 → 把残留 git 文件也删了, 否则 rmtree 遇到 .git 文件报错
        _forcibly_remove_tree(wt.path)
        return not wt.path.exists()
    # 删残留目录 (worktree remove 通常已删, 保险)
    if wt.path.exists():
        import shutil
        shutil.rmtree(wt.path, ignore_errors=True)
    # rmtree 可能因权限问题残留 → 暴力清
    if wt.path.exists():
        _forcibly_remove_tree(wt.path)
    return not wt.path.exists()


def _forcibly_remove_tree(p: "Path") -> None:
    """chmod -R u+rwx 再 rmtree, 处理 agent 产出目录权限问题 (修复 #13)。

    不用 os.walk 因为缺 execute 位的目录 os.scandir 进不去。
    失败后 chmod 0700 防敏感文件残留 0777 被同机用户读取。
    """
    import shutil, subprocess as _sp
    _sp.run(["chmod", "-R", "u+rwx", str(p)], capture_output=True)
    try:
        shutil.rmtree(str(p), ignore_errors=False)
    except Exception:
        try:
            shutil.rmtree(str(p), ignore_errors=True)
        finally:
            # ponytail: rmtree 失败后收回权限, 防 0777 残留
            _sp.run(["chmod", "-R", "go-rwx", str(p)], capture_output=True)


def _wt_head(wt: Worktree) -> str:
    """worktree 里 agent 改动后的 commit SHA (要 merge 回来的 ref)。"""
    r = _run(["rev-parse", "HEAD"], wt.path)
    return r.stdout.strip() if r.returncode == 0 else ""


def _has_changes_in_wt(wt: Worktree) -> bool:
    """worktree 是否有未 commit 的改动。"""
    r = _run(["status", "--porcelain"], wt.path)
    return bool(r.stdout.strip())


def commit_wt(wt: Worktree) -> str:
    """把 worktree 里未提交改动 commit 掉, 返回新 commit SHA。

    修复 #2: v3 路径绕过 merge_back (里面有 add -A + commit), 直接取 HEAD
    拿到空 baseline。抽出此原语, v3 先 commit_wt 拿到含改动的 commit,
    再构造 MergeRequest。
    """
    if _has_changes_in_wt(wt):
        _run(["add", "-A"], wt.path)
        _run(["commit", "-m", f"agent changes in {wt.name}"], wt.path)
    return _wt_head(wt)


def _do_merge(src_ref: str, onto: str, merge_msg: str, repo_root: Path = None) -> MergeResult:
    """合并原语 (修复 #11): merge_back 和 merge_ref 共用。

    主工作区有改动时自动 stash，merge 后 pop 恢复。
    """
    root = repo_root or config.PROJECT_ROOT
    # -uno 排除 untracked 文件 (如 .qidian/ .claude/ 等), 只检查 tracked 文件是否脏
    status_r = _run(["status", "--porcelain", "-uno"], root)
    stashed = False
    if status_r.stdout.strip():
        # ponytail: auto-stash, merge, pop — 避免每次开发都得先 commit
        stash_r = _run(["stash", "push", "-m", "auto-stash before merge"], root)
        if stash_r.returncode != 0:
            return MergeResult(ok=False, reason="工作区不干净且 stash 失败, 拒绝 merge")
        stashed = True

    target_ref = _run(["rev-parse", onto], root)
    if target_ref.returncode != 0:
        if stashed:
            _run(["stash", "pop"], root)
        return MergeResult(ok=False, reason=f"目标分支不存在: {onto}")

    try:
        r = _run(["merge", "--no-commit", "--no-ff", src_ref], root)
        diff = _run(["diff", "--name-only", "--diff-filter=U"], root)
        conflicts = [f for f in diff.stdout.strip().splitlines() if f]

        if conflicts or r.returncode != 0:
            _run(["merge", "--abort"], root)
            reason = "冲突" if conflicts else f"merge 失败: {r.stderr.strip()[:120]}"
            return MergeResult(ok=False, conflicts=conflicts, reason=reason)

        commit_r = _run(["commit", "-m", merge_msg], root)
        if commit_r.returncode != 0:
            # ponytail: git commit 非零可能因为 nothing-to-commit，HEAD 未变
            return MergeResult(ok=False, reason=f"commit 失败: {commit_r.stderr.strip()[:120]}")
        return MergeResult(ok=True, merged_ref=_head_ref(root))
    finally:
        if stashed:
            # ponytail: merge 后恢复开发者未提交的改动
            pop_r = _run(["stash", "pop"], root)
            if pop_r.returncode != 0:
                # 冲突了: 保留 stash, 任务产出优先
                _run(["stash", "apply", "--index"], root)
                _run(["checkout", "--theirs", "."], root)
                _run(["stash", "drop"], root)


def merge_back(wt: Worktree, onto: str = "", repo_root: Path = None) -> MergeResult:
    """worktree 产出合并回主仓库 (v2 路径)。"""
    # worktree 里若有未提交改动, 先 commit
    src_ref = commit_wt(wt)
    if not src_ref:
        return MergeResult(ok=False, reason="worktree 无 HEAD 引用")
    target = onto if onto else "main"
    return _do_merge(src_ref, target, f"merge {wt.name} ({src_ref[:8]})", repo_root=repo_root)


def merge_ref(src_ref: str, onto: str = "main", repo_root: Path = None) -> MergeResult:
    """把任意 commit ref 合并到 onto (供 MergeQueue.drain 用)。

    src_ref 必须是已 commit 的稳定 ref (v3 路径 run() 已调 commit_wt)。
    """
    return _do_merge(src_ref, onto, f"merge {src_ref[:8]} via queue", repo_root=repo_root)


def merge_tree_probe(base_ref: str, ours_ref: str, theirs_ref: str, repo_root: Path = None) -> tuple[bool, list[str]]:
    """三方合并预探测 (不碰工作树)。

    修复 #11: 区分"真冲突"和"命令错误"。
    - 干净 → (True, [])
    - 真冲突 (退出码 1, 输出含冲突文件) → (False, [冲突文件])
    - 命令错误 (ref 不存在等) → (False, []) + 调用方按空冲突+非零判定
    """
    root = repo_root or config.PROJECT_ROOT
    # git 2.38+: --merge-base=<ref> 必须等号式, 且选项在位置参之前
    r = _run(
        ["merge-tree", "--write-tree", "--name-only", f"--merge-base={base_ref}", ours_ref, theirs_ref],
        root,
    )
    if r.returncode == 0:
        return True, []
    # 命令错误: stderr 有内容且 stdout 无文件路径 → 不是真冲突
    if not r.stdout.strip() and r.stderr.strip():
        return False, []
    # 真冲突: 解析 stdout 冲突文件 (merge-tree --name-only 输出路径列表)
    conflicts = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("<<") or line.startswith("CONFLICT"):
            continue
        if "/" in line or "." in line:
            conflicts.append(line)
    return False, conflicts


def cleanup_orphans() -> int:
    """启动时清理上次崩溃残留的孤儿 worktree 和 pending refs。

    Returns: 清理的孤儿数量。
    """
    import shutil
    cleaned = 0

    # 1. 清理 git worktree 注册表中的孤儿条目
    wt_dir = _git_dir() / "worktrees"
    if wt_dir.exists():
        _run(["worktree", "prune"], config.PROJECT_ROOT, timeout=30)

    # 2. 清理磁盘上的孤儿 worktree 目录
    qidian_wt = config.QIDIAN_DIR / "worktrees"
    if qidian_wt.exists():
        for d in qidian_wt.iterdir():
            if not d.is_dir():
                continue
            # 检查 git 是否还认得这个 worktree
            r = _run(["worktree", "list", "--porcelain"], config.PROJECT_ROOT)
            if str(d) not in r.stdout:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned += 1
                except Exception:
                    pass

    # 3. 清理孤儿 git refs (refs/qidian/pending/*)
    import subprocess as _sp
    pending_refs = _sp.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/qidian/pending/"],
        cwd=str(config.PROJECT_ROOT), capture_output=True, text=True, timeout=30,
    )
    if pending_refs.returncode == 0:
        for ref in pending_refs.stdout.strip().splitlines():
            if ref:
                _sp.run(["git", "update-ref", "-d", ref],
                        cwd=str(config.PROJECT_ROOT), capture_output=True, timeout=10)

    return cleaned


def changed_files_between(base_ref: str, branch_ref: str, repo_root: Path = None) -> list[str]:
    """git diff base..branch --name-only, 供 MergeRequest 构造 changed_files。"""
    root = repo_root or config.PROJECT_ROOT
    r = _run(["diff", "--name-only", base_ref, branch_ref], root)
    if r.returncode != 0:
        return []
    return [f for f in r.stdout.strip().splitlines() if f]
