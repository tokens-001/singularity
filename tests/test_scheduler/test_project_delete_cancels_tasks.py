"""删项目**要把它的任务一起停掉** —— 不然它们会在重启时被捞回来接着烧钱。

🔴 2026-09-19 真机踩的：`proj_mod.delete` 只清项目那几份文件，任务一个不动
⇒ 杀掉项目 + 重启后端，任务被回收成 PENDING **又派下去**，烧了 9 分钟、
还和别的轮抢模型。「弃用项目」和「停掉它的任务」在代码里从来不是一件事。

⚠️ 同时钉住**另一半**：任务 JSON 一个都不许删。锚
`refs/qidian/pending/<task_id>` 打在**项目仓**上，而任务文件是"这个锚是什么"的
唯一线索（描述/状态/父项目）—— 删了就只剩查无来处的孤儿 ref。
（`DELETE /api/tasks/<id>` 那条路是反例：它显式 `_release_ref`，**产物真丢**。）
"""

import json

import pytest

from singularity.scheduler import _api_projects, config, tracker, witness
from singularity.scheduler.project import create, load


@pytest.fixture
def _warns(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(witness, "warn", lambda *a, **k: got.append(a + (k,)))
    return got


def _project_with(*statuses):
    """建一个项目，挂上若干任务，各置成指定状态。返回 (项目 id, [任务 id])。"""
    p = create("删项目测试", template="product_dev")
    ids = []
    for i, st in enumerate(statuses):
        t = tracker.create(f"任务{i}", project_id=p.id)
        if st is not tracker.TaskStatus.PENDING:
            tracker.transition(t.id, st)
        ids.append(t.id)
    p.task_ids = list(ids)
    from singularity.scheduler.project import save
    save(p)
    return p.id, ids


def test_pending_tasks_are_cancelled(_warns):
    pid, tids = _project_with(tracker.TaskStatus.PENDING)
    body, code = _api_projects.project_delete(pid)
    assert code == 200 and body["ok"]
    assert body["cancelled"] == 1
    assert tracker.read_task(tids[0]).status is tracker.TaskStatus.FAILED, \
        "PENDING 任务没被取消 —— 重启后它还是会被派下去"


def test_running_tasks_get_a_cancel_file(_warns):
    """RUNNING 那条路不是改状态，是**写取消信号**（工人下一轮才看得到）。"""
    pid, tids = _project_with(tracker.TaskStatus.RUNNING)
    _api_projects.project_delete(pid)
    cancel_file = config.CANCEL_DIR / f"{tids[0]}.json"
    assert cancel_file.exists(), "RUNNING 任务没拿到取消信号 —— 它会一直跑到底"


def test_task_json_survives(_warns):
    """🔴 任务文件必须留着 —— 它是锚的唯一线索。删了就只剩孤儿 ref。"""
    pid, tids = _project_with(tracker.TaskStatus.DONE, tracker.TaskStatus.FAILED)
    _api_projects.project_delete(pid)
    for tid in tids:
        assert tracker.read_task(tid) is not None, \
            "任务文件被删了 —— 那条锚变成查无来处的孤儿 ref（产物还在，没人知道它是什么）"


def test_terminal_tasks_are_not_touched(_warns):
    """边界：已是终态的任务不该被改成别的状态（`task_cancel` 会回 400，别把它当取消算）。"""
    pid, tids = _project_with(tracker.TaskStatus.DONE)
    body, _ = _api_projects.project_delete(pid)
    assert body["cancelled"] == 0
    assert tracker.read_task(tids[0]).status is tracker.TaskStatus.DONE


def test_leftovers_are_reported_not_silent(_warns):
    """留下的任务要**出声** —— 别让孤儿悄悄攒（那条就是这个病的根）。"""
    pid, tids = _project_with(tracker.TaskStatus.PENDING)
    body, _ = _api_projects.project_delete(pid)
    assert body["left_tasks"] == 1
    keys = [k.get("key") for _, _, k in _warns]
    assert "project_deleted_left_tasks" in keys, f"没报：{_warns}"


def test_missing_project_is_404_and_writes_nothing(_warns):
    body, code = _api_projects.project_delete("根本不存在")
    assert code == 404 and body["ok"] is False


def test_deleting_a_project_without_tasks_is_quiet(_warns):
    """没有任务就别报 —— 否则每次删项目都留一条告警，又变成刷屏源。"""
    pid, _ = _project_with()
    _api_projects.project_delete(pid)
    assert not [w for w in _warns if "left_tasks" in str(w)]


def test_fixture_really_isolates_the_write_paths(tmp_path):
    """自检：夹具真把落盘目录指到**本用例的** tmp 了。

    ⚠️ 第一版写的是"路径里含 `pytest` / `qidian-collect`" —— **那条掐不断**：
    conftest 收集期就把 `QIDIAN_DIR` 指到了 `qidian-collect-*`，所以**删掉本文件的
    夹具它照样绿**，等于没查。改成钉"指到了本用例的 tmp_path"。
    """
    assert config.QIDIAN_DIR == tmp_path
    assert config.CANCEL_DIR == tmp_path / "cancels"
    assert config.PAUSE_DIR == tmp_path / "pauses"
