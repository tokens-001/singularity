"""merge 终态要把**真因**带上（2026-09-19 复核判据错位审计 B5）。

`merge._drain_one` 有两条"合不成"的路：
  · 真冲突（探得到冲突文件名）→ `_park` → `conflict_held`，**等人解**；
  · **探不到冲突文件名** → 判"merge probe 命令错误 (ref 可能已过期)" → `status="failed"`。

第二条**不是任务没干好** —— 是我们的锚/ref 没了（超时 / GC / 提前释放）。
而 `orchestrator._drain_pending` 的 `else` 分支原来只写 `f"merge {mr.status}"`
⇒ 终态上一句光秃秃的 `merge failed`，`mr.reason` 被丢掉，排障的人只能去猜。

🔴 **只改说法，判定强度没动**：照样 `FAILED`（ref 没了 = 产物找不回来，
parking 等人也解不了）。所以下面既要钉"原因在"，也要钉"状态还是 FAILED"。

变异验证：把 `error=f"merge {mr.status}"` 改回去 → 红。
"""
from types import SimpleNamespace

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import tracker


def _run(monkeypatch, result):
    calls = []
    monkeypatch.setattr(tracker, "transition",
                        lambda tid, st, **kw: calls.append((tid, st, kw.get("error", ""))))

    class _Q:
        def drain(self):
            return [result]

    t = SimpleNamespace(id="t1", status=None, error="")
    batch = SimpleNamespace(dispatch_result=None, validation=None,
                            pre_search_skipped=None, pre_search_reason="",
                            pre_search_top_decisions=[], pre_search_memory=None,
                            tool_events=[])
    orch._drain_pending({"t1": (t, "route", None, batch)}, _Q(), [])
    return calls


def test_命令错误的真因要落进终态(monkeypatch):
    calls = _run(monkeypatch, SimpleNamespace(
        task_id="t1", status="failed", conflict_files=[], new_head="",
        reason="merge probe 命令错误 (ref 可能已过期: abc12345)"))

    assert calls, "没落终态 —— 任务会停在 RUNNING 没人管"
    _tid, st, err = calls[0]
    assert st == tracker.TaskStatus.FAILED, "终态本身是对的（ref 没了 = 产物找不回来），别改"
    assert "ref 可能已过期" in err, f"真因被丢了，只剩「merge failed」：{err!r}"


def test_没有原因时不写空(monkeypatch):
    """**对照**：`reason` 为空时别落一个空尾巴 —— 写"无原因"，别让人以为漏了。"""
    calls = _run(monkeypatch, SimpleNamespace(
        task_id="t1", status="failed", conflict_files=[], new_head="", reason=""))

    _tid, st, err = calls[0]
    assert st == tracker.TaskStatus.FAILED
    assert "无原因" in err and not err.endswith(": "), err
