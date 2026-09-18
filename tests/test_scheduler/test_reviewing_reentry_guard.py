"""REVIEWING 分支必须防重入（2026-09-18 外派评审抓出，逐行核过）。

`_run_integration_merge_async` **自己就调** `run_test_fix_loop` —— 它先
`set_phase(REVIEWING)` + `save`，**再**调（里面是两次 LLM 调用加最多 10 条
子进程检查，窗口分钟级）。而 `_auto_trigger_test_fix` 是**调度循环每一 tick**
扫一遍，扫到 `reviewing` 就再调一次 ⇒ **同一个项目的验收同时跑两遍**：
两份钱，各自 `issues = []` 再填，最后 `save()` 整对象覆盖。

⚠️ 判据钉在「**有没有被调用**」上，不能只看 `phase` —— 只看 phase 的话，
把守卫整行删掉照样绿（假接线，本仓 2026-09-17 刚栽过一次）。
⚠️ 第二个用例是守卫的**反面**：不在 `_merge_inflight` 里时**必须照常跑**，
否则就成了"为了防重入把路堵死"，项目卡在 REVIEWING 没人推。
"""
from singularity.scheduler import config
from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[],
        supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.REVIEWING
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _proj: None)
    monkeypatch.setattr(orch, "_pending_sse_events", [])
    return p


def _spy(monkeypatch):
    from singularity.scheduler import workflow as wf
    calls = []
    monkeypatch.setattr(wf, "run_test_fix_loop",
                        lambda proj, agents: calls.append(proj.id) or "ok")
    return calls


def test_合并线程正在跑验收时_调度循环不许再进(tmp_path, monkeypatch):
    p = _mk(tmp_path, monkeypatch)
    calls = _spy(monkeypatch)
    monkeypatch.setattr(orch, "_merge_inflight", {p.id})

    orch._auto_trigger_test_fix({}, [])

    assert calls == [], (
        "验收正在跑，调度循环又进去调了一遍 ⇒ 同一个项目两份 LLM 账、"
        "两次 issues 重填、最后一次整对象 save 覆盖前一份")


def test_不在_merge_inflight_里时_照常跑(tmp_path, monkeypatch):
    """守卫只挡「合并线程自己已经在跑」那一种，不能把正常路径一起堵死。"""
    p = _mk(tmp_path, monkeypatch)
    calls = _spy(monkeypatch)
    monkeypatch.setattr(orch, "_merge_inflight", set())

    orch._auto_trigger_test_fix({}, [])

    assert calls == [p.id], "不在飞的就该照常验收，否则项目卡在 REVIEWING 没人推"
