"""兜底升 GATE2 之前，必须先把验收层跑掉（2026-09-17 真机坐实）。

`_run_integration_merge_async` 里「合并成功 + 审查失败触顶」那一支原本直接
`set_phase(GATE2)`，而机器检查 + QA 验收**只挂在另一支**（`run_test_fix_loop`）
⇒ **兜底这条路把整个验收层跳过**。真机 round d 就是这样过的门：
`machine-checks.json` 压根没生成、QA 报告没有、`issues` 空着 ——
**人站到 GATE2 面前时手里没有任何证据**。

⚠️ 判据钉在「**验收真的被调用过**」上，不能只看 `phase`：只看 phase 的话，
把调用整段删掉照样绿 —— 那是"假接线"（本仓 2026-09-17 刚栽过一次）。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[{"rule": "正常路径"}], task_ids=[], issues=[],
        supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.INTEGRATING
    monkeypatch.setattr(proj_mod, "load", lambda _pid: p)
    monkeypatch.setattr(proj_mod, "save", lambda _proj: None)
    monkeypatch.setattr(orch, "_pending_sse_events", [])
    # 合并成功
    monkeypatch.setattr(orch, "_run_integration_merge", lambda _proj: (True, "合并通过"))
    # 审查失败触顶 ⇒ 走兜底
    from singularity.scheduler import _review
    monkeypatch.setattr(_review, "check_review_fail_limit",
                        lambda _pid, _n: {"blocked": True,
                                          "reason": "审查自动修已达上限(2轮), 升GATE2人工兜底"})
    return p


def test_兜底升GATE2之前要先跑验收层(tmp_path, monkeypatch):
    p = _mk(tmp_path, monkeypatch)

    from singularity.scheduler import workflow as wf
    calls = []

    def _fake_verify(proj, agents):
        calls.append(proj.id)
        return ["机械检查 0/7 条通过"]

    monkeypatch.setattr(wf, "_run_verification", _fake_verify)

    orch._run_integration_merge_async("proj1", {})

    assert p.phase == proj_mod.Phase.GATE2, "兜底没把人送到 GATE2 门前"
    assert calls == ["proj1"], (
        "兜底升 GATE2 之前**没跑验收层** ⇒ machine-checks.json 不会生成、"
        "人拿不到任何验收证据（真机 round d 就是这样过的门）")
    assert any(i.get("type") == "verification_before_fallback" for i in p.issues), \
        f"验收结论没落进 issues（界面上看不见）: {p.issues}"


def test_验收自己塌了不该连累兜底(tmp_path, monkeypatch):
    """拿不到证据也要把人送到门前 —— 但**必须留痕**，不能静默。"""
    p = _mk(tmp_path, monkeypatch)

    from singularity.scheduler import workflow as wf

    def _boom(proj, agents):
        raise RuntimeError("QA 挂了")

    monkeypatch.setattr(wf, "_run_verification", _boom)

    warns = []
    from singularity.scheduler import witness
    monkeypatch.setattr(witness, "warn", lambda scope, msg, **k: warns.append((scope, msg)))

    orch._run_integration_merge_async("proj1", {})

    assert p.phase == proj_mod.Phase.GATE2, "验收塌了就把兜底也带走了（人永远到不了门前）"
    assert warns, "验收塌了却一声不吭 —— 「没跑」和「跑了没事」长得一模一样"
    assert any("verification_before_fallback" in m for _s, m in warns), \
        f"留痕没带上关键词，查不到: {warns}"


# ═══════════════════════════════════════════════════════════════
# 触顶不再直接交人 —— 先给「回炉」一次机会（2026-09-20 用户拍板）
# ═══════════════════════════════════════════════════════════════
# `949ed80b`（最小闭环）的自动返工长在 `run_test_fix_loop` 里，而**触顶那一支不走它**
# ⇒ 自动返工**一次都没机会发生**。round c 真机坐实：7 个任务全失败 · 审查触顶 ·
# 项目躺在 GATE2 等人 —— 而那正是最该回炉的时候。
# ⚠️ 代价是真的（回炉 = 再跑一轮 = 再烧一截 token），所以五道闸门一个都不能少。

def test_触顶时先自动回炉一次再交人(tmp_path, monkeypatch):
    """允许 ⇒ 调 `handle_gate3_reject(auto=True)` 回实现层，**不再** `set_phase(GATE2)`。

    ⚠️ 判据钉在「**那个函数真的被调了、且 auto=True**」上 —— 只断言 phase 的话，
    把调用整段删掉、直接改成 `set_phase(EXECUTING)` 也照样绿（假接线）。
    """
    p = _mk(tmp_path, monkeypatch)
    from singularity.scheduler import workflow as wf
    monkeypatch.setattr(wf, "_run_verification", lambda _proj, _agents: [])
    monkeypatch.setattr(wf, "_resolve_fix_route", lambda _proj: ("impl", "qa_report", "", []))
    monkeypatch.setattr(wf, "_auto_rework_allowed", lambda _proj, _route: (True, "QA 判实现层不合格"))

    calls = []

    def _fake_reject(proj, agents, feedback="", auto=False):
        calls.append({"auto": auto, "feedback": feedback})
        proj.phase = proj_mod.Phase.EXECUTING      # 真函数就是这么做的
        return "GATE3 打回 → 回实现层修复"

    monkeypatch.setattr(wf, "handle_gate3_reject", _fake_reject)

    orch._run_integration_merge_async("proj1", {})

    assert calls == [{"auto": True, "feedback": "自动返工: 审查触顶，先回实现层"}], (
        f"触顶没回炉 —— 自动返工一次都没机会发生（round c 就是停在这儿）: {calls}")
    assert p.phase == proj_mod.Phase.EXECUTING, f"回炉后该回实现层，实际 {p.phase}"
    assert any(i.get("type") == "auto_rework_before_fallback" for i in p.issues), \
        f"回炉这件事没落进 issues（界面/账上看不见）: {p.issues}"


def test_不允许回炉时照旧交人_且把理由写出来(tmp_path, monkeypatch):
    """闸门拦下 ⇒ 回到原来的行为（GATE2），**并且说清为什么没回炉** ——
    "没回炉"和"没走到这一步"在界面上长得一模一样，是本仓反复吃亏的形状。"""
    p = _mk(tmp_path, monkeypatch)
    from singularity.scheduler import workflow as wf
    monkeypatch.setattr(wf, "_run_verification", lambda _proj, _agents: [])
    monkeypatch.setattr(wf, "_resolve_fix_route", lambda _proj: ("impl", "qa_report", "", []))
    monkeypatch.setattr(wf, "_auto_rework_allowed",
                        lambda _proj, _route: (False, "已经自动返过一次（防无限循环）"))
    called = []
    monkeypatch.setattr(wf, "handle_gate3_reject",
                        lambda *a, **k: called.append(k) or "不该被调到")

    orch._run_integration_merge_async("proj1", {})

    assert called == [], f"闸门说不允许，却还是回炉了: {called}"
    assert p.phase == proj_mod.Phase.GATE2, f"该照旧交人，实际 {p.phase}"
    assert any(i.get("type") == "no_auto_rework" for i in p.issues), \
        f"没说清为什么没回炉: {p.issues}"
