"""周期对账：把「状态说的」和「磁盘上真有的」比一遍，漂移就报出来。

分析里 P4 的"检出时延收尾" —— **状态漂移要能早发现**，别等项目卡死了才回头查。

⚠️ **只报不改**：自动"纠正"状态会把真问题抹平成假的一致，那正是本项目反复踩的
"静默兜底 = 编造"。
"""
import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod


def _proj(**kw):
    p = proj_mod.ProjectState(
        id="p1", name="t", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    for k, v in kw.items():
        setattr(p, k, v)
    return p


@pytest.fixture
def env(monkeypatch):
    """默认：一个干净项目、任务都在磁盘上。"""
    import singularity.scheduler.tracker as tracker
    state = {"projects": [_proj(phase=proj_mod.Phase.EXECUTING)],
             "tasks": {}}

    class _T:
        def __init__(self, tid):
            self.id = tid
            self.description = "任务"
            self.depends_on = []
            self.created_at = 1.0

    monkeypatch.setattr(proj_mod, "recover_all", lambda: state["projects"])
    monkeypatch.setattr(tracker, "read_task",
                        lambda tid: _T(tid) if tid in state["tasks"] else None)
    return state


def _kinds(drifts):
    return sorted(d["kind"] for d in drifts)


class TestReconcile:
    def test_clean_project_reports_nothing(self, env):
        assert orch.reconcile_projects() == []

    def test_missing_task_files_reported(self, env):
        env["projects"][0].task_ids = ["t1", "t2"]
        env["tasks"] = {"t1": 1}                      # t2 磁盘上没了
        drifts = orch.reconcile_projects()
        assert "task_ids_missing" in _kinds(drifts)
        d = [x for x in drifts if x["kind"] == "task_ids_missing"][0]
        assert "1/2" in d["detail"]

    def test_phase_drift_reported(self, env):
        """phase 和 lineage 最后一条对不上 = 有地方绕过 set_phase 改了状态。"""
        env["projects"][0].phase = proj_mod.Phase.PLANNING
        env["projects"][0].lineage = [
            {"action": "phase", "from": "gate1", "to": "planning"},
            {"action": "phase", "from": "planning", "to": "executing"},   # ← 末条说 executing
        ]
        drifts = orch.reconcile_projects()
        assert "phase_drift" in _kinds(drifts)

    def test_phase_matching_lineage_is_clean(self, env):
        env["projects"][0].phase = proj_mod.Phase.EXECUTING
        env["projects"][0].lineage = [{"action": "phase", "from": "planning", "to": "executing"}]
        assert orch.reconcile_projects() == []

    def test_budget_stop_reported(self, env, monkeypatch):
        from singularity.scheduler import _token_budget as tb
        monkeypatch.setattr(tb, "project_budget_state",
                            lambda pid, budget: ("stop", 9.9, "项目预算已用满 198%"))
        env["projects"][0].token_budget_total = 5.0
        assert "budget" in _kinds(orch.reconcile_projects())

    def test_recover_all_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(proj_mod, "recover_all",
                            lambda: (_ for _ in ()).throw(RuntimeError("磁盘挂了")))
        drifts = orch.reconcile_projects()          # 不抛
        assert _kinds(drifts) == ["reconcile_error"]

    def test_only_reports_never_mutates(self, env):
        """反证：对账**只读**，不许顺手改状态。"""
        p = env["projects"][0]
        p.task_ids = ["t1", "t2"]
        p.phase = proj_mod.Phase.PLANNING
        p.lineage = [{"action": "phase", "to": "executing"}]
        orch.reconcile_projects()
        assert p.task_ids == ["t1", "t2"], "不许动 task_ids"
        assert p.phase == proj_mod.Phase.PLANNING, "不许动 phase"
