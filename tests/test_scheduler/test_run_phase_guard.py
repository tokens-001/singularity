"""`run-phase` 对 template / gate 档不许谎报"已启动"。

旧行为（2026-09-12 实测）：对一个刚建好的 `template` 项目 POST /run-phase，
接口回 `{"ok":true,"started":true}`，而 `workflow.run_phase` 对 TEMPLATE 只
`msgs.append("等待 Owner 填写需求并确认")` 就 `break` —— 后台线程跑了等于没跑，
连那句话都被 `_worker` 丢了。外面看着像启动了，实际项目一动不动（空等 14 分钟）。

判据：**返回 200 不等于动了手**（防御模式 §28）。
"""
import pytest

from singularity.scheduler import project as proj_mod
from singularity.scheduler import _api_projects as ap


def _mk(monkeypatch, tmp_path, phase):
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    p.phase = phase
    monkeypatch.setattr(proj_mod, "load", lambda _id: p)
    return p


@pytest.mark.parametrize("phase,hint", [
    (proj_mod.Phase.TEMPLATE, "/start"),
    (proj_mod.Phase.GATE1, "gate-confirm"),
    (proj_mod.Phase.GATE2, "gate-confirm"),
    (proj_mod.Phase.GATE3, "gate-confirm"),
])
def test_waiting_phases_report_honestly(tmp_path, monkeypatch, phase, hint):
    _mk(monkeypatch, tmp_path, phase)
    started = []
    monkeypatch.setattr(ap, "_start_background",
                        lambda *a, **k: (started.append(a), True)[1])

    data, code = ap.project_run_phase("proj1")

    assert started == [], "这几个阶段不该真的起后台线程（起了也是空转）"
    assert data["started"] is False, "没动手就不能说 started"
    assert data["ok"] is False
    assert hint in data["error"], f"错误信息要告诉人该用哪个接口: {data['error']}"
    assert code == 409


def test_runnable_phase_still_starts(tmp_path, monkeypatch):
    """对照组：正常阶段照旧启动，别把能跑的也拦了。"""
    _mk(monkeypatch, tmp_path, proj_mod.Phase.RESEARCHING)
    monkeypatch.setattr(ap, "_start_background", lambda *a, **k: True)
    from singularity.scheduler import dispatcher
    monkeypatch.setattr(dispatcher, "load_agents", lambda: {})

    data, code = ap.project_run_phase("proj1")

    assert data["started"] is True
    assert code == 200
