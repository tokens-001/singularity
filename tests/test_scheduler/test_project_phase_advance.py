"""项目阶段推进：`executing` 下的任务全到终态之后，往哪走。

`FAILED` 和 `DONE` 在推进判据里**同属"终态"** —— 所以必须先分一次"有没有成功的"。
不分的话，7 个任务全失败的项目会一路推到 DONE 并播报"交付完成!"，
用户看到的和事实完全相反（2026-09-11 流水线探针实测）。
"""
import json

import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker


def _mk_task(tmp_path, tid: str, status: tracker.TaskStatus) -> None:
    t = tracker.Task(id=tid, description=f"任务 {tid}")
    t.status = status
    (tmp_path / "tasks").mkdir(exist_ok=True)
    (tmp_path / "tasks" / f"{tid}.json").write_text(
        json.dumps(t.to_dict() if hasattr(t, "to_dict") else t.__dict__, default=str),
        encoding="utf-8")


def _setup(tmp_path, monkeypatch, statuses: list[tracker.TaskStatus]):
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.EXECUTING
    for i, st in enumerate(statuses):
        tid = f"t{i}"
        _mk_task(tmp_path, tid, st)
        p.task_ids.append(tid)
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    return p


def test_all_tasks_failed_does_not_advance(tmp_path, monkeypatch):
    """一个都没成功 → 不许推进到交付，并记一条 issue。

    旧代码在这里直接推进（FAILED 也算终态），项目最后停在 done —— 就是探针看到的
    "终态 done、7 个任务全 failed"。
    """
    p = _setup(tmp_path, monkeypatch,
               [tracker.TaskStatus.FAILED] * 3)
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.EXECUTING, "全失败还推进 = 会谎报交付完成"
    assert any(i.get("kind") == "all_tasks_failed" for i in p.issues)


def test_issue_recorded_only_once(tmp_path, monkeypatch):
    """调度循环每 tick 都会走到这里 —— issue 只能记一条，否则刷屏。"""
    p = _setup(tmp_path, monkeypatch, [tracker.TaskStatus.FAILED] * 2)
    for _ in range(5):
        orch._auto_trigger_test_fix({}, [])
    assert len([i for i in p.issues if i.get("kind") == "all_tasks_failed"]) == 1


def test_partial_success_still_advances(tmp_path, monkeypatch):
    """有任务成功就该交付 —— 失败的不能连累整个项目卡死。"""
    p = _setup(tmp_path, monkeypatch,
               [tracker.TaskStatus.DONE, tracker.TaskStatus.FAILED])
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.INTEGRATING


def test_all_done_advances(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch, [tracker.TaskStatus.DONE] * 2)
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.INTEGRATING


def test_decompose_fallback_reads_project_architecture(tmp_path, monkeypatch):
    """兜底拆解必须读 `proj.architecture`。

    它以前读 `<项目目录>/architecture.json` —— **全仓没有任何代码写这个文件**，
    所以永远卡在第一步 `if not arch_path.exists(): return`，一次都没救成功过。
    真进入"executing 且没任务"的项目只能一直卡着（2026-09-11 真流水线实测：
    卡 13 分钟、零日志）。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: tmp_path)

    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.architecture = {"tasks": [{"id": "T1", "title": "实现 X",
                                 "description": "创建 x.py", "layer": "impl"}]}
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)

    orch._decompose_and_create_tasks(p, {})
    assert len(p.task_ids) == 1, "架构里有任务却没建出来 = 兜底还在读那个没人写的文件"


def test_no_decomposable_tasks_is_surfaced_not_silently_stuck(tmp_path, monkeypatch):
    """架构拆不出任务 → 必须留痕，不能无声卡死。

    实测（2026-09-11 真流水线）：融合失败（模型欠费）→ 通用合成 → 产物不是合法
    架构 JSON（`{"parse_error": true}`）→ decompose 得 0 个任务 → 项目停在
    executing：没任务可跑、推进判据要求 task_ids 非空所以两条分支都不进
    —— **卡了 13 分钟，日志一行都没有**。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.EXECUTING           # 一个任务都没有
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    monkeypatch.setattr(orch, "_decompose_and_create_tasks", lambda _p, _a: None)

    for _ in range(3):
        orch._auto_trigger_test_fix({}, [])

    assert any(i.get("kind") == "no_decomposable_tasks" for i in p.issues)
    assert len([i for i in p.issues if i.get("kind") == "no_decomposable_tasks"]) == 1


# ── GATE3 入门票（2026-09-11 外派评审）────────────────────

def _mk_proj():
    return proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )


class TestGate3Admission:
    """进 GATE3 必须带验收结论，否则补一条记录 + 告警。

    REVIEWING 被两套驱动同时认识但行为不同：orchestrator 跑
    `run_test_fix_loop`（真跑 QA+安全审计），`run_phase` 的 REVIEWING 分支
    直接跳。异步验收线程炸了 / 用户手快先点了 / auto_mode 且循环没开 ——
    三条路的结局都是"验收整段没跑、零记录"，人审时看不出来。
    """

    def test_no_evidence_adds_ticket(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        warns = []
        import singularity.scheduler.witness as w
        monkeypatch.setattr(w, "warn", lambda *a, **k: warns.append(a))

        p = _mk_proj()
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "手点下一步")

        assert any(i.get("type") == "gate3_no_evidence" for i in p.issues), p.issues
        assert any("gate3_no_evidence" in str(x) for x in warns)
        assert p.phase == proj_mod.Phase.GATE3, "只补票，不阻断"

    def test_ran_marker_clears_the_ticket(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.issues = [{"type": "verification_ran", "detail": "QA + 安全审计已执行"}]
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "验收完成")
        assert not any(i.get("type") == "gate3_no_evidence" for i in p.issues)

    def test_skipped_marker_also_counts_as_evidence(self, tmp_path, monkeypatch):
        """`verification_skipped` 是"有结论"——结论就是没跑。不该再补一张票。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.issues = [{"type": "verification_skipped", "detail": "架构没产出约束清单"}]
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "跳过")
        assert not any(i.get("type") == "gate3_no_evidence" for i in p.issues)

    def test_other_phases_unaffected(self, tmp_path, monkeypatch):
        """门票只管 GATE3 —— 别的流转不该被它碰。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.phase = proj_mod.Phase.GATE2
        p.set_phase(proj_mod.Phase.EXECUTING, "架构通过")
        assert p.issues == []
