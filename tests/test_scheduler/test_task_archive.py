"""任务收尾的三件归档，**两条收尾路径都必须做**。

  · `TaskRunner.finalize`            —— 单任务直接合并
  · `orchestrator._drain_pending`    —— v3 并行，任务走合并队列，合并完才收尾

实测（2026-09-11 真机验证）：跑完一个任务，`experiences.json` / `token_usage.json`
**根本没被创建**，`route_learner.json` 一动不动。而 `events.json` 正常长大 ——
因为 `_save_trace` 两条路径都有，从外面看像是"归档跑了"，其实只跑了一半。
"""
from types import SimpleNamespace

import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker


class _MR:
    """假 MergeResult。"""
    def __init__(self, task_id, status="merged"):
        self.task_id = task_id
        self.status = status
        self.new_head = "abcdef123456"
        self.conflict_files = []
        self.reason = ""


class _MQ:
    def __init__(self, results):
        self._results = results

    def drain(self):
        return self._results


def _setup(monkeypatch, tmp_path):
    from singularity.scheduler import config
    import singularity.scheduler._task_runner as tr
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(orch, "tracker", tracker)
    monkeypatch.setattr(orch, "_maybe_complete_parents", lambda *a: None)
    monkeypatch.setattr(orch, "_release_ref", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)

    called = []
    monkeypatch.setattr(tr.mem_mod, "archive_experience",
                        lambda **k: called.append("experience"))
    monkeypatch.setattr(tr, "record_tokens", lambda **k: called.append("tokens"))
    monkeypatch.setattr(tr.rl_mod, "save_learner", lambda *a: called.append("learner"))
    return called


def _batch():
    return SimpleNamespace(dispatch_result=None, validation=None,
                           pre_search_skipped=False, pre_search_reason="",
                           pre_search_top_decisions=[], pre_search_memory={})


def test_drain_pending_archives_all_three(monkeypatch, tmp_path):
    """合并成功后，三件归档一件都不能少。

    旧代码在 _drain_pending 里自己重写了收尾（transition + _save_trace），
    完全没做这三件 —— 所以这条测试在旧代码上会因为 called 是空的而红。
    """
    called = _setup(monkeypatch, tmp_path)
    t = tracker.create("测试任务：合并路径收尾")
    tracker.transition(t.id, tracker.TaskStatus.DONE)

    pending = {t.id: (tracker.read_task(t.id), None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id)]), [])

    assert sorted(called) == ["experience", "learner", "tokens"], called


def test_drain_pending_conflict_also_archives(monkeypatch, tmp_path):
    """冲突也是任务的终态，同样要归档（failure_mode 记冲突原因）。"""
    called = _setup(monkeypatch, tmp_path)
    t = tracker.create("测试任务：冲突收尾")

    pending = {t.id: (tracker.read_task(t.id), None, None, _batch())}
    orch._drain_pending(pending, _MQ([_MR(t.id, status="conflict")]), [])

    assert sorted(called) == ["experience", "learner", "tokens"], called
