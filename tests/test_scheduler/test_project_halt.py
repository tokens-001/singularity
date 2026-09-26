"""「停」—— 人工叫停 + 前置失败 ⇒ 停滞（2026-09-27 用户拍板）。

**为什么要它**：`round-20260926` 实测 —— 02:17 按了 `project_stop`（任务全取消），
**02:21:40 项目自己从 `executing` 走到 `integrating`**，lineage 写着「任务全部到终态(失败 13)→集成合并」。
根因：`project_stop` **只给任务发取消**，完全不碰 `proj.phase`、也不落任何"已停"标记；
而 `orchestrator._advance_project` 的推进判据**只看任务状态**。

另一半是同一天翻出来的老洞：`tracker._any_dead_dep` 把"上游失败"标成**降级运行**照常放行，
注释写着「返工循环会修复」—— **而项目级自动返工 09-21 起默认关**
⇒ 上游永远不会被修，降级运行 = 在没地基的情况下继续盖。

🔵 本文件钉两层：`project.py`（字段 + 判据）和 `orchestrator._advance_project`（拦截点）。
`ready_tasks` 的项目级冻结与 HTTP 路由各自另有文件钉。

⚠️ **两种停的来源不一样，这是本节最要紧的一条**：
  · `user_stop` **落盘**（人做的决定，重启后仍成立），只能由人显式恢复；
  · `task_failed` **派生**（判据 = 现在有没有失败的任务），人一重试**自己就没了**。
    下面对第二条专门钉一条"把 `mark_user_stop`/`clear_user_stop` 全删掉它照样绿" ——
    那个"绿"本身就是"派生"这个设计的钉子：**没有清标记的代码，就不可能写出清错的标记**。
"""
import json

import pytest

from singularity.scheduler import project as P
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


@pytest.fixture
def proj():
    return P.create(name="_t_halt", template="feature", description="x")


def _mk_task(proj, status=TaskStatus.PENDING, desc="[T1] 实现某模块: 创建 x.py"):
    """建任务并**挂到项目上** —— `tracker.create` 不会自己进 `proj.task_ids`。"""
    t = tracker.create(desc, depth=0, project_id=proj.id)
    proj.task_ids = list(proj.task_ids or []) + [t.id]
    P.save(proj)
    if status != TaskStatus.PENDING:
        tracker.transition(t.id, status)
    return t.id


# ── ① 字段与序列化 ─────────────────────────────────────────────

def test_存量文件没有新字段也能读_且不出声(monkeypatch):
    """老项目 JSON（没 `halted_reason`/`halted_at`）必须能读。

    ⚠️ 顺带钉住"**不告警**"：`unknown_fields_dropped` 那条告警是给"代码不认识这个键"用的，
    加字段的方向反了（代码认识、文件没有）时不该响 —— 响了就是每天几十条假警。

    🔴 **桩必须走 `monkeypatch`**：第一版直接 `witness.warn = lambda ...`，**没有还原**，
    于是后面所有用例的 `witness.warn` 都成了假货 —— 全量跑时 `test_witness_alerts.py`
    **30 条红**，而单独跑这个文件全绿（红在**别的文件里**）。同族：`防御模式.md`
    「借别人的桩，还原名单漏一个模块」。
    """
    from singularity.scheduler import witness
    warns = []
    monkeypatch.setattr(witness, "warn", lambda *a, **k: warns.append((a, k)))

    raw = P.create(name="_t_old", template="feature", description="x").to_dict()
    raw.pop("halted_reason", None)
    raw.pop("halted_at", None)
    p = P.ProjectState.from_dict(raw)
    assert p.halted_reason == "" and p.halted_at == 0.0
    assert not [w for w in warns if "unknown_fields_dropped" in str(w)], warns


def test_to_dict_带上了新字段():
    """`to_dict` 是 dataclass 派生的 —— 这条钉住"别有人改回手抄字段清单"。

    （手抄过一次：GATE3 的 qa_report 就是这么在界面上整份丢掉的。）
    """
    d = P.ProjectState(id="x", name="y").to_dict()
    assert "halted_reason" in d and "halted_at" in d


# ── ② halt_state：三分支 ───────────────────────────────────────

def test_没失败没叫停就是没停(proj):
    _mk_task(proj, TaskStatus.DONE)
    h = P.halt_state(proj)
    assert h["halted"] is False and h["reason"] == "" and h["failed_tasks"] == []


def test_有失败就是停滞_且列出是哪个(proj):
    good, bad = _mk_task(proj, TaskStatus.DONE), _mk_task(proj, TaskStatus.FAILED)
    h = P.halt_state(proj)
    assert h["halted"] is True
    assert h["reason"] == "task_failed"
    assert h["failed_tasks"] == [bad], "得说是哪个任务失败了，人才知道去重试谁"


def test_ROLLED_BACK也算失败(proj):
    """判据与 `tracker._DEAD_END` 同一份 —— 不在这里抄第二份。"""
    rb = _mk_task(proj, TaskStatus.ROLLED_BACK)
    assert P.halt_state(proj)["failed_tasks"] == [rb]


def test_人工叫停优先于任务层(proj):
    _mk_task(proj, TaskStatus.DONE)
    proj.mark_user_stop("测试")
    assert P.halt_state(proj)["reason"] == P.HALT_USER_STOP


# ── ③ mark / clear ────────────────────────────────────────────

def test_人工叫停记lineage且不动phase(proj):
    """**`phase` 不动是有意的** —— "停在哪个阶段"正是恢复时要用的信息。"""
    before = proj.phase
    proj.mark_user_stop("POST /api/projects/x/stop")
    assert proj.phase == before, "叫停不该改 phase"
    assert proj.halted_reason == P.HALT_USER_STOP and proj.halted_at > 0
    last = proj.lineage[-1]
    assert last["action"] == "halt" and last["phase"] == before.value


def test_clear只对user_stop生效(proj):
    proj.clear_user_stop("没停过")          # 没停过 ⇒ 什么都不该发生
    assert proj.halted_reason == ""
    assert not [e for e in proj.lineage if e.get("action") == "resume"]
    proj.mark_user_stop("停")
    proj.clear_user_stop("恢复")
    assert proj.halted_reason == "" and proj.halted_at == 0.0
    assert proj.lineage[-1]["action"] == "resume"


def test_派生那一半不靠任何清标记代码(proj):
    """🔴 **这条的"绿"就是设计本身**：把失败任务重试成 PENDING 之后，停滞**自己就没了**。

    变异咬法：把 `halt_state` 里 `failed_task_ids` 那一支改成读一个落盘字段 ⇒
    这条会红（没人去清那个字段）。
    """
    bad = _mk_task(proj, TaskStatus.FAILED)
    assert P.halt_state(proj)["halted"] is True
    tracker.transition(bad, TaskStatus.PENDING)      # 人按了①重试
    assert P.halt_state(proj)["halted"] is False, "重试之后停滞没解除 —— 标记成了僵尸"


def test_落盘往返(proj):
    """`mark_user_stop` → `save` → `load`：重启后"人喊过停"这件事仍然成立。"""
    proj.mark_user_stop("停")
    P.save(proj)
    assert P.load(proj.id).halted_reason == P.HALT_USER_STOP


def test_停的项目文件里没混进别的键(proj):
    """新字段别把 `unknown_fields_dropped` 触发到别的字段上（回归面最小的那条）。"""
    proj.mark_user_stop("停")
    P.save(proj)
    raw = json.loads(P._path(proj.id).read_text(encoding="utf-8"))
    assert set(raw) <= {f.name for f in __import__("dataclasses").fields(P.ProjectState)}


# ── ④ 拦截点：`orchestrator._advance_project` ───────────────────
#
# 夹具照抄 `test_project_phase_advance.py` 的 `_setup` —— 它把项目与任务都造在
# monkeypatch 过的 QIDIAN_DIR 下，`read_task` 走得到。这里不重造一套。

def _setup_advance(tmp_path, monkeypatch, statuses):
    from singularity.scheduler import config
    from singularity.scheduler import orchestrator as orch
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    p = P.ProjectState(
        id="proj_halt", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = P.Phase.EXECUTING
    (tmp_path / "tasks").mkdir(exist_ok=True)
    for i, st in enumerate(statuses):
        t = tracker.Task(id=f"h{i}", description=f"任务 h{i}")
        t.status = st
        (tmp_path / "tasks" / f"h{i}.json").write_text(
            json.dumps(t.to_dict() if hasattr(t, "to_dict") else t.__dict__, default=str),
            encoding="utf-8")
        p.task_ids.append(f"h{i}")
    monkeypatch.setattr(P, "list_all", lambda: [p])
    monkeypatch.setattr(P, "save", lambda _p: None)
    return p, orch


def test_人工叫停后不推进_哪怕任务全绿(tmp_path, monkeypatch):
    """🔴 这一条就是 `round-20260926` 那个现场：任务都到终态了，但人喊过停。

    变异：删掉 `_advance_project` 开头那句 `if halt["halted"]: return` ⇒ 相位变 INTEGRATING。
    """
    p, orch = _setup_advance(tmp_path, monkeypatch, [TaskStatus.DONE] * 2)
    p.mark_user_stop("测试")
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is P.Phase.EXECUTING, "叫停了还往前走"
    assert any(i.get("kind") == "project_stalled" and i["reason"] == "user_stop"
               for i in p.issues), p.issues


def test_人工叫停后连任务都不拆(tmp_path, monkeypatch):
    """🔴 **早退必须在拆任务之前** —— `_decompose_and_create_tasks` 第一件事就是
    `project.task_ids = []` 然后整批重建，放它后面等于"停了个寂寞"。

    变异：把早退那三行挪到 `if not proj.task_ids:` **之后** ⇒ 本条红。
    """
    p, orch = _setup_advance(tmp_path, monkeypatch, [])
    p.mark_user_stop("测试")
    called = []
    monkeypatch.setattr(orch, "_decompose_and_create_tasks",
                        lambda *a, **k: called.append(1))
    orch._auto_trigger_test_fix({}, [])
    assert called == [], "叫停的项目还被拆了一批任务出来"
    assert p.task_ids == []


def test_停滞解除时issue要撤掉(tmp_path, monkeypatch):
    """派生那一半人一重试就没了 —— 但**issue 不会自己消失**，必须显式撤票。

    没有撤票的话 `project_stalled` 会赖在 issues 里，界面永远在说"已停" ——
    那是"僵尸标记"换了个地方长。
    """
    p, orch = _setup_advance(tmp_path, monkeypatch, [TaskStatus.FAILED])
    orch._auto_trigger_test_fix({}, [])
    assert any(i.get("kind") == "project_stalled" for i in p.issues), "先得有票"

    tracker.transition("h0", TaskStatus.PENDING)        # 人按了①重试
    orch._auto_trigger_test_fix({}, [])
    assert not [i for i in p.issues if i.get("kind") == "project_stalled"], \
        "停滞解除了，票还挂着"


def test_撤了票之后还能正常推进(tmp_path, monkeypatch):
    """**反方向对照**：解除之后别把推进也一起闸死了。"""
    p, orch = _setup_advance(tmp_path, monkeypatch, [TaskStatus.FAILED, TaskStatus.DONE])
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is P.Phase.EXECUTING
    # ⚠️ 两步走：`failed→done` 是被状态机**拒绝**的（`_TERMINAL_EXIT` 只放 PENDING），
    #    这正是"人重试"那条路的形状 —— 先回 PENDING，再跑成 DONE。
    tracker.transition("h0", TaskStatus.PENDING)
    tracker.transition("h0", TaskStatus.DONE)
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is P.Phase.INTEGRATING, "全绿了还不动 —— 闸门关死了"
