"""流程复利账本（分析里的 P6）：记下每轮**实际发生了什么**。

三个乘数里"复利"是唯一属于自己那个（模型是租的、流程是行业常识），而它今天 ≈ 0。
这是从 0 到 1 的那一步。

两条规矩钉在这里：
· **独立文件、只追加** —— §39：别塞进会被"整行重建"的地方
· **它记事实，不下结论** —— 系统替人总结"该怎么做"会变成自己教自己的回音壁
"""
import json

import pytest

from singularity.scheduler import _process_ledger as pl
from singularity.scheduler import project as pm
from singularity.scheduler import tracker


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """conftest 已经把 QIDIAN_DIR 隔离到 tmp_path —— 账本自动落在那里。"""
    return tmp_path


def _proj(**kw):
    p = pm.ProjectState(
        id="p1", name="演示项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _fake_tasks(monkeypatch, statuses: dict):
    class _T:
        def __init__(self, st):
            self.status = st
    monkeypatch.setattr(tracker, "read_task",
                        lambda tid: _T(statuses[tid]) if tid in statuses else None)


class TestRecord:
    def test_tasks_done_counted_by_enum_value(self, ledger, monkeypatch):
        """⚠️ 回归：`str(TaskStatus.DONE)` 是 `'TaskStatus.DONE'` 不是 `'done'` ——
        第一版拿它去 `endswith("done")`，大小写不匹配，**tasks_done 恒为 0**。"""
        _fake_tasks(monkeypatch, {"a": tracker.TaskStatus.DONE,
                                  "b": tracker.TaskStatus.DONE,
                                  "c": tracker.TaskStatus.FAILED})
        row = pl.record(_proj(task_ids=["a", "b", "c"]))
        assert row["tasks_done"] == 2 and row["tasks_total"] == 3

    def test_missing_task_not_counted_as_done(self, ledger, monkeypatch):
        _fake_tasks(monkeypatch, {"a": tracker.TaskStatus.DONE})
        row = pl.record(_proj(task_ids=["a", "b"]))     # b 磁盘上没了
        assert row["tasks_done"] == 1

    def test_issue_kinds_tallied(self, ledger, monkeypatch):
        _fake_tasks(monkeypatch, {})
        row = pl.record(_proj(issues=[{"type": "x"}, {"type": "x"}, {"type": "y"}]))
        assert row["issues"] == {"x": 2, "y": 1}

    def test_appends_does_not_rewrite_history(self, ledger, monkeypatch):
        _fake_tasks(monkeypatch, {})
        pl.record(_proj(id="a", name="第一轮"))
        pl.record(_proj(id="b", name="第二轮"))
        rows = pl.load()
        assert [r["name"] for r in rows] == ["第一轮", "第二轮"], "只追加，不改老的"

    def test_writes_its_own_file(self, ledger, monkeypatch):
        """§39：独立文件。不许塞进项目文件 / token_usage 那种会被整行重建的地方。"""
        _fake_tasks(monkeypatch, {})
        pl.record(_proj())
        assert (ledger / "process_ledger.json").exists()
        # 也不该跟别的记录混在一个文件里
        raw = (ledger / "process_ledger.json").read_text(encoding="utf-8")
        assert json.loads(raw)[0]["project_id"] == "p1"

    def test_failure_outcome_is_recorded_too(self, ledger, monkeypatch):
        """**失败也要记** —— 只记成功的话 digest 永远一片大好，下一轮照样撞墙。"""
        _fake_tasks(monkeypatch, {})
        row = pl.record(_proj(), {"delivery": "failed", "detail": "拆不出任务"})
        assert row["delivery"] == "failed"
        assert "拆不出任务" in row["detail"]

    def test_never_raises_on_bad_input(self, ledger, monkeypatch):
        monkeypatch.setattr(tracker, "read_task",
                            lambda tid: (_ for _ in ()).throw(RuntimeError("磁盘挂了")))
        row = pl.record(_proj(task_ids=["a"]))          # 不抛
        assert row["tasks_done"] == 0


class TestDigest:
    def test_empty_ledger_gives_empty(self, ledger):
        assert pl.digest() == ""

    def test_states_facts_not_lessons(self, ledger, monkeypatch):
        """"上一轮发生了什么"，**不是"你该怎么做"** —— 系统没资格下那个结论。"""
        _fake_tasks(monkeypatch, {"a": tracker.TaskStatus.DONE})
        p = _proj(task_ids=["a"], fix_round=2)
        p.phase = pm.Phase.DONE
        pl.record(p)
        d = pl.digest()
        assert "任务 1/1 成功" in d
        for preachy in ("应该", "建议", "务必", "教训是"):
            assert preachy not in d, f"digest 不许说教（出现『{preachy}』）"

    def test_mentions_failures_and_issues(self, ledger, monkeypatch):
        _fake_tasks(monkeypatch, {"a": tracker.TaskStatus.FAILED})
        p = _proj(task_ids=["a"], issues=[{"type": "requirement_uncovered"}])
        p.phase = pm.Phase.GATE3
        pl.record(p)
        d = pl.digest()
        assert "失败 1" in d and "requirement_uncovered" in d

    def test_limits_to_recent_n(self, ledger, monkeypatch):
        _fake_tasks(monkeypatch, {})
        for i in range(7):
            pl.record(_proj(id=f"p{i}", name=f"第{i}轮"))
        d = pl.digest(3)     # 最近 3 条 = 第4/第5/第6
        assert "第6轮" in d and "第5轮" in d and "第4轮" in d
        assert "第3轮" not in d, "更早的不该进来"


class TestPathNotFrozen:
    def test_path_is_computed_at_call_time(self, tmp_path, monkeypatch):
        """§34：#34 —— 冻在模块级的路径会写进生产。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "one")
        assert pl._path() == tmp_path / "one" / "process_ledger.json"
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "two")
        assert pl._path() == tmp_path / "two" / "process_ledger.json"
