"""GATE3 打回退到哪一层，由 `fix_route` 定 —— **不能由 severity 猜**（2026-09-15 真机）。

现场（项目 `1789481895784`）：QA 判 `no_go`，原文写着「**实现已全部正确，只差补齐
测试文件**」，而它把 8 条 issues 全标了 `severity: "critical"`（忠实 —— 验收确实没过）。
下游却把 critical 读成"架构级缺陷" ⇒ 人工点「打回」时走 design 分支 ⇒
`set_phase(PLANNING)` + **`project.architecture = None`（清空架构、从头重新规划）**。

`severity` 回答的是"这条验收过没过"，`fix_route` 回答的才是"要退到哪一层"。
"""
import json

import pytest

from singularity.scheduler import config, tracker, validator, workflow
from singularity.scheduler.project import Phase, ProjectState


# ── 判据本身 ──────────────────────────────────────────────

def test_只有critical没有fix_route时不判成架构级():
    """**这条钉的就是真机那个形状** —— 去掉它，那个 bug 会原样回来。"""
    issues = [{"id": "maintainability_test_coverage", "severity": "critical",
               "detail": "test_fizzbuzz.py 为完全空文件（0 行、0 字节）"}]
    assert validator.grade_fix_route(issues, "no_go") == "impl", \
        "补成 design ⇒ 人工点「打回」会清空架构重新规划，代价最大"


def test_显式写了design才算架构级():
    assert validator.grade_fix_route(
        [{"severity": "critical", "fix_route": "design"}], "no_go") == "design"


def test_build_qa_report给critical补的默认路由是impl():
    """补默认值这一步才是真机上真正写进 `qa_report.json` 的东西。"""
    rep = validator.build_qa_report(
        passed=[], verdict="no_go", verdict_reason="实现全对，只差补测试文件",
        issues=[{"id": "x", "severity": "critical", "detail": "缺测试文件"}])
    assert rep["issues"][0]["fix_route"] == "impl"


def test_提示词必须给出fix_route字段否则这条路由是死路():
    """**静态钉子**（同 `test_no_undefined_names` 那种）。

    `design` 这一档只有"有人显式写出来"才会被选中。提示词不给这个字段，
    模型就永远写不出它 ⇒ 架构重规划那条路**谁都走不到**（静默变成死路）。
    """
    from pathlib import Path
    src = Path(workflow.__file__).read_text(encoding="utf-8")
    assert '"fix_route": "impl|design|note"' in src, \
        "QA 提示词里的 issues schema 必须带 fix_route，否则读的那一端没有依据"


# ── 接线：人工点「打回」时真的按它走 ────────────────────────

@pytest.fixture
def tmp_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(workflow, "_projects_dir", lambda: tmp_path / "projects")
    (tmp_path / "projects").mkdir(exist_ok=True)
    monkeypatch.setattr(workflow, "_read_observer_rollup", lambda pid: None)
    monkeypatch.setattr(tracker, "transition", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "read_task", lambda tid: None)
    return tmp_path / "projects"


def _write_qa_report(projects_dir, pid: str, issue: dict):
    """**走真实链路写报告** —— 不是手搓一份。

    真机上 `qa_report.json` 是 `build_qa_report` 从模型原话生成的，路由也是在
    那一步补出来的。手搓一份就等于把被测的那一环跳过去了（只测"读"，没测"填"）。
    """
    rep = validator.build_qa_report(passed=[], issues=[issue], verdict="no_go",
                                    verdict_reason="实现全对，只差补测试文件")
    (projects_dir / f"{pid}.qa_report.json").write_text(
        json.dumps(rep, ensure_ascii=False), encoding="utf-8")


def test_打回时critical_only的项目回实现层_架构留着(tmp_projects):
    """端到端：这正是"点打回会不会把架构清掉"的答案。

    issue 就用真机那个形状 —— QA 只说 critical，**没给 fix_route**（它写不出来，
    提示词里当时没这个字段）。
    """
    p = ProjectState(id="p1", name="n", phase=Phase.GATE3)
    p.architecture = {"modules": ["原架构"]}
    _write_qa_report(tmp_projects, "p1",
                     {"id": "maintainability_test_coverage", "severity": "critical",
                      "detail": "test_fizzbuzz.py 为完全空文件（0 行、0 字节）"})

    workflow.handle_gate3_reject(p, agents={}, feedback="测试文件是空的")

    assert p.phase == Phase.EXECUTING, f"应回实现层，实际 {p.phase}"
    assert p.architecture is not None, "回实现层**不该**动架构"


def test_打回时显式design才回规划并清架构(tmp_projects):
    """对照组：真的架构级缺陷时，原行为不变（别把这条一起改没了）。"""
    p = ProjectState(id="p1", name="n", phase=Phase.GATE3)
    p.architecture = {"modules": ["原架构"]}
    (tmp_projects / "p1.qa_report.json").write_text(json.dumps({
        "summary": {"verdict": "no_go"},
        "issues": [{"id": "x", "severity": "critical", "fix_route": "design"}],
    }), encoding="utf-8")

    workflow.handle_gate3_reject(p, agents={}, feedback="架构本身不对")

    assert p.phase == Phase.PLANNING
    assert p.architecture is None, "design 分支的既定行为：清空架构重新规划"
