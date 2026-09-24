"""空壳 `BatchOutput` 不许顶掉真结果 —— `prev_batch` 原来是无条件赋值的。

出处：`~/OPEN.md` 🟡「`_budget_exhausted` 只拦 `≤0`」（详情 `docs/防御模式.md` §77.8）。
2026-09-18 复查把原文两处更正了：

  · **"cap=6s 注定空"是推断，没数据** —— 盘上只有**任务级** `elapsed`，没有每次派发的预算。
  · **真伤害不是那 6 秒，是丢账**：`_run_with_retry` 里 `prev_batch = batch` **无条件** ⇒
    第 1 轮真干活、第 2 轮（预算是 `6s` 这种**正数**，`_budget_exhausted` 不拦）拿到空壳、
    第 3 轮撞到点 ⇒ `return prev_batch` **交回的是空壳**，第 1 轮那份真账整份丢。

上面的 guard（"下一轮什么都别发起"）和这里（"这一轮的空壳别顶掉上一轮"）是**同一件事的两半** ——
原来只修了上半句。用户 2026-09-18 拍板走"修伤害面"这条路（不用猜阈值）。

这些测试都**钉接线**：把 `_batch_has_facts` 这个条件去掉（退回无条件赋值），测试必须红。
"""
from types import SimpleNamespace as NS

from singularity.scheduler import _exec as X


def _er(files=(), tokens=0):
    return NS(changed_files=list(files), token_count=tokens, tool_events=[])


def _batch(files=(), tokens=0, merge_request=None, dispatch_result="有", turn_count=0,
           deadline_wrapup=False):
    """`dispatch_result="有"` 是哨兵：真传 None 表示"压根没发起过调用"那种空壳。"""
    dr = NS(executor_result=_er(files, tokens)) if dispatch_result == "有" else None
    return NS(ok=False, task_id="T1", term_reason="", dispatch_result=dr,
              merge_request=merge_request, tool_events=[], turn_count=turn_count,
              deadline_wrapup=deadline_wrapup, planner_decomposed=False)


# ═══════════════════════════════════════════════════════════════
# ① 判据本身
# ═══════════════════════════════════════════════════════════════

def test_有产物就算有事实():
    assert X._batch_has_facts(_batch(files=["a.py"])) is True


def test_花过钱也算有事实():
    """只有 token、没文件 —— 钱是真花了的，账不能丢。"""
    assert X._batch_has_facts(_batch(tokens=1200)) is True


def test_有合并请求算有事实():
    """worktree 里已 commit 并构了合并请求 —— 最硬的那种事实（哪怕文件列表是空的）。"""
    assert X._batch_has_facts(_batch(merge_request=NS())) is True


def test_压根没发起过调用的空壳():
    """`dispatch_result=None`：一具连调用都没发起的空壳（预算 ≤0 时 `run()` 立刻收尾那种）。"""
    assert X._batch_has_facts(_batch(dispatch_result=None)) is False


def test_发起了但什么都没干出来的空壳():
    """0 文件 / 0 token —— 预算 6 秒那种，发起了、活着 39 毫秒、什么也没留下。"""
    assert X._batch_has_facts(_batch()) is False


def test_轮次不能当判据():
    """🔴 **这条是给"下次改的人"写的**：`turn_count=1` 的批次**仍然是空壳**。

    写这条判据时最容易想到的就是"跑过至少一轮就算有事实" —— 而**它正好把要修的那个
    空壳放回来**：预算 6 秒那次**确实发起了**一轮，`turn_count` 就是 1，
    交回的却是 0 文件 0 token。**判据必须是"留下了什么"，不是"跑了多久"。**

    把 `_batch_has_facts` 改成看 `turn_count > 0`（或加进 or 链里）⇒ 红。
    """
    b = _batch(turn_count=1)          # 跑过一轮
    assert b.turn_count == 1, "前置：这个批次确实跑过一轮"
    assert X._batch_has_facts(b) is False, (
        "跑过一轮 ≠ 留下了东西 —— 拿轮次当判据，那个 39 毫秒的空壳就又被放回来了")


def test_只读了文件没有产物也算空壳():
    """`tool_events` **不**进判据：只读文件、改了又回滚的那些轮确实"发生了点什么"，
    但拿它去换掉一份**真交了文件**的账，是净亏。"""
    b = _batch()
    b.tool_events = [{"tool": "read_file", "status": "done"}]
    assert X._batch_has_facts(b) is False


# ═══════════════════════════════════════════════════════════════
# ② 接线：`_run_with_retry` 交出去的到底是哪一份
# ═══════════════════════════════════════════════════════════════

def _drive(monkeypatch, batches, exhausted):
    """把 `_run_with_retry` 跑起来 —— `run()` 和"表到点没"都换成排好的剧本。"""
    it = iter(batches)
    monkeypatch.setattr(X, "run", lambda *a, **k: next(it))
    ex = iter(exhausted)
    monkeypatch.setattr(X, "_budget_exhausted", lambda ctx: next(ex))
    # ⚠️ 剧本只写**真正会被问到的那几次**：`retry=0` 时 `prev_batch is None`
    # 会把 guard 短路掉（`and` 的左边），`_budget_exhausted` **一次都不调**。
    # 多写一格不报错、只是永远用不到；少写一格 `next()` 直接 StopIteration 报出来。
    monkeypatch.setattr(X.val_mod, "post_execution_hook", lambda *a, **k: {})
    task = NS(id="T1", max_retries=2, description="t")
    # ⚠️ `merge_queue` 必须**非 None**：None 走 v2（真去主仓 rollback），
    # 这条测试不该碰任何真仓。给个哨兵走 v3 分支（"主仓未动"）。
    return X._run_with_retry(task, NS(retry_count=0, merge_queue=object()), {})


def test_第一轮真干活_第二轮空壳_交回第一轮那份(monkeypatch):
    """**正题** —— 这就是 09-18 真机上丢账的那条路。

    第 1 轮：交了 `a.py` + 100 token（真账）
    第 2 轮：空壳（预算只剩 6 秒，发起了、什么也没留下）
    第 3 轮：表到点 ⇒ guard 交回 `prev_batch`

    把 `prev_batch` 那行的条件去掉（退回无条件赋值）⇒ 交回的是空壳 ⇒ 红。
    """
    real = _batch(files=["a.py"], tokens=100)
    got = _drive(monkeypatch, [real, _batch()], exhausted=[False, True])
    assert got is real, "交回来的是那个空壳 —— 第 1 轮真干出来的账整份丢了"
    assert got.dispatch_result.executor_result.changed_files == ["a.py"]


def test_第一轮就是空壳时照常交给它(monkeypatch):
    """**边界**：`prev_batch is None` 时没有"更好的上一份"可留，空壳就空壳。

    ⚠️ 这条**不是**在说"空壳可以接受"，是在说那一格本来就没有东西可留 ——
    真没干活的任务，账上就该是 0（`_account_salvaged` 那侧管"钱花没花"）。
    """
    shell = _batch()
    got = _drive(monkeypatch, [shell], exhausted=[True])
    assert got is shell


def test_后一轮更满就换成后一轮的(monkeypatch):
    """**反过来也要对**：第 2 轮交的东西**更多**，就该用第 2 轮的（它才是最新的磁盘事实）。

    只判"非空"是不够的 —— 别把这条修成"第一份锁死"。
    """
    first = _batch(files=["a.py"])
    second = _batch(files=["a.py", "b.py"])
    got = _drive(monkeypatch, [first, second], exhausted=[False, True])
    assert got is second


# ═══════════════════════════════════════════════════════════════
# ③ 分档：弱的不许顶掉强的（2026-09-20 真机 round-20260920b 的 T3/T4/T6）
#
# 上面 ①② 修的是"0 产物**且**0 token"的空壳。真机补的那一刀是**另一种空壳**：
# 它**烧了 4773~4920 token**、0 个文件 —— 旧判据是个 bool，照样返回 True ⇒
# 它把上一轮带 `merge_request`（pending ref 上 +1008 行）的那份顶掉了。
# ⇒ 判词「无文件改动」⇒ `orchestrator` 只看 `batch.merge_request`，None ⇒ 产物进不了合并队列。
# ═══════════════════════════════════════════════════════════════

def test_分档_合请最硬_文件次之_只烧钱最软():
    assert X._batch_evidence(_batch(merge_request=NS())) == 3
    assert X._batch_evidence(_batch(files=["a.py"])) == 2
    assert X._batch_evidence(_batch(tokens=4920)) == 1
    assert X._batch_evidence(_batch()) == 0
    assert X._batch_evidence(_batch(dispatch_result=None)) == 0


def test_只烧钱的那一轮_顶不掉带合并请求的那一轮(monkeypatch):
    """**正题（真机的形状）** —— `round-20260920b` T3：

    第 1 轮：真干完，`merge_request` 建好、ref 锚上（`changed_files` 是空的也不影响）
    第 2 轮：表到点又起了一轮，烧了 4920 token、0 个文件
    第 3 轮：guard 交回 `prev_batch`

    ⇒ 交回的必须是**第 1 轮**那份。把 `_batch_evidence(batch) >= _batch_evidence(prev_batch)`
    改回 `_batch_has_facts(batch)`（bool）⇒ 第 2 轮的空壳顶掉第 1 轮 ⇒ 红。
    """
    real = _batch(merge_request=NS(), tokens=100)
    burn = _batch(tokens=4920)
    got = _drive(monkeypatch, [real, burn], exhausted=[False, True])
    assert got is real, (
        "只烧钱的那一轮把带合并请求的顶掉了 —— 产物还在 pending ref 上，而这条路交出去的是空壳")
    assert got.merge_request is not None


def test_撞预算收尾时交回的是手里更硬的那份(monkeypatch):
    """**第二半**：`deadline_wrapup` 那条 return 原来是**无条件**交 `batch` 的。

    即使上面那行已经把 `prev_batch` 保住了，这一句照样把空壳交出去 ——
    于是产物**永远到不了** `orchestrator` 的 `if batch.merge_request: mq.submit(...)`。

    把 `if prev_batch is not None and _batch_evidence(prev_batch) > ...` 那两句删掉
    （退回无条件 `return batch`）⇒ 红。
    """
    real = _batch(merge_request=NS(), tokens=100)
    wrapup_shell = _batch(tokens=4812, deadline_wrapup=True)   # T6 那个数
    got = _drive(monkeypatch, [real, wrapup_shell], exhausted=[False])
    assert got is real, "撞预算收尾交回的是那个空壳 —— 真产物到不了合并队列"
    assert got.merge_request is not None


def test_撞预算时手里没有更硬的_照旧交收尾那一份(monkeypatch):
    """**边界**：手里那份不比收尾这份硬时，别自作主张换 —— 收尾那份才是最新的账。"""
    first = _batch(tokens=100)
    wrapup = _batch(tokens=4920, deadline_wrapup=True)
    got = _drive(monkeypatch, [first, wrapup], exhausted=[False])
    assert got is wrapup


# ═══════════════════════════════════════════════════════════════
# ④ **重试用尽**那一支漏了同一刀（2026-09-25，Qoder 外派审查 #1）
#
# 上面 deadline 那一支 09-20 修好了，而"重试次数用完"那一支还是**无条件交 `batch`**
# —— 同一个函数里同样的形状，修了一半。真机上这条更常见：`max_retries=3` ⇒ 最多跑 4 轮，
# 而"最后一轮只想不写"是常态（`~/OPEN.md`：「15 个失败任务全败在没产出」）。
# ═══════════════════════════════════════════════════════════════

def test_重试用尽时交回手里更硬的那份(monkeypatch):
    """**正题**：前几轮有产物，最后一轮零产出 ⇒ 交回的必须是那份有产物的。

    `max_retries=2` ⇒ 跑满 3 轮才走到 `retry > task.max_retries`。
    变异：把这一支改回无条件 `return batch` ⇒ 交回的是那个只烧钱的空壳 ⇒ 红。
    """
    real = _batch(merge_request=NS(), tokens=100)   # 第 1 轮真干完了
    burn1 = _batch(tokens=1000)                      # 第 2 轮只烧钱
    burn2 = _batch(tokens=4000)                      # 第 3 轮也只烧钱
    got = _drive(monkeypatch, [real, burn1, burn2], exhausted=[False, False])

    assert got is real, (
        "重试用尽交回的是最后那个空壳 —— 前几轮的产物还在 pending ref 上，"
        "而 `orchestrator` 只看 `batch.merge_request`，None ⇒ 产物进不了合并队列")
    assert got.merge_request is not None


def test_边界_重试用尽时最后一轮更硬就交它_别锁死第一份(monkeypatch):
    """**对照**：别把这一支修成"永远交第一份" —— 最后那轮更硬时它才是最新的磁盘事实。"""
    first = _batch(files=["a.py"], tokens=100)        # 档 2
    last = _batch(merge_request=NS(), tokens=100)     # 档 3
    got = _drive(monkeypatch, [first, _batch(tokens=5), last], exhausted=[False, False])

    assert got is last
