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


def _mk_committed_worktree(tmp_path, task_id):
    """造一个"干完了并提交了"的 worktree —— 这是原来看不见的那种。

    ⚠️ agent 跑完一轮会自己 `commit_wt`（"agent changes in <taskid>"），
    提交之后 `git status` 干净 —— 只跑 `git status --porcelain` 的话，
    **"干完了并提交了"和"什么都没干"长得一模一样**。
    """
    repo = tmp_path / "proj2"
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()

    wtroot = tmp_path / ".proj2-worktrees"
    wtroot.mkdir(parents=True, exist_ok=True)
    wt = wtroot / f"{task_id}_any"
    # **必须是真 worktree**（共享对象库），否则基准 ref 在它里面解析不了 ——
    # 这跟线上一致：任务跑在自己的 worktree 里，基准来自主仓的快照。
    subprocess.run(["git", "worktree", "add", "-q", "-b", f"wt_{task_id}",
                    str(wt), "HEAD"], cwd=str(repo), check=True,
                   capture_output=True, text=True)
    (wt / "test_txtstat.py").write_text("def test_a():\n    assert 1\n")
    (wt / "txtstat.py").write_text("print(1)\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"agent changes in {task_id}_any")
    return repo, wtroot, base


class TestCommittedWorkIsNotInvisible:
    """2026-09-12 探路2 T2 实测的那条：**干完并提交了，trace 里却什么都没。**"""

    def _setup(self, tmp_path, monkeypatch):
        from singularity.scheduler import project as proj_mod
        from singularity.scheduler import _git_worktree as gw
        repo, wtroot, base = _mk_committed_worktree(tmp_path, "task-c")
        monkeypatch.setattr(proj_mod, "repo_root_for", lambda t: repo)
        monkeypatch.setattr(gw, "_worktrees_dir", lambda root=None: wtroot)
        return type("T", (), {"id": "task-c"})(), base

    def test_with_snapshot_sees_committed_files(self, tmp_path, monkeypatch):
        task, base = self._setup(tmp_path, monkeypatch)
        snap = type("S", (), {"ref": base, "method": "git"})()
        er = orch._salvage_timed_out(task, 901.0, snap).executor_result
        assert "test_txtstat.py" in er.changed_files
        assert "txtstat.py" in er.changed_files
        assert er.new_commits and "agent changes" in er.new_commits[0]
        assert "未合并" in er.raw_output

    def test_without_snapshot_misses_them(self, tmp_path, monkeypatch):
        """对照组 —— 这就是 T2 当时的样子（只看得见未提交的，于是什么都没有）。"""
        task, _ = self._setup(tmp_path, monkeypatch)
        er = orch._salvage_timed_out(task, 901.0).executor_result
        assert "test_txtstat.py" not in er.changed_files, "旧行为正是漏掉它的原因"

    def test_copy_type_snapshot_falls_back_and_says_so(self, tmp_path, monkeypatch):
        """copy 型快照的 ref 是目录不是 git ref → 退回旧行为，但**如实标注**。"""
        task, _ = self._setup(tmp_path, monkeypatch)
        snap = type("S", (), {"ref": "/tmp/somewhere", "method": "copy"})()
        er = orch._salvage_timed_out(task, 901.0, snap).executor_result
        assert "拿不到执行前基准" in er.raw_output, "看不全就得说看不全"


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


class TestTimeoutAlsoIndexesIntoMemory:
    """超时的任务**记忆那侧也要进** —— §55 只修了 trace 侧。

    2026-09-12 探路2 实测：T2 写完 373 行测试 + 完整实现、T3 写完计数核，
    两个都超时被杀 —— trace 里捞得回来，但 `events.json` 里**轨迹是 0 字**，
    因为 `index_task` 走的是 `_exec.py` 那条正常收尾路径，被 deadline 砍掉就整个跳过。
    **干完了但超时的经验，永远进不了记忆。**
    """

    def test_timeout_calls_index_task(self, tmp_path, monkeypatch):
        from concurrent.futures import Future

        from singularity.scheduler import memory as mem

        class _ER:
            changed_files = ["a.py", "test_a.py"]
            raw_output = "(超时摘要)"

        class _Disp:
            executor_result = _ER()

        monkeypatch.setattr(orch, "_salvage_timed_out", lambda *a, **k: _Disp())
        monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
        monkeypatch.setattr(orch, "_release_ref", lambda *a, **k: None)
        monkeypatch.setattr(orch.config, "CANCEL_DIR", tmp_path)
        monkeypatch.setattr(orch.config, "ensure_dirs", lambda: None)
        monkeypatch.setattr(orch.tracker, "transition", lambda *a, **k: None)
        # `_reap_futures` 开头会"等第一个 future 完成，最多 10s"——假 future 永远
        # 不完成，桩掉它，否则每个用例白等 10 秒（纯测试开销，不是产品行为）。
        monkeypatch.setattr(orch, "wait", lambda *a, **k: None)

        seen = {}
        monkeypatch.setattr(mem, "index_task", lambda **kw: seen.update(kw))

        t = type("T", (), {"id": "t1", "description": "任务", "depends_on": [],
                           "created_at": 1.0})()
        fut = Future()
        running = {fut: (t, None, None, None, 0.0)}   # submitted_at=0 → 早过 deadline
        orch._reap_futures(running, {}, None, None, [])

        assert seen.get("task_id") == "t1", "超时的任务也要进记忆"
        assert seen.get("changed_files") == ["a.py", "test_a.py"]
        assert seen.get("force") is True, "超时条目要留下，别被 Jaccard 去重吃掉"

    def test_index_task_failure_does_not_break_reap(self, tmp_path, monkeypatch):
        """记忆写失败不许把回收带崩 —— 但要有痕（witness.warn）。"""
        from concurrent.futures import Future

        from singularity.scheduler import memory as mem

        class _Disp:
            executor_result = type("E", (), {"changed_files": [], "raw_output": ""})()

        monkeypatch.setattr(orch, "_salvage_timed_out", lambda *a, **k: _Disp())
        monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
        monkeypatch.setattr(orch, "_release_ref", lambda *a, **k: None)
        monkeypatch.setattr(orch.config, "CANCEL_DIR", tmp_path)
        monkeypatch.setattr(orch.config, "ensure_dirs", lambda: None)
        monkeypatch.setattr(orch.tracker, "transition", lambda *a, **k: None)
        # `_reap_futures` 开头会"等第一个 future 完成，最多 10s"——假 future 永远
        # 不完成，桩掉它，否则每个用例白等 10 秒（纯测试开销，不是产品行为）。
        monkeypatch.setattr(orch, "wait", lambda *a, **k: None)

        def boom(**kw):
            raise RuntimeError("记忆挂了")
        monkeypatch.setattr(mem, "index_task", boom)

        t = type("T", (), {"id": "t2", "description": "x", "depends_on": [],
                           "created_at": 1.0})()
        running = {Future(): (t, None, None, None, 0.0)}
        orch._reap_futures(running, {}, None, None, [])   # 不抛就算过
