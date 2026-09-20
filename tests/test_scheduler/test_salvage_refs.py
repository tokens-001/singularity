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


# ═══════════════════════════════════════════════════════════════
# 「孤儿 pending ref」—— 上线把两件事分清（2026-09-20）
#
# 上面那套说的是"**任务还在**、产物也在"（挂到任务卡上看得见）。
# 这一套说的是另一种：**ref 还在，而任务文件已经没了** ⇒
# `salvageable_refs()` 那张表是**按 task_id 挂到任务行上**的，**没有行能挂** ⇒
# **盘上有一份、界面上一个字看不见**。真机此刻盘上是 **14 条**。
# ═══════════════════════════════════════════════════════════════

def test_孤儿是_ref在而任务文件没了(tmp_path, monkeypatch):
    """判据：`refs/qidian/pending/<task_id>` 在 ∧ `.qidian/tasks/<task_id>.json` 不在。

    变异：把 `orphan_refs` 里那句 `if tid not in have` 去掉 ⇒ 本条红
    （那样孤儿会退化成"所有 ref"，包括挂得上任务的）。
    """
    from singularity.scheduler import config, _git_worktree
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian" / "tasks").mkdir(parents=True)
    # 一个**有任务文件**的、一个**没有**的
    (tmp_path / ".qidian" / "tasks" / "T-alive.json").write_text("{}", encoding="utf-8")

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    for tid in ("T-alive", "T-gone"):
        subprocess.run(["git", "update-ref", f"refs/qidian/pending/{tid}", sha],
                       cwd=str(repo), capture_output=True, text=True)
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])

    assert set(_api_tasks.salvageable_refs()) == {"T-alive", "T-gone"}
    assert set(_api_tasks.orphan_refs()) == {"T-gone"}, "孤儿认错了"


def test_一条孤儿都没有时是空的(tmp_path, monkeypatch):
    """**边界**：都挂得上任务 ⇒ 孤儿 0 条。不许把正常的也报成孤儿。"""
    from singularity.scheduler import config, _git_worktree
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian" / "tasks").mkdir(parents=True)
    (tmp_path / ".qidian" / "tasks" / "T1.json").write_text("{}", encoding="utf-8")

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/pending/T1", sha],
                   cwd=str(repo), capture_output=True, text=True)
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])
    assert _api_tasks.orphan_refs() == {}


def test_孤儿只读不删(tmp_path, monkeypatch):
    """🔴 **命门**：`orphan_refs()` 是**只读**的 —— 它数出来的东西**一个都不许动**。

    清一条 = 永久删掉一份**还在**的产物，而「该不该留」的判据（任务文件）已经没了。
    ⇒ 只能人判，不能进任何自动清理。变异：在函数里加一句 `_release_ref` ⇒ 本条红。
    """
    from singularity.scheduler import config, _git_worktree
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian" / "tasks").mkdir(parents=True)

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/pending/T-gone", sha],
                   cwd=str(repo), capture_output=True, text=True)
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])

    assert _api_tasks.orphan_refs() == {"T-gone": sha}
    after = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)",
                            "refs/qidian/pending/"], cwd=str(repo),
                           capture_output=True, text=True).stdout.split()
    assert after == ["qidian/pending/T-gone"], f"数了一遍就把 ref 动了：{after}"


# ═══════════════════════════════════════════════════════════════
# `refs/qidian/salvaged/` —— **删任务时留下的**那批（2026-09-20）
#
# `task_delete` 换了桩（见 `_worktree._salvage_ref`）：待捞的产物不剪断，改挂到
# `salvaged/` 底下。那批**任务已经删了** ⇒ 同样"没有行能挂"，只能靠
# `delivery_facts.py --refs` 看。这里钉的是"扫得出来"，别让它变成第二个看不见。
# ═══════════════════════════════════════════════════════════════

def test_扫得出_salvaged_ref(tmp_path, monkeypatch):
    """**两个命名空间不许串** —— 扫 pending 的别把 salvaged 一起捞进来，反之亦然。

    变异：把 `salvaged_refs` 里的 `"salvaged"` 改成 `"pending"` ⇒ 本条红
    （那样"删任务留下的"和"任务还在的"会混成一批，界面上分不出哪批挂了活任务）。
    """
    from singularity.scheduler import _git_worktree
    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/pending/T-live", sha],
                   cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "update-ref", "refs/qidian/salvaged/T-deleted", sha],
                   cwd=str(repo), capture_output=True, text=True)
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])

    assert _api_tasks.salvageable_refs() == {"T-live": sha}, "pending 那批扫串了"
    assert _api_tasks.salvaged_refs() == {"T-deleted": sha}, "salvaged 那批扫串了"


def test_salvaged_扫出来是只读的(tmp_path, monkeypatch):
    """命门同「孤儿只读不删」：数一遍**不许动盘**。"""
    from singularity.scheduler import _git_worktree
    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/qidian/salvaged/T1", sha],
                   cwd=str(repo), capture_output=True, text=True)
    monkeypatch.setattr(_git_worktree, "_project_repo_roots", lambda: [repo])

    assert _api_tasks.salvaged_refs() == {"T1": sha}
    after = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)",
                            "refs/qidian/salvaged/"], cwd=str(repo),
                           capture_output=True, text=True).stdout.split()
    assert after == ["qidian/salvaged/T1"], f"数了一遍就把 ref 动了：{after}"


def test_账本半行坏掉不炸掉整份报告(tmp_path, monkeypatch):
    """`salvaged.jsonl` 是 append-only 的 —— 写到一半被打断会留半行。

    半行只该让**那一条**没有线索，不该让 `delivery_facts.py --refs` 整个抛掉
    （那正是"报现场的工具自己先炸了"）。
    """
    import sys
    from pathlib import Path

    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    (tmp_path / ".qidian").mkdir()
    (tmp_path / ".qidian" / "salvaged.jsonl").write_text(
        '{"task_id": "T1", "repo": "/x", "sha": "abc", "description": "d", "status": "failed"}\n'
        '{"task_id": "T2", "repo": "/x", "sh\n',      # ← 半行
        encoding="utf-8")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import delivery_facts

    clues = delivery_facts._salvaged_clues()
    assert clues["T1"]["description"] == "d"
    assert "T2" not in clues, "半行被当成线索用了"
