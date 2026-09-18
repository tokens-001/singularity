"""`_start_background` 必须在**后台线程里**重新 load（2026-09-18 外派评审第二轮坐实）。

原来四个调用点都把**请求线程 `load()` 出来的那个对象**直接交进后台线程 ——
而那线程是**分钟级**的（`run_phase` 里会跑多模型委员会，实测 621 秒）。
而 `project.save()` 是 `to_dict()` **整份覆盖写**：后台线程一存盘，就把这几分钟里
别人写的一切（`owner_confirm` 里人的批准、`review_failures` 棘轮复位、`task_ids`、
`issues`、`phase`）**整份退回**，还会给这次倒退记一条 lineage —— 轨迹上像有人推了它。

⚠️ 判据钉在「**load 发生在别的线程里**」上，**不能钉"函数跑通了"**：
把"传对象"那条路还原，函数照样跑通、照样返回 True，只是又抹了别人的写入 ——
那是假绿（本仓 2026-09-17 栽过一次同形状的）。
"""
import threading
import time

from singularity.scheduler import _api_projects as api_p
from singularity.scheduler import project as proj_mod
from singularity.scheduler import witness


def _mk():
    return proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[],
        supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
    )


def _wait_until(pred, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_项目在后台线程里重新load_而不是拿请求线程那份(monkeypatch):
    main_tid = threading.get_ident()
    load_calls = []

    def _load(pid):
        load_calls.append((pid, threading.get_ident()))
        return _mk()

    monkeypatch.setattr(proj_mod, "load", _load)
    monkeypatch.setattr(api_p, "_RUNNING_PHASES", set())

    seen = []
    done = threading.Event()

    def _fn(proj, agents):
        seen.append(proj)
        done.set()

    assert api_p._start_background("proj1", "t", _fn, {}) is True
    assert done.wait(5), "后台线程没跑起来"

    assert load_calls, (
        "后台线程没有 load ⇒ 手里攥着请求线程那份旧快照，一存盘就把这几分钟里"
        "别人的写入（人的批准 / 棘轮复位 / task_ids）整份抹掉")
    assert load_calls[0][1] != main_tid, (
        f"load 发生在请求线程（tid={main_tid}）里 ⇒ 拿的还是那份旧对象")
    assert seen and seen[0].id == "proj1", "fn 拿到的不是项目对象"


def test_load不到项目时出声_且不把None喂给fn(monkeypatch):
    """排队期间项目被删了 —— 必须出声。

    静默的后果：后台线程悄悄什么都没干，而调用方已经回了 `ok: true, started: true`，
    界面上"启动了"和"没启动"长得一模一样。
    """
    monkeypatch.setattr(proj_mod, "load", lambda pid: None)
    phases = set()
    monkeypatch.setattr(api_p, "_RUNNING_PHASES", phases)
    warns = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **k: warns.append(msg))

    called = []
    assert api_p._start_background("gone", "t", lambda p, a: called.append(p), {}) is True
    assert _wait_until(lambda: "gone" not in phases), "worker 没跑完"

    assert called == [], "项目没了还把 None 喂给 fn（那是个只在后台线程里炸的失败）"
    assert any("project_gone" in m for m in warns), f"项目没了却一声不吭: {warns}"
