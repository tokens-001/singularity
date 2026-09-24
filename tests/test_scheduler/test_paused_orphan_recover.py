"""重启后的 PAUSED 是孤儿 —— 它会把**整个调度循环**钉死（2026-09-25）。

来历：Qoder CN 外派审查（`docs/Qoder-审查-20260924.md` #4）报的，**我逐跳核过**。
它还是 ZCode 那批 #4 的**重现** —— `docs/扫bug-01-20260914.md` 09-14 就把它列为
"高危 5 条全是真的"之一，**一直没修**。

四跳缺一不可：
  ① `_INFLIGHT` 不含 PAUSED ⇒ `recover()` 不捞它（不当"崩溃要重跑"）；
  ② `_SCHEDULABLE` **含** PAUSED ⇒ `ready_tasks()` 每轮都把它算成就绪；
  ③ 而 `_dispatch_ready` 只有 `PENDING/BLOCKED→ROUTED` 和 `ROUTED→DISPATCHED`
     两个 CAS，两条都不认 PAUSED（`cas` 会比对当前状态）⇒ **永远派不下去**；
  ④ `_run_queue_v3` 的出口是 `if not remaining: break` ⇒ remaining 恒非空 ⇒ **不 break**。

⇒ 后果**不是"那个任务卡住"**：`run_queue()` 在 `web/app.py` 的
`while not _loop_stop.is_set():` **里面**，它不返回 ⇒ 停止位（`/api/loop/stop`）、
心跳落盘（`_write_loop_tick`）、周期对账（`reconcile_projects`）全都不再执行。
心跳停了，进程外的看门狗会把活着的进程当"装死"。
而 PAUSED 唯一的出口（worker 自己写回 RUNNING）要求 worker 还活着 ——
重启之后那个前提没了；`task_resume` 又只删标记不改状态 ⇒ 界面上点恢复也救不回来。

修法两条（见各自的注释）：
  · `_SCHEDULABLE` 去掉 PAUSED —— 它**压根派不动**，留在表里只有一个后果；
  · `recover()` 把孤儿 PAUSED 送回 PENDING，**保留暂停标记**（人暂停的意图一个字不动）。

变异验证：
  · 把 PAUSED 加回 `_SCHEDULABLE` ⇒ 第 1、3 条红；
  · 删掉 `recover()` 里那支 ⇒ 第 2 条红；
  · 给那支补一句 `retry_count += 1` ⇒ 第 2 条红（"它不是失败"那半）。
"""
import threading

from singularity.scheduler import tracker


def _paused_task(desc="暂停中的任务"):
    t = tracker.create(desc)
    tracker.transition(t.id, tracker.TaskStatus.PAUSED)
    return tracker.read_task(t.id)


def test_盘上有暂停任务时_它不许出现在就绪表里():
    """**这条钉的是"派不动就别留在表里"**。

    判据不是"能不能派"（那是 `_dispatch_ready` 的事），而是"它根本就不该是就绪态"：
    `cas` 比对当前状态，PAUSED 两条 CAS 都不中 ⇒ 留在 `ready` 里**只**制造一个后果
    —— 让 `if not remaining: break` 永远到不了。
    """
    t = _paused_task()
    ids = {x.id for x in tracker.ready_tasks()}

    assert t.id not in ids, (
        "PAUSED 又回到就绪表里了 —— 它派不动（cas 两条都不认它），"
        "留在表里只会让 `_run_queue_v3` 的 `if not remaining: break` 永远到不了")


def test_对照组_待处理的任务照旧在就绪表里():
    """**这条是对照**：别把 `_SCHEDULABLE` 整个掏空 —— 真该跑的照旧返回。"""
    t = tracker.create("正常待处理")
    ids = {x.id for x in tracker.ready_tasks()}

    assert t.id in ids, "PENDING 都进不了就绪表了 —— 这刀砍过头了"


def test_重启后孤儿暂停被送回pending_且不许算成失败():
    """`recover()` 是**进程刚起来**时跑的 ⇒ 那一刻"没有任何 worker 在跑它"是定义。

    三条断言，缺一不可：
      · 状态回到 PENDING（否则它永远出不来）；
      · `retry_count` 一个字不动（**它不是失败** —— 别跟 `_INFLIGHT` 那一支混）；
      · **暂停标记还在**（人暂停的意图要原样带过去；重派后 `_check_paused`
        在 turn 循环最开头、任何模型调用之前就会再暂停一次）。
    """
    t = _paused_task()
    marker = tracker.config.PAUSE_DIR / f"{t.id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"task_id": "%s", "auto": false}' % t.id, encoding="utf-8")
    before = tracker.read_task(t.id).retry_count

    tracker.recover()

    after = tracker.read_task(t.id)
    assert after.status == tracker.TaskStatus.PENDING, (
        f"重启后它还是 {after.status.value} —— 没有 worker 能再来碰它，"
        "它就是个死状态（`task_resume` 只删标记不改状态，点恢复也没用）")
    assert after.retry_count == before, (
        "把它算成失败重试了 —— 它只是被暂停，不是崩了；涨 retry_count 会挤掉真正的重试额度")
    assert marker.exists(), (
        "暂停标记被删了 —— 人暂停的意图被我们抹掉了，重派之后它会直接往下跑")


def test_接线_盘上只剩一条暂停任务时_调度循环必须返回():
    """**这条钉接线**（也是这条 bug 的真症状）：`run_queue` 必须能返回。

    上面两条各自只测函数，验不到"合起来那个死循环"——本仓栽过多次的
    「函数对 ≠ 接线通」。这里真起一次调度循环：盘上**只有**一条 PAUSED 任务，
    谁都派不动它，所以循环该立刻返回。

    变异：把 PAUSED 加回 `_SCHEDULABLE` ⇒ 这条**永远不返回**（8 秒后判红）。
    ⚠️ 红的代价是留一个空转线程（daemon，随进程退出）——它是 0.5s 一次的 sleep 循环，
       不吃 CPU，可以接受；总比"这条测不出东西"强。
    """
    from singularity.scheduler import orchestrator

    _paused_task()
    returned = threading.Event()

    def _run():
        orchestrator.run_queue({}, max_concurrent=1)
        returned.set()

    threading.Thread(target=_run, daemon=True).start()

    assert returned.wait(8.0), (
        "调度循环 8 秒没返回 —— 盘上只有一条 PAUSED 任务，谁都派不动它，"
        "而它一直留在 ready 表里 ⇒ `if not remaining: break` 永远到不了。"
        "后果不是那个任务卡住，是**整个循环不再回头**（停止位 / 心跳 / 对账全停）。")
