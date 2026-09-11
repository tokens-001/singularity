"""批准 GATE1 之后要把 `planning` 阶段踢起来 —— 但 GATE2 之后不能踢。

判据是"那个阶段归谁推"：

- **planning** 只有 `run_phase` 能推。调度循环只管 EXECUTING/INTEGRATING/DELIVERING，
  而前端**没有** run-phase 调用者（`api.runPhase` 定义了但没人用）。
  不踢的话项目批准完就永远停在 planning —— 2026-09-12 实测空等 14 分钟，
  界面只显示"架构设计中"，看不出是**没人点火**。
- **executing** 归调度循环，在这儿推 = 两套驱动抢着写同一个 phase。
"""
import pytest

from singularity.scheduler import project as proj_mod
from singularity.scheduler import _api_projects as ap


def _setup(tmp_path, monkeypatch, gate, next_phase):
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    p.phase = gate
    monkeypatch.setattr(proj_mod, "load", lambda _id: p)
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    monkeypatch.setattr(p, "confirm_gate", lambda g, d: next_phase)
    return p


class TestGateConfirmKick:
    def test_gate1_approval_kicks_planning(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch, proj_mod.Phase.GATE1, proj_mod.Phase.PLANNING)
        kicks = []
        monkeypatch.setattr(ap, "_start_background",
                            lambda pid, label, fn, *a: (kicks.append(label), True)[1])
        from singularity.scheduler import dispatcher
        monkeypatch.setattr(dispatcher, "load_agents", lambda: {})

        data, code = ap.project_gate_confirm("proj1", "gate1", "approved")

        assert code == 200
        assert data["next_phase"] == "planning"
        assert kicks == ["planning"], "GATE1 批准后必须把 planning 踢起来"

    def test_gate2_approval_does_not_kick(self, tmp_path, monkeypatch):
        """executing 归调度循环 —— 这儿推了就是两套驱动抢着写 phase。"""
        _setup(tmp_path, monkeypatch, proj_mod.Phase.GATE2, proj_mod.Phase.EXECUTING)
        kicks = []
        monkeypatch.setattr(ap, "_start_background",
                            lambda pid, label, fn, *a: (kicks.append(label), True)[1])

        data, code = ap.project_gate_confirm("proj1", "gate2", "approved")

        assert code == 200
        assert data["next_phase"] == "executing"
        assert kicks == [], "executing 不该在这儿启动"

    def test_gate3_approval_does_not_kick(self, tmp_path, monkeypatch):
        """delivering 同样归调度循环。"""
        _setup(tmp_path, monkeypatch, proj_mod.Phase.GATE3, proj_mod.Phase.DELIVERING)
        kicks = []
        monkeypatch.setattr(ap, "_start_background",
                            lambda pid, label, fn, *a: (kicks.append(label), True)[1])

        _, code = ap.project_gate_confirm("proj1", "gate3", "approved")

        assert code == 200
        assert kicks == []
