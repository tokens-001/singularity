"""`POST /api/projects/<id>/stop` —— 停这个项目（2026-09-21 真机，用户拍板）。

**来历**：那天想把一整轮停下来，发现**没有这个开关**。先试 `POST /api/loop/stop`，
**停不住** —— 观察者建/路由任务那句末尾写着「确保调度循环在跑」，19:15 停的循环
19:17 就被拉回来派了新活。最后只能**杀进程**（连 `SIGTERM` 都杀不动，要 `-9`）。

🔵 实现之所以这么小，是因为一条**关键事实**：任务一旦进了 `_exec.run()` 的回合循环，
就**不再经过调度循环**了（它自己一轮一轮调 `dispatch()`）。所以"停循环"拦不住正在跑的，
只有**取消标记**拦得住（`_check_cancelled` 每轮开头读）。

钉四条 —— ⚠️ **它们的"红"来自哪儿要说清**：`project_stop` 自己**只有一句实质代码**
（把每个任务喂给已有的 `task_cancel`），所以下面 ①②③ 三条钉的其实是
**"那一句在场且喂对了对象"**，`task_cancel` 内部的分支行为由它自己的测试管。
这是有意的：两份逻辑迟早会漂，所以这里**不复制**那套判定。

  ① 在跑的 ⇒ 拿到取消标记，**而且 `_check_cancelled` 真的拦得下它**（这条最要紧：
     只断言"文件写出来了"是自检自己 —— 文件写了没人读，正是这个仓最恨的形状）
  ② 没轮到的 ⇒ 转终态。**不留"可调度"的活口** —— 留着的话循环下一 tick 照旧派它，
     派下去才发现被取消，白搭一次 worktree 准备
  ③ 终态 ⇒ 一个都不碰（已交付的不能被改死）
  ④ HTTP 那条路真的通（函数对 ≠ 接线通；把路由删掉 ⇒ 红）
"""
import json

import pytest

from singularity.scheduler import config, tracker
from singularity.scheduler import project as P
from singularity.scheduler.tracker import TaskStatus


@pytest.fixture
def proj(tmp_path):
    return P.create(name="_t_stop", template="feature", description="x")


def _mk_task(proj, status, desc="[T1] 实现某模块: 创建 x.py"):
    """建一个任务**并挂到项目上** —— `tracker.create` 不会自己进 `proj.task_ids`，
    漏了这一步 `project_stop` 扫不到它（第一版夹具就漏了，四条全红）。"""
    t = tracker.create(desc, depth=0, project_id=proj.id)
    proj.task_ids = list(proj.task_ids or []) + [t.id]
    P.save(proj)
    if status != TaskStatus.PENDING:
        tracker.transition(t.id, status)
    return tracker.read_task(t.id)


def test_在跑的写取消标记_而且真的拦得下(tmp_path, proj):
    t = _mk_task(proj, TaskStatus.RUNNING)
    from singularity.scheduler._api_projects import project_stop
    res, code = project_stop(proj.id)
    assert code == 200 and res["stopped"] == 1, res

    assert (config.CANCEL_DIR / f"{t.id}.json").exists(), "在跑的任务没拿到取消标记"
    # 🔴 **接线**：文件写了不等于有人读。这一跳才是"停"真正生效的地方。
    from singularity.scheduler._exec import _check_cancelled
    out = _check_cancelled(tracker.read_task(t.id), [])
    assert out is not None, "标记写了，但执行器读不到 —— 那这个 stop 是装饰"
    assert out.term_reason == "cancelled_by_user", out.term_reason


def test_没轮到的转终态_不留可调度的活口(tmp_path, proj):
    """PENDING/BLOCKED 如果不转终态，循环下一 tick 照旧派它 —— 派下去才发现被取消。"""
    t = _mk_task(proj, TaskStatus.PENDING)
    from singularity.scheduler._api_projects import project_stop
    res, _ = project_stop(proj.id)
    assert res["stopped"] == 1, res
    after = tracker.read_task(t.id)
    assert after.status == TaskStatus.FAILED, f"还留着一个会被派下去的活口：{after.status}"


def test_终态一个都不碰(tmp_path, proj):
    done = _mk_task(proj, TaskStatus.DONE)
    failed = _mk_task(proj, TaskStatus.FAILED)
    live = _mk_task(proj, TaskStatus.RUNNING)
    from singularity.scheduler._api_projects import project_stop
    res, _ = project_stop(proj.id)
    assert res["stopped"] == 1 and res["terminal"] == 2, res
    assert tracker.read_task(done.id).status == TaskStatus.DONE, "把已交付的改死了"
    assert tracker.read_task(failed.id).status == TaskStatus.FAILED
    assert (config.CANCEL_DIR / f"{live.id}.json").exists()


def test_项目不存在(tmp_path):
    from singularity.scheduler._api_projects import project_stop
    res, code = project_stop("nope-000")
    assert code == 404


def test_HTTP路由接通(tmp_path, proj):
    """**函数对 ≠ 接线通**：路由没挂上，界面/脚本就调不到它。"""
    t = _mk_task(proj, TaskStatus.RUNNING)
    from singularity.web.app import app
    r = app.test_client().post(f"/api/projects/{proj.id}/stop")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body["ok"] and body["stopped"] == 1, body
    assert (config.CANCEL_DIR / f"{t.id}.json").exists(), "路由通了但没真停"
