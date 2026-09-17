"""调研解析失败要**重试一次**（2026-09-17，一天撞了三次）。

规划那条路早就有这道（`_run_planning` 里的 `[格式错误]` 重试），**调研没有** ——
坏了就直接进 GATE1，用户看到的是一份解不开的报告，而调研比规划还贵。

真机实测两次的根因都是**输出被截断**（`raw_truncated=True`、末尾停在半句话），
所以重试提示词必须让它**写短一点把 JSON 补完整** —— 只管"请用 ```json 包起来"
治不了截断（它本来就是合法 JSON，只是没写完）。

⚠️ 判据钉在「**真的又调了一次 dispatch**」上：只断言"最后拿到了好报告"的话，
把重试换成"直接解析原文"照样绿（假接线）。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
# ⚠️ 必须先导入 workflow：它 `import *` 了 `_workflow_phases`，而后者反过来又 import 它
# —— 直接从测试里先碰 `_workflow_phases` 会撞上"半初始化模块"（循环导入）。
from singularity.scheduler import workflow  # noqa: F401


def _mk_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="proj1", name="探路", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    return p


def _stub(monkeypatch, raws: list):
    """按调用次序依次回 `raws`，并记下每次的 prompt。"""
    from singularity.scheduler import _workflow_phases as wp

    calls = []

    class _FakeER:
        def __init__(self, raw):
            self.raw_output = raw

    class _FakeDisp:
        def __init__(self, raw):
            self.executor_result = _FakeER(raw)
            self.agent_cfg = {"model": "fake-model"}

    def fake_safe_dispatch(prompt, level, task_id, agents, project, lineup=None,
                           restrict=False, phase="", no_tools=False):
        calls.append({"phase": phase, "task_id": task_id, "prompt": prompt})
        idx = min(len(calls) - 1, len(raws) - 1)
        return _FakeDisp(raws[idx]), ""

    monkeypatch.setattr(wp, "_safe_dispatch", fake_safe_dispatch)
    monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_phase_selection", lambda phase, project: (None, False))
    return wp, calls


GOOD = '{"competitive_analysis": {"products": [{"name": "A"}]}, "recommendation": "x"}'
# 截断的样子：JSON 没闭合、停在半句话上（真机上就是这个形状）
TRUNCATED = '{"competitive_analysis": {"products": [{"name": "angle-grinder", "pro'


def test_解析失败要重试一次_并用重试的结果(tmp_path, monkeypatch):
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [TRUNCATED, GOOD])

    wp._run_research(p, {})

    research_calls = [c for c in calls if c["phase"] == "researching"]
    assert len(research_calls) == 2, (
        f"解析失败没重试（只调了 {len(research_calls)} 次）⇒ 用户看到的是一份解不开的报告")
    assert not p.research_report.get("parse_error"), "重试拿到了好报告却没换上去"
    assert p.research_report["competitive_analysis"]["products"][0]["name"] == "A"


def test_重试提示词要针对截断_不是只说加代码块(tmp_path, monkeypatch):
    """截断的报告本来就是合法 JSON 的开头，只是没写完 —— 提示词得让它**写短**。"""
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [TRUNCATED, GOOD])

    wp._run_research(p, {})

    retry_prompt = [c for c in calls if c["phase"] == "researching"][1]["prompt"]
    assert "截断" in retry_prompt, f"重试提示词没提截断这件事：{retry_prompt[-120:]}"
    assert "短" in retry_prompt, f"没让它写短一点（那下次还会截断）：{retry_prompt[-120:]}"


def test_重试还是坏就退回第一次的_并留痕(tmp_path, monkeypatch):
    """重试也救不回来时，至少 raw 原文还在（界面能看原文），而且不能一声不吭。"""
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [TRUNCATED, "还是没闭合的 {"])

    wp._run_research(p, {})

    research_calls = [c for c in calls if c["phase"] == "researching"]
    assert len(research_calls) == 2, "重试没有发生"
    assert p.research_report.get("parse_error"), "重试也坏，却把坏的当好的收下了？"
    retry_marks = [e for e in p.lineage if e.get("action") == "research_parse_retry"]
    assert retry_marks and retry_marks[-1]["ok"] is False, \
        f"重试失败了却没留痕：{p.lineage}"
