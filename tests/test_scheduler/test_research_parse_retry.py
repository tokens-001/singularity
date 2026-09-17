"""调研解析失败要**重试一次**（2026-09-17，一天撞了三次）。

规划那条路早就有这道（`_run_planning` 里的 `[格式错误]` 重试），**调研没有** ——
坏了就直接进 GATE1，用户看到的是一份解不开的报告，而调研比规划还贵。

⚠️ **别把 `raw_truncated` 当"被截断"的证据**（2026-09-17 我犯过）：那只是 `_io.py`
兜底里"原文超过 5000 字"的**展示标记**。拉原文一看，JSON 结尾是完整闭合的
（`...]\n}\n```），真因是**字符串里写了没转义的双引号** ⇒ 重试提示词得说这个。

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
# **真机那个形状**（2026-09-17 原文第 307 行）：JSON 是**完整闭合**的，
# 但字符串里写了没转义的双引号 ⇒ 字符串提前闭合 ⇒ 解析器报 line/column。
# ⚠️ 别拿"没写完的 JSON"当测试输入 —— 那就把"被截断"这个错前提写进测试里了。
BAD_QUOTES = (
    '{"pitfalls": ["【管道崩溃】会打出难看的 traceback（"Exception ignored in..."）。'
    '需捕获并 dup2 到 devnull 后返回 0"]}'
)


def test_解析失败要重试一次_并用重试的结果(tmp_path, monkeypatch):
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [BAD_QUOTES, GOOD])

    wp._run_research(p, {})

    research_calls = [c for c in calls if c["phase"] == "researching"]
    assert len(research_calls) == 2, (
        f"解析失败没重试（只调了 {len(research_calls)} 次）⇒ 用户看到的是一份解不开的报告")
    assert not p.research_report.get("parse_error"), "重试拿到了好报告却没换上去"
    assert p.research_report["competitive_analysis"]["products"][0]["name"] == "A"


def test_重试提示词要说真原因_不是猜(tmp_path, monkeypatch):
    """提示词必须指向**真因**（字符串里没转义的双引号），并且把解析器报的那句带上。

    ⚠️ 反面教材就是我自己：第一版照 `raw_truncated` 猜成"被截断"，让它"写短一点"
    —— 治错了病。这条钉住"别猜，用解析器报的"。"""
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [BAD_QUOTES, GOOD])

    wp._run_research(p, {})

    retry_prompt = [c for c in calls if c["phase"] == "researching"][1]["prompt"]
    assert "转义" in retry_prompt, f"没告诉它真因（引号没转义）：{retry_prompt[-140:]}"
    assert "line" in retry_prompt, (
        f"没把解析器报的那句带上 —— 不带就只能靠猜：{retry_prompt[-140:]}")


def test_重试还是坏就退回第一次的_并留痕(tmp_path, monkeypatch):
    """重试也救不回来时，至少 raw 原文还在（界面能看原文），而且不能一声不吭。"""
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub(monkeypatch, [BAD_QUOTES, "{\"pitfalls\": [\"还有没转义的 \"引号\"]}"])

    wp._run_research(p, {})

    research_calls = [c for c in calls if c["phase"] == "researching"]
    assert len(research_calls) == 2, "重试没有发生"
    assert p.research_report.get("parse_error"), "重试也坏，却把坏的当好的收下了？"
    retry_marks = [e for e in p.lineage if e.get("action") == "research_parse_retry"]
    assert retry_marks and retry_marks[-1]["ok"] is False, \
        f"重试失败了却没留痕：{p.lineage}"
