"""「停」的**接线** —— 入口、路由、以及 §77.1 那个"会不会变成空转"的钉子。

`test_project_halt.py` 钉的是判据本身；这里钉的是**判据有没有接到路上**。
（本仓规矩：函数对 ≠ 接线通。删掉调用点、判据照样"对"，而线上照旧往前走。）

⚠️ 每一条都写了"变异咬法" —— 判据是「**删掉哪一行它会红**」。
"""
import json
import time

import pytest

from singularity.scheduler import project as P
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


@pytest.fixture(autouse=True)
def _clean_halt_cache():
    """`_halted_project_ids` 是 2 秒 TTL 的**模块级**缓存 —— 用例之间会互相串。

    （同族：`防御模式.md`「借别人的桩，还原名单漏一个模块」—— 全量跑挂、单独跑绿。）
    """
    tracker.invalidate_halt_cache()
    yield
    tracker.invalidate_halt_cache()


@pytest.fixture
def proj():
    p = P.create(name="_t_halt_wire", template="feature", description="x")
    p.phase = P.Phase.EXECUTING
    P.save(p)
    return p


def _mk_task(proj, status=TaskStatus.PENDING, desc="[T1] 实现某模块: 创建 x.py"):
    t = tracker.create(desc, depth=0, project_id=proj.id)
    proj.task_ids = list(proj.task_ids or []) + [t.id]
    P.save(proj)
    if status != TaskStatus.PENDING:
        tracker.transition(t.id, status)
    return t.id


# ── ① `project_stop` 的后半段：冻住 phase ──────────────────────

def test_stop_落标记且phase不动(proj):
    """**"停"必须落在项目上**，不能只停在任务上 —— 那是这次要堵的洞。

    变异：删掉 `proj.mark_user_stop(...)` 那句 ⇒ 本条红。
    """
    from singularity.scheduler._api_projects import project_stop
    _mk_task(proj, TaskStatus.RUNNING)
    before = P.load(proj.id).phase
    res, code = project_stop(proj.id)
    assert code == 200, res
    assert res["halted"] is True
    after = P.load(proj.id)
    assert after.halted_reason == P.HALT_USER_STOP, "项目级标记没落 —— 那 phase 拦不住"
    assert after.phase == before, "停不该改 phase（停在哪一档是恢复时要用的信息）"


def test_stop_先落标记再停任务(proj, monkeypatch):
    """🔴 **顺序的钉子**：先落标记 ⇒ 就算某个 `task_cancel` 抛了，项目也已经是"停"的。

    反过来会留下一个缝隙：任务都停了、标记没落，而 phase 照旧往前推 —— 正是这次要堵的洞。

    变异：把两句调换（先循环 task_cancel 再 mark_user_stop）⇒ 本条红。
    """
    from singularity.scheduler import _api_tasks
    from singularity.scheduler._api_projects import project_stop
    _mk_task(proj, TaskStatus.RUNNING)

    monkeypatch.setattr(_api_tasks, "task_cancel",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟取消炸了")))
    with pytest.raises(RuntimeError):
        project_stop(proj.id)
    assert P.load(proj.id).halted_reason == P.HALT_USER_STOP, \
        "取消任务那步炸了，而项目还没落'停' —— 缝隙就在这儿"


def test_resume_只解禁令不重派任务(proj):
    """**停不是暂停**：恢复只解禁令；任务要回来得逐个 retry。

    变异：把 `resume` 实现成调 `project_start` ⇒ 本条红（`task_ids` 会被整批重建）。
    """
    from singularity.scheduler._api_projects import project_resume, project_stop
    tid = _mk_task(proj, TaskStatus.FAILED)
    project_stop(proj.id)
    ids_before = list(P.load(proj.id).task_ids)
    status_before = tracker.read_task(tid).status

    res, code = project_resume(proj.id)
    assert code == 200 and res["resumed"] is True, res
    after = P.load(proj.id)
    assert after.halted_reason == ""
    assert list(after.task_ids) == ids_before, "恢复把任务表重写了 —— 那不是恢复，是重开"
    assert tracker.read_task(tid).status is status_before, \
        "恢复把任务状态动了 —— 那成了「暂停续跑」，而 09-21 拍板的语义是「停」"


def test_resume_派生那条还成立就不撤issue(proj):
    """🔴 **resume 只解人工那一半**：还有失败任务 ⇒ 项目仍停滞 ⇒ 界面上那条**必须留着**。

    09-27 真机撞出来的现场：`resume` 之后 `halt_state` 仍报 `halted=True / task_failed(13)`，
    而 `issues` 被清空了 —— 而**界面读的就是 `issues`**（全仓前端源码里 `halt` 出现 0 次）
    ⇒ 界面看着项目没事、其实它还停着。

    变异：把撤票改回**无条件**（去掉 `if halted_now["halted"]` 那层）⇒ 本条红。
    """
    from singularity.scheduler._api_projects import project_resume, project_stop
    _mk_task(proj, TaskStatus.FAILED)
    project_stop(proj.id)
    assert project_resume(proj.id)[1] == 200

    after = P.load(proj.id)
    kept = [i for i in after.issues if i.get("kind") == "project_stalled"]
    assert kept, "停还没解除就把 issue 撤了 —— 界面上唯一的证据没了"
    assert kept[0]["reason"] == "task_failed", \
        f"理由还停在上一条（user_stop）上，而它已经不成立了：{kept[0]}"
    assert P.halt_state(after)["halted"] is True


def test_resume_停真解除了才撤issue(proj):
    """**反方向对照**：撤票别写成恒不撤（那样恢复之后界面永远在说"已停"）。

    ⚠️ **本条的现场刻意选"还没任务"**：`project_stop` 会把 PENDING/BLOCKED 直接转
    **FAILED**（"停"的语义），所以**任何在 executing 期停过的项目，恢复后都必然
    还带着失败任务**、派生那条必然成立 ⇒ 拿 PENDING 任务造的这个对照是假的
    （第一版就是那么写的，当场红）。真实的"停真解除"就是文档里那句
    「人在更早的阶段（template/gate2…）恢复」—— 那时一条任务都还没有。

    变异：把 `if halted_now["halted"]` 那层去掉另一半（恒不撤）⇒ 本条红。
    """
    from singularity.scheduler._api_projects import project_resume, project_stop
    proj.phase = P.Phase.TEMPLATE
    P.save(proj)
    project_stop(proj.id)
    assert project_resume(proj.id)[1] == 200

    after = P.load(proj.id)
    assert [i for i in after.issues if i.get("kind") == "project_stalled"] == [], \
        "停已经解除了，issue 还赖着 —— 界面永远在说「已停」"
    assert P.halt_state(after)["halted"] is False


def test_resume_没停过就409(proj):
    """**不假装成功**：返回 200 会让人以为"我按了恢复，项目在动了"。"""
    from singularity.scheduler._api_projects import project_resume
    res, code = project_resume(proj.id)
    assert code == 409, res


# ── ② 四条入口的 409 ──────────────────────────────────────────

def test_gate_confirm_在停了的项目上被拒(proj):
    """⚠️ 这条路**同时是观察者聊天"通过"的入口**，拦在这儿两条一起堵住。

    变异：删掉 `project_gate_confirm` 里那句 halt 检查 ⇒ 本条红（相位会动）。
    """
    from singularity.scheduler._api_projects import project_gate_confirm
    proj.phase = P.Phase.GATE2
    proj.mark_user_stop("测试")
    P.save(proj)
    res, code = project_gate_confirm(proj.id, gate="gate2", decision="approved")
    assert code == 409, res
    assert P.load(proj.id).phase is P.Phase.GATE2, "停着还能批 GATE ⇒ 拦了个寂寞"


def test_start_在停了的项目上被拒(proj):
    """`/start` 在 EXECUTING 上会 `task_ids = []` 整批重建 —— 恢复必须走 `/resume`。

    变异：删掉 `project_start` 里那句 halt 检查 ⇒ 本条红。
    """
    from singularity.scheduler._api_projects import project_start
    proj.mark_user_stop("测试")
    P.save(proj)
    res, code = project_start(proj.id)
    assert code == 409, res


def test_run_phase_接口在停了的项目上被拒(proj):
    from singularity.scheduler._api_projects import project_run_phase
    proj.mark_user_stop("测试")
    P.save(proj)
    res, code = project_run_phase(proj.id)
    assert code == 409, res


def test_HTTP路由接通(tmp_path, monkeypatch):
    """函数对 ≠ 接线通：把路由删掉 ⇒ 本条红。"""
    from singularity.web import app as webapp
    rules = {str(r) for r in webapp.app.url_map.iter_rules()}
    assert "/api/projects/<project_id>/resume" in rules
    assert "/api/projects/<project_id>/stop" in rules


# ── ③ `task_retry`：两条相反的路（对照组）─────────────────────

def test_retry_人工叫停的拒_前置失败的放(proj):
    """**一条判据的两侧必须互咬** —— 只测一侧的话，"恒拒"和"恒放"都能骗过去。

    变异：删掉 `task_retry` 里那句判断 ⇒ 第一条红；把判断写成恒拒 ⇒ 第二条红。
    """
    from singularity.scheduler._api_tasks import task_retry

    # (a) 人工叫停 ⇒ 409（背着他动手 = 任务一回 PENDING 就被派下去烧钱）
    a = _mk_task(proj, TaskStatus.FAILED)
    proj.mark_user_stop("测试")
    P.save(proj)
    res, code = task_retry(a)
    assert code == 409, f"叫停着还能重试单任务：{res}"

    # (b) 前置失败停滞（**没有** user_stop）⇒ 放行，**这就是用户二选一里的①**
    _p = P.load(proj.id)
    _p.clear_user_stop("测试")
    P.save(_p)
    res2, code2 = task_retry(a)
    assert code2 == 200, f"两个选项里唯一能点的那个被拦了：{res2}"
    assert tracker.read_task(a).status is TaskStatus.PENDING


# ── ④ `workflow.run_phase` 的每轮重读 ─────────────────────────

def test_run_phase_停时不调模型(proj, monkeypatch):
    """变异：删掉 `run_phase` 的 `while True` 开头那段守卫 ⇒ 本条红（会抛）。"""
    from singularity.scheduler import workflow as wf
    proj.phase = P.Phase.RESEARCHING
    proj.mark_user_stop("测试")
    P.save(proj)
    monkeypatch.setattr(wf, "_run_research",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("不该跑到这儿")))
    out = wf.run_phase(P.load(proj.id), {})
    assert "已停" in out, out


def test_停是中途落的也拦得住(proj, monkeypatch):
    """🔴 **stale 对象的钉子** —— 本函数跑在后台线程里，手里那份是**进线程那一刻的快照**，
    而人是**中途**按的停。光看手里那份永远看不到。

    ⚠️ 而且必须把盘上那份的标记**搬回**手里那份：下面各阶段会 `save(project)`，
    而 `save` 是**整份覆盖写** ⇒ 不搬的话，这一存就把人刚落的停抹掉。

    变异：把"每轮重读"换成读手里那份 ⇒ 本条红。
    """
    from singularity.scheduler import workflow as wf
    stale = P.load(proj.id)              # 手里这份：**没有**停
    assert stale.halted_reason == ""
    disk = P.load(proj.id)               # 盘上那份：人按了停
    disk.phase = P.Phase.RESEARCHING
    disk.mark_user_stop("中途按的")
    P.save(disk)
    monkeypatch.setattr(wf, "_run_research",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("不该跑到这儿")))
    out = wf.run_phase(stale, {})
    assert "已停" in out, out
    assert stale.halted_reason == P.HALT_USER_STOP, "没把盘上的停搬回来 —— 下一存就抹掉了"


def test_没停的项目照旧推(proj, monkeypatch):
    """**反方向对照**：守卫别写成恒 break（那样所有项目都推不动了）。"""
    from singularity.scheduler import workflow as wf
    proj.phase = P.Phase.RESEARCHING
    P.save(proj)
    calls = []

    def _fake_research(project, agents):
        calls.append(1)
        project.phase = P.Phase.TEMPLATE      # 下一轮进 TEMPLATE 分支就 break
        return "调研完了"

    monkeypatch.setattr(wf, "_run_research", _fake_research)
    wf.run_phase(P.load(proj.id), {})
    assert calls == [1], "没停的项目被闸门拦住了"


# ── ⑤ §77.1 的钉子：停了的项目，任务不进 ready ────────────────

def test_停了的项目的任务不进ready(proj, monkeypatch):
    """变异：删掉 `ready_tasks` 里那句 `continue` ⇒ 本条红。"""
    from singularity.scheduler import project as _pm
    tid = _mk_task(proj, TaskStatus.PENDING)
    monkeypatch.setattr(_pm, "list_all", lambda: [P.load(proj.id)])
    assert [t.id for t in tracker.ready_tasks()] == [tid], "先证明没停的时候它是在的"

    p = P.load(proj.id)
    p.mark_user_stop("测试")
    P.save(p)
    tracker.invalidate_halt_cache()
    assert [t.id for t in tracker.ready_tasks()] == [], "停了的项目还在派任务"


def test_没停的项目照旧进ready(proj, monkeypatch):
    """**反方向对照**：把过滤条件写成恒真（什么都滤掉）⇒ 本条红。"""
    from singularity.scheduler import project as _pm
    tid = _mk_task(proj, TaskStatus.PENDING)
    monkeypatch.setattr(_pm, "list_all", lambda: [P.load(proj.id)])
    tracker.invalidate_halt_cache()
    assert [t.id for t in tracker.ready_tasks()] == [tid]


def test_停了之后循环会退出而不是空转(proj, monkeypatch):
    """🔴🔴 **这一条是 §77.1 的机器判据**。

    `orchestrator._run_queue_v3` 的**出口判据就是 `ready_tasks` 的返回值**：
    `remaining = tracker.ready_tasks(...)` ⇒ `if not remaining: break`。
    ⇒ 如果过滤放在**派发口**（`_dispatch_ready`）而这里照旧返回，
    就变成"表里有东西 ∧ 一圈没进展" ⇒ 出口恒不满足 ⇒ **全速空转**
    —— 那正是 2026-09-17 那次事故的形状（1731 条 `drain_dep_blocked` / 2 分钟、44 分钟 CPU）。

    判据不是"跑得快不快"（时延断言不可靠），而是：**它返回了没有**。
    把 `time.sleep` 换成"第 50 次就抛"，循环要是没退，这里必然红。

    变异：把过滤从 `ready_tasks` 挪到 `_dispatch_ready` ⇒ 本条红，且报的正是"空转"。
    """
    from singularity.scheduler import orchestrator as orch
    from singularity.scheduler import project as _pm
    _mk_task(proj, TaskStatus.PENDING)
    p = P.load(proj.id)
    p.mark_user_stop("测试")
    P.save(p)
    monkeypatch.setattr(_pm, "list_all", lambda: [p])
    monkeypatch.setattr(orch, "_dispatch_ready", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_auto_trigger_test_fix", lambda *a, **k: None)

    ticks = []

    def _fake_sleep(_s):
        ticks.append(1)
        if len(ticks) > 50:
            raise AssertionError("循环没退出 ⇒ §77.1 的空转形状又回来了")

    monkeypatch.setattr(time, "sleep", _fake_sleep)
    tracker.invalidate_halt_cache()
    orch._run_queue_v3({}, 1)            # 它必须**返回**
    assert ticks == [], f"睡了 {len(ticks)} 次 —— 空转了"
