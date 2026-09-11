"""超时被杀的的任务：痕迹和账不能一起消失。

2026-09-11 探路轮实测：3 个任务各撞 900s 上限被杀，trace 里是
`changed_files: [] / elapsed: 0 / token_count: 0` —— **看上去"什么都没干"**。
而磁盘上 worktree 里文件是写全的（wc_lite.py + 测试 + README）。

两处都改：
· `orchestrator._salvage_timed_out` 抢救已知事实（改了哪些文件、实际跑了多久）
· `neijinglu` 里"取不到用量"记 **None** 而不是 0 —— 0 是"没花钱"，None 是"不知道"，
  这两个必须分得开（§44 承诺类字段三态可分）。把"不知道"写成 0，
  等于让最需要排查的场景留下一份**看起来正常**的假账。
"""
import subprocess

from singularity.scheduler import orchestrator as orch


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _mk_worktree(tmp_path, task_id):
    """造一个"跑了半截"的 worktree：有未提交的新文件。"""
    repo = tmp_path / "proj"
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    wtroot = tmp_path / ".proj-worktrees"
    wt = wtroot / f"{task_id}_any"
    wt.mkdir(parents=True, exist_ok=True)
    _git(wt, "init", "-q")
    (wt / "wc_lite.py").write_text("print(1)\n")
    (wt / "README.md").write_text("用法\n")
    return repo, wtroot


def test_salvage_recovers_changed_files(tmp_path, monkeypatch):
    """超时 trace 必须看得出"它改了哪些文件"，而不是一个空列表。"""
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import _git_worktree as gw

    repo, wtroot = _mk_worktree(tmp_path, "task-1")
    monkeypatch.setattr(proj_mod, "repo_root_for", lambda t: repo)
    monkeypatch.setattr(gw, "_worktrees_dir", lambda root=None: wtroot)

    task = type("T", (), {"id": "task-1"})()
    got = orch._salvage_timed_out(task, 903.5)

    assert got is not None, "抢救失败 → trace 又会是一份空白"
    files = got.executor_result.changed_files
    assert "wc_lite.py" in files and "README.md" in files, files


def test_salvage_marks_usage_unknown_not_zero(tmp_path, monkeypatch):
    """用量**取不到就留 None**，不许写成 0（0 = "没花钱"，是假账）。"""
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import _git_worktree as gw

    repo, wtroot = _mk_worktree(tmp_path, "task-2")
    monkeypatch.setattr(proj_mod, "repo_root_for", lambda t: repo)
    monkeypatch.setattr(gw, "_worktrees_dir", lambda root=None: wtroot)

    task = type("T", (), {"id": "task-2"})()
    er = orch._salvage_timed_out(task, 900.0).executor_result

    assert er.token_count is None, "0 会被读成'这个任务没花钱'"
    assert er.elapsed == 900.0, "跑了多久是**确定**的，不能丢"
    assert "超时" in er.raw_output


def test_salvage_never_raises(tmp_path, monkeypatch):
    """抢救本身不许把调度循环带崩 —— 取不到就返回 None，走老路。"""
    from singularity.scheduler import project as proj_mod

    def boom(t):
        raise RuntimeError("repo 没了")

    monkeypatch.setattr(proj_mod, "repo_root_for", boom)
    task = type("T", (), {"id": "task-3"})()
    assert orch._salvage_timed_out(task, 900.0) is None


def test_report_says_unknown_when_no_executor_result():
    """`build_report` 拿不到 executor_result 时，用量记 None 而不是 0。"""
    from singularity.scheduler.neijinglu import build_report

    class _V:
        verdict, action, validate_verdict = "阻断", "abort", ""
        validate_reason, gate_passed, gate_message = "超时", None, ""
        turns_used = 0
        gate_required = False
        unverified = ["未执行验证（worker 异常 / 超时）"]

    rep = build_report(
        task="t",
        route=type("R", (), {"gate_required": False, "task_type": "default",
                             "matched_signals": []})(),
        executor_result=None, validation=_V(),
        snapshot=type("S", (), {"id": "s1", "method": "git", "ref": "abc"})(),
    )
    d = rep.to_dict() if hasattr(rep, "to_dict") else rep
    assert d["token_count"] is None, "0 是'没花钱'，None 才是'不知道'"
    assert d["elapsed"] is None
