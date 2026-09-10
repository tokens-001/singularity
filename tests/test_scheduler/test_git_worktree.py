"""`_git_worktree.merge_tree_probe` 的冲突解析 —— 用真 git 仓库验，不能打桩。

`git merge-tree --write-tree --name-only` 的输出是：
    第 1 行 = 写入的 tree OID
    随后若干行 = 冲突文件名
    空行
    之后是人类可读消息块（"Auto-merging X" / "CONFLICT (...): ..."）

原先靠 `"/" in line or "." in line` 滤掉 tree OID，实测在真仓库上错两处：
根目录下无扩展名的冲突文件被滤掉；"Auto-merging xxx.md" 被当成文件名。
"""
import subprocess

import pytest


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)


@pytest.fixture
def conflict_repo(tmp_path):
    """base → 两条分支各自改同一个无扩展名文件 → 必然冲突。"""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "Makefile").write_text("base\n")
    (r / "README.md").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD").stdout.strip()

    _git(r, "checkout", "-qb", "feat")
    (r / "Makefile").write_text("feat\n")
    (r / "README.md").write_text("feat\n")
    _git(r, "commit", "-qam", "feat")
    theirs = _git(r, "rev-parse", "HEAD").stdout.strip()

    _git(r, "checkout", "-q", "-")           # 回原分支
    (r / "Makefile").write_text("ours\n")
    (r / "README.md").write_text("ours\n")
    _git(r, "commit", "-qam", "ours")
    ours = _git(r, "rev-parse", "HEAD").stdout.strip()
    return r, base, ours, theirs


def test_reports_root_level_extensionless_conflict(conflict_repo):
    """Makefile 这种没有扩展名的根文件必须被报出来。

    漏掉它的后果不只是列表少一项：若它是**唯一**冲突，conflict_files 为空 →
    `_drain_one` 判成"merge probe 命令错误" → 任务标 **failed 终态**，
    而不是 parking 等人工解决 —— 任务直接死掉。
    """
    from singularity.scheduler._git_worktree import merge_tree_probe
    root, base, ours, theirs = conflict_repo
    clean, files = merge_tree_probe(base, ours, theirs, repo_root=root)

    assert not clean
    assert "Makefile" in files, f"根目录无扩展名的冲突文件被漏掉: {files}"
    assert "README.md" in files, files


def test_does_not_leak_progress_messages_into_conflict_list(conflict_repo):
    """"Auto-merging xxx.md" / "CONFLICT (...)" 是消息，不是文件名。"""
    from singularity.scheduler._git_worktree import merge_tree_probe
    root, base, ours, theirs = conflict_repo
    _clean, files = merge_tree_probe(base, ours, theirs, repo_root=root)

    bad = [f for f in files if f.startswith(("Auto-merging", "CONFLICT"))]
    assert not bad, f"消息行混进冲突列表: {bad}"
    # tree OID 也不能混进来（它没有扩展名也没有 /，老写法靠这两点误打误撞滤掉）
    assert not any(len(f) >= 40 and all(c in "0123456789abcdef" for c in f) for f in files), files


def test_empty_conflict_list_means_command_error(tmp_path):
    """ref 不存在 → 命令错误 → 必须返回空列表（调用方据此判 failed，而不是 parking）。"""
    from singularity.scheduler._git_worktree import merge_tree_probe
    r = tmp_path / "repo2"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.py").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")

    clean, files = merge_tree_probe("deadbeefdeadbeef", "HEAD", "HEAD", repo_root=r)
    assert clean is False and files == [], files
