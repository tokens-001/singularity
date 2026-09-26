"""`CONFLICT_HELD` 不是死路 —— 上游**卡住等人**，不是**死了**。

出处：Qoder CN 第二轮 #7（`docs/Qoder-审查-20260925-第二轮.md`），逐跳核过成立。

**病**：`_DEAD_END` 里含 `CONFLICT_HELD`，而**同一个文件**的 `is_terminal()` 的 docstring
明写它「**在这份表里从来就不是终态** …等人解决 merge 冲突，之后都要继续走」——
两条判据直接矛盾。而它确实是活的：`_cli_tasks` 的 `resolve(manual)` 把它推 **DONE**。

后果不止"早跑一步"：`_any_dead_dep` 那一支会把
「**上游依赖 X 已失败**」这句**假话落盘**给人审页看，并让下游**立刻起跑** ——
造在还没合进来的产物上。

钉三件事：
  ① 上游 `CONFLICT_HELD` ⇒ 下游**不可调度**、**不许**被打上"已失败"的标记；
  ② **别修过头**：上游真 `FAILED` ⇒ 照旧降级起跑 + 标记落盘（09-19 修过的那条，别推翻）；
  ③ 冲突解决之后下游**要能**被调度出来（不是把它永远挡住）。
"""
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus


def _upstream(status: TaskStatus, project_id="p-conflict"):
    up = tracker.create("上游实现", project_id=project_id)
    tracker.transition(up.id, status, error="boom")
    return up


def _downstream(up, project_id="p-conflict"):
    return tracker.create("下游集成", project_id=project_id, depends_on=[up.id])


# ═══════════════════════════════════════════════════════════════
# ① 上游卡在冲突 ⇒ 下游等，别替它宣布"上游已失败"
# ═══════════════════════════════════════════════════════════════

def test_上游卡在冲突_下游不许被判已失败并起跑():
    """变异：把 `TaskStatus.CONFLICT_HELD` 加回 `_DEAD_END` ⇒ 本条红。"""
    up = _upstream(TaskStatus.CONFLICT_HELD)
    down = _downstream(up)

    ready_ids = [t.id for t in tracker.ready_tasks()]
    assert down.id not in ready_ids, \
        "上游还在等人解冲突，下游就起跑了 —— 它要造在那份**还没合进来**的产物上"

    on_disk = tracker.read_task(down.id)
    err = str(getattr(on_disk, "error", "") or "")
    assert "已失败" not in err, \
        f"上游只是卡住，却落盘一句'上游已失败'给读人审页的人看：{err!r}"


# ═══════════════════════════════════════════════════════════════
# ② 别修过头：真失败的照旧降级起跑
# ═══════════════════════════════════════════════════════════════

def test_对照组_上游真失败照旧降级起跑():
    """变异：把 `TaskStatus.FAILED` 从 `_DEAD_END` 里拿掉 ⇒ 本条红。

    ⚠️ "不级联失败、降级起跑"是**有意设计**（返工循环会修），09-19 还专门修过
    "这句标记必须落盘"。这次只挪走 `CONFLICT_HELD`，**一个字节都不许碰这一支**。
    """
    up = _upstream(TaskStatus.FAILED)
    down = _downstream(up)

    ready_ids = [t.id for t in tracker.ready_tasks()]
    assert down.id in ready_ids, "真失败的上游该让下游降级起跑（不级联失败）"

    on_disk = tracker.read_task(down.id)
    assert "降级运行" in str(getattr(on_disk, "error", "") or ""), \
        "内存里设了、没落盘 ⇒ 读侧看不见"


def test_对照组_rolled_back也算死路():
    """**另一半**：`ROLLED_BACK` 同样不会再产出，照旧是死路。"""
    up = _upstream(TaskStatus.ROLLED_BACK)
    down = _downstream(up)
    assert down.id in [t.id for t in tracker.ready_tasks()], \
        "ROLLED_BACK 的上游不该把下游卡死"


# ═══════════════════════════════════════════════════════════════
# ③ 冲突解决之后，下游要能被调度出来
# ═══════════════════════════════════════════════════════════════

def test_冲突解决后下游才可调度():
    """**这条防的是"用一个更大的坑填掉原来的坑"**：把下游永远挡住也不行。"""
    up = _upstream(TaskStatus.CONFLICT_HELD)
    down = _downstream(up)
    assert down.id not in [t.id for t in tracker.ready_tasks()]

    tracker.transition(up.id, TaskStatus.DONE, error="")   # 人工 resolve 走的就是这条
    assert down.id in [t.id for t in tracker.ready_tasks()], \
        "上游已经 DONE 了，下游还是调度不出来 —— 挡住比早跑更坏"


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
