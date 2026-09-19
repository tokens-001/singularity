"""「上游失败、降级运行」这个标记的**接线**：写侧 (`tracker.ready_tasks`) ↔ 读侧
(`workflow._flag_degraded_tasks`)。

现有那两条测试（`test_silent_failure_invariants.py::TestDegradedDependencyIsVisible`）
**自己往 `tracker.transition(error=...)` 里塞了那句措辞** —— 也就是说它们钉住的是
「读侧认得这个子串」，**写侧改词一个字都不会红**（同仓 `§` 那个"测了函数没测接线"的形状）。

这里从**写侧真实的入口**（`ready_tasks()`）走一遍。
"""
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


def _upstream_failed(project_id: str = "p1"):
    up = tracker.create("上游实现", project_id=project_id)
    tracker.transition(up.id, TaskStatus.FAILED, error="boom")
    return up


def test_降级标记落盘_pending任务():
    """任务**不是 BLOCKED**（PENDING/ROUTED）时，`ready_tasks()` 设的那句 error
    也必须落到盘上 —— 读侧是 `read_task()`（从盘上读），内存里改一下等于没改。"""
    up = _upstream_failed()
    down = tracker.create("下游测试", project_id="p1", depends_on=[up.id])

    ready_ids = [t.id for t in tracker.ready_tasks()]
    assert down.id in ready_ids, "降级任务该照常可调度（不级联失败是有意设计）"

    on_disk = tracker.read_task(down.id)
    assert "降级运行" in str(getattr(on_disk, "error", "") or ""), \
        "内存里设了、没落盘 ⇒ 读侧（read_task）永远看不见这条标记"


def test_读侧认得写侧的那句话():
    """`_flag_degraded_tasks` 认的是 `ready_tasks()` 真写出来的那句话。"""
    from singularity.scheduler import workflow as W
    from singularity.scheduler.project import ProjectState

    up = _upstream_failed()
    down = tracker.create("下游测试", project_id="p1", depends_on=[up.id])
    tracker.ready_tasks()                      # 写侧：标记降级

    p = ProjectState(id="p1", name="t")
    p.task_ids = [down.id]
    p.issues = []
    W._flag_degraded_tasks(p)
    assert "degraded_dependency" in [i.get("type") for i in p.issues], \
        "写侧写了、读侧没认出 ⇒ 人审页上看不见（GATE3 只显示『机械检查全过』）"
