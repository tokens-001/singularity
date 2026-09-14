"""建任务 + 登记进项目：**要么一起成、要么一起不成**。

本仓有**三处**「先 `create`、后登记进 `project.task_ids`」的写法：
  · `_api_tasks.task_submit`（单条）
  · `_workflow_phases._run_execution`（批量）
  · `orchestrator._decompose_and_create_tasks`（批量，中间还多一步 `transition`）

中间那步一抛，任务就落在盘上、而**项目不认识它** —— 项目页数的是 `task_ids`、
orchestrator 也只认 `task_ids` ⇒ **它永远不会被派发**，可从界面上看它就是一条
正常的 pending。是 §65 那个"状态说有、其实没人管"的同族。

三条都是**接线**测试：让"登记"那步抛，断言**盘上没留下那个任务**。
⚠️ 只测 `tracker.rollback_create` 本身是不够的 —— **函数对 ≠ 接线通**
（外派⑬ 报过同款假绿：解析器钉得很干净，而调用点那句接线删掉照样全绿）。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import witness
from singularity.scheduler import _api_tasks
# ⚠️ `workflow` 必须排在 `_workflow_phases` **前面**：后者 import 前者，
# 而前者在末尾又 `from ..._workflow_phases import *` —— 谁先被导入谁吃亏。
# （同族那条更硬的记在 OPEN.md：`_dispatch_exec` 单独导入会炸、故意不修。）
from singularity.scheduler import workflow  # noqa: F401
from singularity.scheduler import _workflow_phases as wp


def _mk_state(pid: str = "proj1") -> "proj_mod.ProjectState":
    return proj_mod.ProjectState(
        id=pid, name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )


def _isolate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")


def _task_files(tmp_path) -> list[str]:
    d = tmp_path / "qidian" / "tasks"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _collect_warns(monkeypatch) -> list[str]:
    got: list[str] = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **kw: got.append(msg))
    return got


# ═══════════════════════════════════════════════════════════════
# ① 单条：POST /api/tasks 那个入口
# ═══════════════════════════════════════════════════════════════

def test_task_submit_登记失败要撤回(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p = _mk_state()
    monkeypatch.setattr(proj_mod, "load", lambda _id: p)

    def _boom(_p):
        raise OSError("磁盘满了")

    monkeypatch.setattr(proj_mod, "save", _boom)
    warns = _collect_warns(monkeypatch)

    res, code = _api_tasks.task_submit("测试：建了任务但登记失败", project_id=p.id)

    assert code == 500 and "error" in res, f"没如实告诉调用方没建成: {res} / {code}"
    assert _task_files(tmp_path) == [], \
        "任务文件还在 ⇒ 项目不认识它，它永远不会被派发（而界面看着就是一条正常 pending）"
    assert any("task_attach_failed" in w for w in warns), f"炸了没出声: {warns}"


# ═══════════════════════════════════════════════════════════════
# ② 批量：_run_execution 拆解落任务
# ═══════════════════════════════════════════════════════════════

def test_run_execution_登记失败要撤掉这一批(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: tmp_path)
    p = _mk_state()
    p.architecture = {
        "constraints": [],
        "tasks": [{"id": "T1", "title": "实现 X", "description": "创建 x.py", "layer": "impl"},
                  {"id": "T2", "title": "实现 Y", "description": "创建 y.py", "layer": "impl"}],
    }

    def _boom(proj):
        # ⚠️ 只让"登记完那一次 save"炸：前面的 save 提前炸的话，任务还没建出来，
        # 这条测试会**假绿**（盘上当然没有文件）。
        if proj.task_ids:
            raise OSError("磁盘满了")

    monkeypatch.setattr(wp, "save", _boom)
    _collect_warns(monkeypatch)

    with pytest.raises(OSError):
        wp._run_execution(p, agents={})

    assert _task_files(tmp_path) == [], "这一批建出来的任务没撤掉 ⇒ 它们永远不会被派发"


# ═══════════════════════════════════════════════════════════════
# ③ 批量：_decompose_and_create_tasks（GATE2 批准那条兜底路）
# ═══════════════════════════════════════════════════════════════

def test_decompose_登记失败要撤掉这一批(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: tmp_path)
    p = _mk_state()
    p.architecture = {
        "tasks": [{"id": "T1", "title": "实现 X", "description": "创建 x.py", "layer": "impl"}],
    }

    def _boom(proj):
        if proj.task_ids:
            raise OSError("磁盘满了")

    monkeypatch.setattr(proj_mod, "save", _boom)
    warns = _collect_warns(monkeypatch)

    orch._decompose_and_create_tasks(p, {})      # 这条路自己吞异常（不往上抛）

    assert _task_files(tmp_path) == [], "这一批建出来的任务没撤掉 ⇒ 它们永远不会被派发"
    assert any("created_task_rolled_back" in w for w in warns), f"撤了没出声: {warns}"


# ═══════════════════════════════════════════════════════════════
# 撤回原语本身
# ═══════════════════════════════════════════════════════════════

def test_rollback_删不掉就退化成显式失败(tmp_path, monkeypatch):
    """删文件都可能失败（权限 / 占用）。那时**不许算了** —— 退化成一条显式的 FAILED。

    留着一条"看着像待办、实际没人管"的 pending，比留一条写清原因的失败坏得多。
    """
    from singularity.scheduler import tracker

    _isolate(tmp_path, monkeypatch)
    t = tracker.create("测试：撤不掉的任务")
    assert _task_files(tmp_path) == [f"{t.id}.json"]

    def _boom(_self, *a, **k):
        raise OSError("权限不足")

    monkeypatch.setattr(type(tracker._path(t.id)), "unlink", _boom, raising=False)
    warns = _collect_warns(monkeypatch)

    tracker.rollback_create([t.id], why="测试")

    assert any("rollback_unlink_failed" in w for w in warns), f"删不掉没出声: {warns}"
    assert tracker.read_task(t.id).status is tracker.TaskStatus.FAILED, \
        "既没撤掉、也没标失败 ⇒ 留下一条没人管的 pending"
