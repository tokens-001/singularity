"""改动文件采集的判据：**必须跟执行前的基线比，不能裸 `git status` / 裸 `git diff`**。

防御模式 §55 这个形状已经踩过三次（`validator._diff_base`、`_salvage_timed_out`、
executor 这层）。坏法每次一样：**agent 干完一轮自己 `git commit`**（`git commit`
不在 `base.py._BLOCKED_COMMANDS` 里，拦不住），提交之后工作区干净 ⇒
"干完了并提交了"和"什么都没干"长得一模一样。

后果不是"少报几个文件"这么轻 —— `_exec` 那句 `if changed:` 为假会**把整条
审查 / QA / 安全审计跳过**。

用**真 git** 测（跟 `test_review_gate.py` 一个路子）：桩测试测不出这个时序。
"""
import subprocess
from pathlib import Path

import pytest


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    """一个真 git 仓库：一个初始提交，返回 (路径, 初始 commit sha)。"""
    d = tmp_path / "wt"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "init")
    return d, _git(d, "rev-parse", "HEAD").strip()


def _make_executor(cls, cwd, baseline_ref):
    """子类绕开真 __init__（要 cfg/task，跟这条判据无关）。"""
    e = cls.__new__(cls)
    e._cwd = cwd
    e.cwd = cwd
    e.baseline_ref = baseline_ref
    e._changed_files = []
    return e


def test_openai_sees_committed_changes(repo):
    """正题：agent 自己 commit 过的改动，仍然要出现在 changed_files 里。

    裸 `git status --porcelain` 在这里会返回空 —— 就是原来那个 bug。
    """
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor as E

    d, base = repo
    (d / "new_feature.py").write_text("print('hi')\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "agent changes in T1")      # ← agent 自己提交

    # 先确认裸 status 确实看不见（证明这个用例不是白测）
    assert _git(d, "status", "--porcelain").strip() == "", "前提不成立：工作区不干净"

    ex = _make_executor(E, d, base)
    ex._track_changed_files()
    assert "new_feature.py" in ex._changed_files, (
        f"提交过的改动没被采集到 —— 判据又退回裸 status 了：{ex._changed_files}")


def test_openai_sees_uncommitted_changes(repo):
    """没提交的也要看得见（原来靠 status 覆盖的那部分，别改坏了）。"""
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor as E

    d, base = repo
    (d / "wip.py").write_text("y = 2\n", encoding="utf-8")

    ex = _make_executor(E, d, base)
    ex._track_changed_files()
    assert "wip.py" in ex._changed_files, ex._changed_files


def test_openai_still_filters_pycache(repo):
    """`__pycache__` / `.pyc` 是构建产物，不算交付文件 —— 老行为别丢。"""
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor as E

    d, base = repo
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")

    ex = _make_executor(E, d, base)
    ex._track_changed_files()
    assert not [f for f in ex._changed_files if "__pycache__" in f or f.endswith(".pyc")], \
        ex._changed_files


def test_shared_helper_reports_baseline_degradation(repo, monkeypatch):
    """拿不到基线时要**出声** —— 静默降级正是这个 bug 的一半。

    `_git_changed_files` 在 baseline_ref 为空时只能跟 HEAD 比，
    那时**已提交的改动看不见**。降级可以发生，但不能悄没声。
    """
    from singularity.scheduler import witness
    from singularity.scheduler.executors.claude_cli import _git_changed_files

    seen = []
    monkeypatch.setattr(witness, "warn",
                        lambda *a, **k: seen.append((a, k)))

    d, base = repo
    (d / "new_feature.py").write_text("print('hi')\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "agent changes in T1")

    files = _git_changed_files("", str(d))          # 没基线
    assert "new_feature.py" not in files, "前提不成立：没基线时本来就该看不见已提交的"
    assert any("no_baseline_ref" in str(a) for a, _ in seen), \
        f"降级了却没告警：{seen}"
