"""需求覆盖率那根尺子的三条接线 —— 2026-09-23。

该修的到底是什么（读代码核出来的，不是猜的）：
架构师被要求给 `scope_clarification.core` 的**索引**，而喂给它的调研报告是
`research_md[:5000]`（截断）。实测 5 个项目里 **4 个**的 `"scope_clarification"`
落在 5000 字符**之外**（5349 / 7262 / 7378 / 7462），全仓 grep 也确认**没有任何地方
把这份清单拼进过任何提示词** ⇒ **它从没见过自己要照填的那份清单**。
09-22 那轮只好填眼前的用户原话 ⇒ 覆盖率报 `0/10`（假红）；b 轮则填了一堆
**不存在的索引**（core 6 条，covers 里出现 6/7/8/9），越界的被 `_idx()` 静默丢掉，
剩下的恰好把每栏点亮 ⇒ 报表上「覆盖率 100%」（假绿）。

⚠️ **两条命门必须同时绿**：
  · `test_清单要进提示词_哪怕调研报告被截断` —— 删掉 `research_context +=` 那两行 ⇒ 红；
  · `test_越界索引不能静默丢` —— 只在 `uncovered` 非空时报警 ⇒ 这条红（b 轮那形状
    `uncovered` 是空的，报警永远轮不到）。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow  # noqa: F401  ← 先导它，绕开循环导入
from singularity.scheduler import _workflow_phases as wp

CORE = ["reader：以 UTF-8 打开文件，逐行产出 str",
        "counter：按行统计出现次数并排序",
        "cli：argparse 入口，--top N"]


def _mk_project(tmp_path, monkeypatch, research_report):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="proj1", name="探路", description="做个按行统计的小工具",
        raw_constraints=[], owner_confirm={}, constraints_checklist=[], task_ids=[],
        issues=[], supervision_log=[], lineage=[], handoffs=[], agent_lineup={},
        research_report=research_report,
    )
    proj_mod.save(p)
    return p


def _stub_pipeline(monkeypatch, raw: str):
    """换掉 `_workflow_phases` 的 dispatch：只记 prompt、回一段假架构。"""
    calls = []

    class _FakeDisp:
        def __init__(self):
            self.executor_result = type("ER", (), {"raw_output": raw})()
            self.agent_cfg = {"model": "fake-model"}

    def fake_safe_dispatch(prompt, level, task_id, agents, project, lineup=None,
                           restrict=False, phase="", no_tools=False):
        calls.append({"prompt": prompt})
        return _FakeDisp(), ""

    monkeypatch.setattr(wp, "_safe_dispatch", fake_safe_dispatch)
    monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_phase_selection", lambda phase, project: (None, False))
    return calls


def _write_research_md(tmp_path, core_at: int):
    """在盘上造一份 `research.md`：**core 落在第 core_at 个字符之后**（模拟真机那份）。"""
    md = '{"filler": "' + "x" * core_at + '", "scope_clarification": {"core": %s}}' \
        % json.dumps(CORE, ensure_ascii=False)
    d = tmp_path / "qidian" / "projects"
    d.mkdir(parents=True, exist_ok=True)
    (d / "proj1.research.md").write_text(md, encoding="utf-8")
    return md


def _arch(constraints):
    return json.dumps({"tasks": [{"id": "T1", "title": "读模块", "description": "y"}],
                       "constraints": constraints}, ensure_ascii=False)


# ── 命门一：清单必须进提示词 ─────────────────────────────────────────

def test_清单要进提示词_哪怕调研报告被截断(tmp_path, monkeypatch):
    """🔴 **这条是本次修复的核心判据。**

    喂进去的 research.md 里 `scope_clarification` 在第 6000 字符处（真机 4/5 轮的形状），
    而截断是 5000 ⇒ 光靠截断永远带不进去。判据：**删掉 `research_context +=` 那两行 ⇒ 红。**
    """
    md = _write_research_md(tmp_path, core_at=6000)
    assert md.index("scope_clarification") > 5000, "夹具没造出'截断之外'那个形状"
    p = _mk_project(tmp_path, monkeypatch, {"scope_clarification": {"core": CORE}})
    calls = _stub_pipeline(monkeypatch, _arch([]))

    wp._run_planning(p, {})

    prompt = calls[0]["prompt"]
    assert "【需求清单" in prompt, "清单没进提示词 —— 架构师又被要求照一份它看不见的表填 covers"
    assert f"[0] {CORE[0]}" in prompt, "编号没带进去，架构师没法用索引回答"
    assert f"[{len(CORE) - 1}] {CORE[-1]}" in prompt


def test_没有清单时不硬凑(tmp_path, monkeypatch):
    """调研报告里没有 core ⇒ 拼一个空块进去只会误导架构师。"""
    p = _mk_project(tmp_path, monkeypatch, {"scope_clarification": {}})
    calls = _stub_pipeline(monkeypatch, _arch([]))

    wp._run_planning(p, {})

    assert "【需求清单" not in calls[0]["prompt"]


# ── 命门二：两个静默都要出声 ─────────────────────────────────────────

def _issue_types(p):
    return [i.get("type") for i in p.issues]


def test_越界索引不能静默丢(tmp_path, monkeypatch):
    """🔴 **b 轮那形状**：core 2 条、约束把 0/1 都点亮了，同时填了个不存在的 99。

    `uncovered` 是**空的** ⇒ 只在 uncovered 非空时报警，这条就永远抓不到 ——
    而它正是"伪造覆盖率"最省事的那条路（填一堆不存在的索引，越界的被丢掉，
    剩下的恰好把每栏点亮 ⇒ 报表上 100%）。
    """
    p = _mk_project(tmp_path, monkeypatch,
                    {"scope_clarification": {"core": CORE[:2]}})
    _stub_pipeline(monkeypatch, _arch([
        {"rule": "a", "check": "散文", "covers": [0, 1, 99]},
    ]))

    wp._run_planning(p, {})

    cov = [l for l in p.lineage if l.get("action") == "requirement_coverage"][-1]
    assert cov["covered"] == 2 and cov["unmatched"] == 1, cov
    assert "requirement_covers_unmatched" in _issue_types(p), \
        f"越界索引被静默丢了，报表上只看到'覆盖率 100%': {p.issues}"
    assert "requirement_uncovered" not in _issue_types(p), "这条不是缺口，别混成一种"


def test_对不上的原文算没覆盖(tmp_path, monkeypatch):
    """09-22 那形状：架构师填的是眼前的用户原话，不等于清单上的原文 ⇒ 一条都没命中。"""
    p = _mk_project(tmp_path, monkeypatch, {"scope_clarification": {"core": CORE}})
    _stub_pipeline(monkeypatch, _arch([
        {"rule": "a", "check": {"argv": ["python3", "-m", "pytest", "-q"], "expect_exit": 0},
         "covers": ["读一个 UTF-8 文本文件"]},
    ]))

    wp._run_planning(p, {})

    assert "requirement_uncovered" in _issue_types(p), p.issues
    detail = [i["detail"] for i in p.issues
              if i.get("type") == "requirement_uncovered"][0]
    assert "3/3" in detail, detail


def test_拿不到清单要出声_不是零缺口(tmp_path, monkeypatch):
    """`total=0` 原来被 `if _rc["total"] and ...` 直接吞掉 ⇒ 没量显示成"没问题"。"""
    p = _mk_project(tmp_path, monkeypatch, {"recommendation": "有个小工具挺合适"})
    _stub_pipeline(monkeypatch, _arch([
        {"rule": "a", "check": "散文", "covers": [0]},
    ]))

    wp._run_planning(p, {})

    assert "requirement_list_missing" in _issue_types(p), p.issues
    assert "requirement_uncovered" not in _issue_types(p), "没量 ≠ 量出来有缺口"


def test_重规划会把上一轮的这几条清掉(tmp_path, monkeypatch):
    """不清 ⇒ 上一轮判的词条会挂在界面上，和新的一版混在一起。"""
    p = _mk_project(tmp_path, monkeypatch, {"scope_clarification": {"core": CORE}})
    p.issues = [{"type": "requirement_uncovered", "detail": "上一轮的"},
                {"type": "requirement_list_missing", "detail": "上一轮的"},
                {"type": "requirement_covers_unmatched", "detail": "上一轮的"}]
    _stub_pipeline(monkeypatch, _arch([
        {"rule": "a", "check": "散文", "covers": [0, 1, 2]},
    ]))

    wp._run_planning(p, {})

    assert [i for i in p.issues if str(i.get("type")).startswith("requirement_")] == []
