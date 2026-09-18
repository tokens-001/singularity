"""钉住 `_start_background` 的**四个调用点**传的是什么（2026-09-18）。

为什么单独一个文件：`test_background_loads_fresh.py` 测的是 `_start_background`
**自己**的行为，**覆盖不到调用点**。签名从 `(proj, agents)` 变成 `(agents)` 之后，
任何一个调用点忘了改，真机上就是 `TypeError` —— 而那种错**只在点到那个按钮时才炸**。

⚠️ 为什么冒烟测试抓不到：`tests/smoke_test.py` 那条 `Gate确认API` 传的是
`{"decision": "skip"}`，而 `confirm_gate` 只认 `"approved"` / `"rejected"` ——
**`skip` 两个分支都不进**，恰好绕开了所有会真的干活的分支，
而四个调用点**全在 `approved` 那一支里**。

⚠️ 判据钉在「**最后一个参数是 agents(dict)，不是 ProjectState**」上 ——
钉"函数被调用了"没用：传对象也照样被调用，只是又回到整份覆盖写那条老路。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import _api_projects as api_p
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow as wf_mod


@pytest.fixture
def spy(monkeypatch, tmp_path):
    """把 `_start_background` 换成记录参数的探针，并备好一个干净项目。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(disp_mod, "load_agents", lambda: {})

    saved = {}
    monkeypatch.setattr(proj_mod, "save", lambda p: saved.update(p=p))

    calls = []

    def _fake_start_background(project_id, label, fn, agents):
        calls.append({"project_id": project_id, "label": label,
                      "fn": fn, "agents": agents})
        return True

    monkeypatch.setattr(api_p, "_start_background", _fake_start_background)
    return calls


def _seed(monkeypatch, project_id="proj1"):
    p = proj_mod.ProjectState(
        id=project_id, name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[],
        supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
        description="测试",
    )
    monkeypatch.setattr(proj_mod, "load", lambda pid: p if pid == project_id else None)
    return p


def _assert_last_arg_is_agents(calls):
    assert calls, "调用点没走到 —— 这个测试就白测了"
    for c in calls:
        assert isinstance(c["agents"], dict), (
            f"{c['label']}: 最后一个参数不是 agents，是 "
            f"{type(c['agents']).__name__} —— 又回到"
            f"「把内存对象传进分钟级后台线程」那条整份覆盖写的老路了")
        assert not isinstance(c["agents"], proj_mod.ProjectState)


def test_gate批准那条路的调用点(spy, monkeypatch):
    p = _seed(monkeypatch)
    p.phase = proj_mod.Phase.GATE1
    api_p.project_gate_confirm("proj1", gate="gate1", decision="approved")
    _assert_last_arg_is_agents(spy)
    assert any(c["label"] == "planning" for c in spy), f"没走 planning 那条: {spy}"


def test_run_phase那条路的调用点(spy, monkeypatch):
    p = _seed(monkeypatch)
    api_p.project_run_phase("proj1", phase_name="planning")
    _assert_last_arg_is_agents(spy)


def test_start那条路的调用点(spy, monkeypatch):
    p = _seed(monkeypatch)
    p.phase = proj_mod.Phase.DELIVERING  # 让 start 走"已有流程在跑"之外的分支
    api_p.project_start("proj1")
    _assert_last_arg_is_agents(spy)
