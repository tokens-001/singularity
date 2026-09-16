"""🔴 **F3：判失败但产物可打捞** —— 界面上要看得见（2026-09-17 真机）。

任务判 `failed` / 超时之后，产物**不一定丢**：executor 干完一轮会 `commit_wt`，
并把提交**锚在 `refs/qidian/pending/<task_id>`** 上（`_worktree._anchor_ref` 打的，
防 git gc 回收）。成功合并那条路会 `_release_ref` 删掉它
⇒ **ref 还在 = 这个任务有可打捞的产物**。

真机那轮：3 个任务全判 `failed`，产物好好躺在 pending ref 上（拼起来 `pytest 40 passed`）
—— 而**界面上一个字都不显示**，用户只看到"失败"。
"""
import subprocess

import pytest

from singularity.scheduler import _api_tasks


def _git_repo(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    def run(*a):
        return subprocess.run(["git", *a], cwd=str(d), capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (d / "a.txt").write_text("x", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return d


def test_能从项目仓里捞出_pending_ref(tmp_path, monkeypatch):
    """🔴 **要在项目仓里找，不是奇点仓** —— "读错仓库"这一族本仓踩过三次。"""
    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/pending/T1", sha],
                   cwd=str(repo), capture_output=True, text=True)

    from singularity.scheduler import _git_worktree
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])

    got = _api_tasks.salvageable_refs()
    assert got.get("T1") == sha, f"项目仓里的 pending ref 没被捞出来: {got}"


def test_没有_ref就什么都不报(tmp_path, monkeypatch):
    """**边界**：ref 不在 ⇒ 不许凭空说"有可打捞的产物"（那是另一种撒谎）。"""
    repo = _git_repo(tmp_path)
    from singularity.scheduler import _git_worktree
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])
    assert _api_tasks.salvageable_refs() == {}


def test_任务列表带上_salvage_ref(tmp_path, monkeypatch):
    """接线：捞出来的东西**真进了任务列表的载荷**（不然界面上还是看不见）。"""
    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/pending/T1", sha],
                   cwd=str(repo), capture_output=True, text=True)
    from singularity.scheduler import _git_worktree
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])
    monkeypatch.setattr(_api_tasks, "_list_all_tasks",
                        lambda: [{"id": "T1", "status": "failed", "description": "x",
                                  "_filename": "T1"}])

    payload, code = _api_tasks.task_list()
    assert code == 200
    row = next(r for r in payload["tasks"] if r["id"] == "T1")
    assert row.get("salvage_ref") == sha, f"载荷里没有 salvage_ref ⇒ 界面还是看不见: {row}"
