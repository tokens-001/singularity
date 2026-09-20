"""GATE3 打回 `impl` 时，返工要重置**哪些**任务（2026-09-20 · A 档 · 用户拍板）。

**改之前**：只重置 `DONE`。而 `tracker.ready_tasks` 只扫 `PENDING/ROUTED/BLOCKED`
⇒ **失败的任务原地不动、永远不会被重派** ⇒ 返工一次 = 把做成的推倒重做、
没做成的照旧缺 ⇒ 又冲回 GATE3 ⇒ 转圈。

⚠️ **这个改动打破了 `docs/防御模式.md` §82.3 的泄漏面核验**，所以本文件
**一半的用例在钉"停"标记的处置**，不是附加题：
那条核验的结论是「会重新派 FAILED 任务的路只有 `task_retry` / `tracker.recover`，
而 `handle_gate3_reject` **只重置 DONE** ⇒ 三条路都够不着取消标记」。
现在第四条路出现了，而超时那条路是**成对**写的（先写标记、再转 FAILED，
`orchestrator._reap_futures`）⇒ 标记不消费的话，重派的任务一上场就被
`_exec._check_cancelled` 判成"取消"，白跑一轮、账还记在"取消"上。

⚠️ **本文件不验真机**：判据是"盘上 status 真的变成 PENDING 了"，
而"变成 PENDING 之后调度循环真的把它派下去了"要靠真机（方案 §五 第 3 条）。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker
from singularity.scheduler import workflow as W
from singularity.scheduler.project import Phase
from singularity.scheduler.tracker import TaskStatus


def _mk(tmp_path, monkeypatch, statuses):
    """真 tracker + 真任务文件 + 真 QA 报告 —— 重置读的正是盘上那些东西。

    ⚠️ **不 mock `tracker`**：被测的就是"盘上 status 变了没有"，
    喂替身等于测替身（本仓踩过不止一次）。
    """
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    monkeypatch.setattr(config, "CANCEL_DIR", tmp_path / "qidian" / "cancels")
    (tmp_path / "qidian" / "projects").mkdir(parents=True, exist_ok=True)
    config.CANCEL_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(W, "save", lambda _p: None)      # 别真写项目目录
    tracker._invalidate_scan_cache()

    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    p.phase = Phase.GATE3

    ids = []
    for desc, st in statuses:
        t = tracker.create(desc, project_id="proj1")
        if st is not TaskStatus.PENDING:
            tracker.transition(t.id, st, force=True)
        ids.append(t.id)
    p.task_ids = ids

    # 路由判据读的是这份报告（`_resolve_fix_route`）—— 写一份真的，别 patch 路由函数
    (proj_mod._projects_dir() / "proj1.qa_report.json").write_text(
        json.dumps({"issues": [{"fix_route": "impl", "description": "x"}]}), encoding="utf-8")
    return p


def _marker(tid, body):
    (config.CANCEL_DIR / f"{tid}.json").write_text(body, encoding="utf-8")


def _status(tid):
    return tracker.read_task(tid).status


# ═══════════════════════════════════════════════════════════════
# 正题：失败的任务也得被捞回来
# ═══════════════════════════════════════════════════════════════

def test_失败的任务会被重置成PENDING(tmp_path, monkeypatch):
    """**这条就是 A 档本身**。删掉 impl 分支里的 `elif ... == FAILED` 那支 ⇒ 本条红。

    改之前：失败的任务原地不动 ⇒ `ready_tasks` 永远扫不到它 ⇒
    "欠的活"永远没人补，而做成的那个被推倒重做 —— 转圈的根因。
    """
    p = _mk(tmp_path, monkeypatch, [("做成的", TaskStatus.DONE), ("没做成的", TaskStatus.FAILED)])
    W.handle_gate3_reject(p, {}, "再来一遍")
    assert _status(p.task_ids[1]) is TaskStatus.PENDING, "失败的活没被重派 —— 转圈的根因还在"
    assert _status(p.task_ids[0]) is TaskStatus.PENDING


def test_两栏分开记进lineage(tmp_path, monkeypatch):
    """DONE 和 FAILED 必须分栏 —— 合成一个数的话，"返工补没补上欠的活"又读不出来了。

    「两栏相加 ≠ 全集」是这个仓反复吃亏的形状（09-20 计数判据那条）。
    """
    p = _mk(tmp_path, monkeypatch, [("做成的", TaskStatus.DONE), ("没做成的", TaskStatus.FAILED)])
    W.handle_gate3_reject(p, {}, "再来一遍")
    e = [x for x in p.lineage if x.get("action") == "gate3_route"][-1]
    assert e["reset_tasks"] == 1, f"已交付那栏不对: {e}"
    assert e["reset_failed"] == 1, f"失败那栏不对: {e}"


# ═══════════════════════════════════════════════════════════════
# §82.3 泄漏面：超时写的"停"标记必须先清掉
# ═══════════════════════════════════════════════════════════════

def test_超时标记要被清掉再重派(tmp_path, monkeypatch):
    """超时那条路是**成对**写的（写标记 → 转 FAILED）。标记留着的话，
    重派下去一上场就被 `_exec._check_cancelled` 判成"取消" ⇒ 白跑一轮。

    **掐法**：把 `_clear_cancel_marker_for_rewind` 的 `p.unlink()` 删掉 ⇒ 本条红。
    """
    p = _mk(tmp_path, monkeypatch, [("超时那个", TaskStatus.FAILED)])
    _marker(p.task_ids[0], json.dumps({"by": "timeout", "at": 1.0}))
    W.handle_gate3_reject(p, {}, "再来一遍")
    assert _status(p.task_ids[0]) is TaskStatus.PENDING
    assert not (config.CANCEL_DIR / f"{p.task_ids[0]}.json").exists(), \
        "标记还在 —— 重派后一上场就会被判成「取消」"


def test_人工取消的任务不重派(tmp_path, monkeypatch):
    """**"尊重别继续"是刻意的一半**：人明确取消过 ⇒ 返工不该把它又拉起来。

    判据口径直接沿用 §82.3 定的那条：**读不出 `by` 一律当用户取消**。
    """
    p = _mk(tmp_path, monkeypatch, [("人取消的", TaskStatus.FAILED)])
    _marker(p.task_ids[0], json.dumps({"task_id": p.task_ids[0], "cancelled_at": 1.0}))
    W.handle_gate3_reject(p, {}, "再来一遍")
    assert _status(p.task_ids[0]) is TaskStatus.FAILED, "人工取消的任务被返工拉起来了"
    assert (config.CANCEL_DIR / f"{p.task_ids[0]}.json").exists(), "标记不该被删"
    e = [x for x in p.lineage if x.get("action") == "gate3_route"][-1]
    assert e["held_back_user_cancelled"] == 1, f"没记账: {e}"


@pytest.mark.parametrize("body", ['{}', 'not json at all', '{"by": ""}'])
def test_读不出by的标记一律当人工取消(tmp_path, monkeypatch, body):
    """空 body / 坏 JSON / `by` 是空串 —— 三种都退回"用户取消"。

    ⚠️ `"{}"` 是**历史上真的写过**的形状（09-19 之前超时标记就是它，
    §82.3 的根因就是"空 body 里没有东西能区分超时和人工取消"）。
    """
    p = _mk(tmp_path, monkeypatch, [("x", TaskStatus.FAILED)])
    _marker(p.task_ids[0], body)
    W.handle_gate3_reject(p, {}, "再来一遍")
    assert _status(p.task_ids[0]) is TaskStatus.FAILED, f"body={body!r} 被当成超时了"


def test_标记读坏了要出声(tmp_path, monkeypatch):
    """§82.3 那句是「退回旧行为 = 用户取消，**且不静默**」。

    静默的代价：文件在那儿却读不出来是个异常，而它的后果是"这个任务**不被重派**"
    —— 没有声音的话，表现就是"返工之后那个活还是缺的"，没人知道为什么。

    **掐法**：删掉那个 `except` 里的 `witness.warn` ⇒ 本条红
    （`test_no_silent_except` 那条闸门也会红 —— 它就是这么发现的）。
    """
    p = _mk(tmp_path, monkeypatch, [("x", TaskStatus.FAILED)])
    _marker(p.task_ids[0], "not json at all")
    warns = []
    from singularity.scheduler import witness
    monkeypatch.setattr(witness, "warn", lambda *a, **k: warns.append(a))

    W.handle_gate3_reject(p, {}, "再来一遍")

    assert any("cancel_marker_unreadable" in str(a) for a in warns), \
        f"标记读坏了却没人知道: {warns}"


def test_旧格式没有by键的不报警(tmp_path, monkeypatch):
    """「没有 `by` 键」是**合法的旧格式**（`task_cancel` 写的那个），不该刷告警。

    报警就是**常亮的假红** —— 本仓为这个立过规矩（"稳定的配置事实不该每任务挂一句"）。
    ⚠️ 它跟"读坏了"是两件事：前者是数据正常、后者是文件出问题。
    两者都退回"用户取消"，但只有一个值得出声。
    """
    p = _mk(tmp_path, monkeypatch, [("人取消的", TaskStatus.FAILED)])
    _marker(p.task_ids[0], json.dumps({"task_id": p.task_ids[0], "cancelled_at": 1.0}))
    warns = []
    from singularity.scheduler import witness
    monkeypatch.setattr(witness, "warn", lambda *a, **k: warns.append(a))

    W.handle_gate3_reject(p, {}, "再来一遍")

    assert not any("cancel_marker" in str(a) for a in warns), \
        f"合法的旧格式被报成异常了（假红）: {warns}"


def test_删不掉标记就不重派(tmp_path, monkeypatch):
    """删不掉 ⇒ 重派也白搭（一上场照样被判取消）⇒ **退回"尊重取消"**，并出声。

    静默的话：钱花了、任务跑了、账上写着"取消"，没人知道为什么。
    """
    p = _mk(tmp_path, monkeypatch, [("超时那个", TaskStatus.FAILED)])
    _marker(p.task_ids[0], json.dumps({"by": "timeout"}))
    monkeypatch.setattr("pathlib.Path.unlink",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("锁住了")))
    warns = []
    from singularity.scheduler import witness
    monkeypatch.setattr(witness, "warn", lambda *a, **k: warns.append(a))

    W.handle_gate3_reject(p, {}, "再来一遍")

    assert _status(p.task_ids[0]) is TaskStatus.FAILED, "删不掉还硬要重派 ⇒ 白跑一轮"
    assert any("cancel_marker_unlink_failed" in str(a) for a in warns), \
        f"删不掉却没出声: {warns}"
