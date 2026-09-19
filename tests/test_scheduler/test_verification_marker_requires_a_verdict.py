"""验收门槛标记：**"QA 报告落盘了" ≠ "QA 产出了结论"**（2026-09-19 修外派评审 ③）。

那道"坏刹车"原样是：
    qa_saved = True      # 只要 build_qa_report 没抛
    if qa_saved: project.issues.append({"type": "verification_ran",
                                        "detail": "QA + 安全审计已执行"})

而 `build_qa_report([], [], verdict, reason)` **空输入照样不抛** ⇒
**QA 那一维什么都没产出时，标记照样撒、detail 还写着"已执行"**。
⇒ 越是"QA 报错/没产出"那轮（也正是最该被拦住的那轮），门槛标记越会开火 ——
而 `_gate3_admission` 一看有标记就放行。

现在要求 `verdict != 未产出`；没结论就不算"验收跑过"，让 admission 去报缺证据。
⚠️ `qa_verdict_missing` 那条 issue 由 `_flag_missing_qa_verdict` 照旧单独记，**两条都该在**。
"""

import pytest

from singularity.scheduler import workflow as wf
from singularity.scheduler import witness
from singularity.scheduler.project import ProjectState


@pytest.fixture
def _warns(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(witness, "warn", lambda *a, **k: got.append((a, k)))
    return got


def _project() -> ProjectState:
    p = ProjectState(id="p1", name="探针")
    return p


def _types(p) -> list[str]:
    return [i.get("type") for i in p.issues]


def test_有结论才撒_verification_ran(_warns):
    p = _project()
    wf._record_verification_marker(p, qa_saved=True, qa_verdict="go")
    assert "verification_ran" in _types(p)


def test_没结论时不撒_要走_缺证据(_warns):
    """🔴 这条就是那道刹车 —— 原来这里是绿的（标记照样撒）。"""
    p = _project()
    wf._record_verification_marker(p, qa_saved=True, qa_verdict=wf._QA_VERDICT_MISSING)
    assert "verification_ran" not in _types(p), \
        "QA 没产出结论却撒了『QA + 安全审计已执行』—— 坏刹车又回来了"
    # 不撒就得有人知道：留一条出声
    assert any("verification_ran_withheld" in str(a) for a, _ in _warns), _warns


def test_报告压根没落盘时不撒(_warns):
    p = _project()
    wf._record_verification_marker(p, qa_saved=False, qa_verdict="go")
    assert "verification_ran" not in _types(p)


def test_没撒标记时_admission_就会报缺证据(_warns):
    """接线：**不撒标记**这件事要真的让 GATE3 的准入看见 —— 否则等于没改。"""
    p = _project()
    wf._record_verification_marker(p, qa_saved=True, qa_verdict=wf._QA_VERDICT_MISSING)
    assert not p.has_verification_evidence(), "没撒标记却仍被认为『有验收记录』"
    p._gate3_admission()
    assert "gate3_no_evidence" in _types(p), p.issues


def test_no_go_也算有结论(_warns):
    """边界：`no_go` 是**结论**（不过是坏结论）—— 别把它和"没产出"混为一谈。"""
    p = _project()
    wf._record_verification_marker(p, qa_saved=True, qa_verdict="no_go")
    assert "verification_ran" in _types(p)


def test_初值是_未产出_不是_go(tmp_path, monkeypatch):
    """边界：QA 那段 try 在赋值前就抛时，标记必须走安全的一侧。

    `qa_verdict` 的初值取 `_QA_VERDICT_MISSING` ⇒ 拿不准就别声称验收跑过。
    """
    p = _project()
    wf._record_verification_marker(p, qa_saved=True, qa_verdict=wf._QA_VERDICT_MISSING)
    assert "verification_ran" not in _types(p)
