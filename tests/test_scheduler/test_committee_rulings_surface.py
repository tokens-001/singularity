"""委员会裁决记录的两跳接线：**落盘** + **出口**（2026-09-27）。

`fuse_architecture_v2` 造出的 `rulings`（谁定稿、辩了几轮、哪些分歧判给谁、
哪些没裁出来）在 `_dispatch_exec` 里挂上了 `fusion_meta`，而
`_workflow_phases._run_planning` 重建 `project.committee_fusion` 时
**只挑了 models/count/fused/outputs 四个键，把它丢了** ——
于是 `execution_judge` 那句注释「落进 fusion_meta，GATE2 才查得到『凭什么长这样』」
一直是**假的**：盘上没有、接口里没有、界面上也没有。

⚠️ 同族的老坑：出参**有**测试（`test_rulings_out_param_records_the_debate`），
但那条只验了 `fuse_architecture_v2` 的出参，**没验它活过这一跳**。
这个文件补的正是那一跳。

第二跳是**出口**：未裁决的分歧要写进 `project.issues`
（`GatePanel` 的 `ProjectIssues` 读 `info.issues` 的 `it.detail`，**前端一行不用动**）。
用户 09-24 拍板「前端排最后」，所以这条刻意走现成通道。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
# ⚠️ 顺序不能动：`workflow` 末尾 `from _workflow_phases import *`，
# 先 import `_workflow_phases` 会拿到 partially-initialized 的模块（仓里既有的设计）。
from singularity.scheduler import workflow            # noqa: F401
from singularity.scheduler import _workflow_phases as wp

ARCH = '{"tasks": [{"id": "t1", "title": "x", "desc": "y"}], "constraints": []}'

RULINGS = {
    "writer": "m1",
    "rounds": 2,
    "resolved": [{"id": 1, "point": "金额怎么存", "winner": "m2", "basis": "conceded"}],
    "unresolved": [{"id": 2, "point": "拆不拆 billing", "winner": None,
                    "basis": "deadlock"}],
    "adopted": [],
    "rejected": [],
    "missing_votes": [],
}


def _mk_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="proj1", name="探路", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    proj_mod.save(p)   # repo_dir() 靠 load() 从盘上算路径，不存盘会走兜底分支
    return p


def _stub(monkeypatch, rulings):
    """把架构阶段那一步换成假的委员会结果（带 fusion_meta）。"""
    class _ER:
        raw_output = ARCH
        fusion_meta = {"models": ["m1", "m2"], "count": 2, "fused": "融合稿",
                       "outputs": ["A", "B"], "rulings": rulings}

    class _D:
        executor_result = _ER()
        agent_cfg = {"model": "fake-model"}

    monkeypatch.setattr(wp, "_safe_dispatch", lambda *a, **k: (_D(), ""))
    monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_phase_selection", lambda phase, project: (None, False))


# ── 第一跳：rulings 得活到盘上 ────────────────────────────────

def test_rulings_survives_the_persist_hop(tmp_path, monkeypatch):
    """落盘那一跳原来把 `rulings` 整个丢了。

    变异：删掉 `committee_fusion` 里 `"rulings": fm.get("rulings") or {},` 那行 → 红。
    """
    p = _mk_project(tmp_path, monkeypatch)
    _stub(monkeypatch, RULINGS)
    wp._run_planning(p, {})

    r = (p.committee_fusion or {}).get("rulings")
    assert r, "rulings 在落盘那一跳被丢了 —— 注释里『GATE2 查得到』那句就是假的"
    assert r["writer"] == "m1" and r["rounds"] == 2, r


def test_committee_fusion_still_keeps_the_old_fields(tmp_path, monkeypatch):
    """别为了补 rulings 把原来那四个键挤掉（前端「融合」页/后续阶段读它们）。"""
    p = _mk_project(tmp_path, monkeypatch)
    _stub(monkeypatch, RULINGS)
    wp._run_planning(p, {})
    for k in ("models", "count", "fused", "outputs"):
        assert k in p.committee_fusion, f"committee_fusion 少了 {k}"


# ── 第二跳：未裁决的分歧得有出口 ──────────────────────────────

def test_unresolved_disagreement_shows_up_as_a_project_issue(tmp_path, monkeypatch):
    """出口：未裁决的分歧必须落到 `project.issues` 上。

    "落盘"和"看得见"是两件事 —— 人在 GATE2 不会去翻项目 JSON。
    变异：删掉那段 `_unres` 追加 → 红。
    """
    p = _mk_project(tmp_path, monkeypatch)
    _stub(monkeypatch, RULINGS)
    wp._run_planning(p, {})

    hits = [i for i in p.issues if i.get("type") == "committee_unresolved"]
    assert len(hits) == 1, f"未裁决的分歧没有出口: {p.issues}"
    d = hits[0]["detail"]
    assert "拆不拆 billing" in d, f"出口里没说清是哪条: {d}"
    assert "僵持 1" in d, f"没把僵持和缺票分开: {d}"


def test_no_unresolved_no_issue(tmp_path, monkeypatch):
    """反方向对照：全裁出来了就不该有这条 issue —— 否则出口会变成常亮噪声。"""
    p = _mk_project(tmp_path, monkeypatch)
    _stub(monkeypatch, {**RULINGS, "unresolved": []})
    wp._run_planning(p, {})

    assert not [i for i in p.issues if i.get("type") == "committee_unresolved"], p.issues


def test_issue_is_replaced_not_accumulated(tmp_path, monkeypatch):
    """重规划时旧的要清掉 —— 否则打回一次多一条，出口很快没人看。

    变异：删掉那行 `project.issues = [i for i in project.issues if ...]` → 红。
    """
    p = _mk_project(tmp_path, monkeypatch)
    _stub(monkeypatch, RULINGS)
    wp._run_planning(p, {})
    assert [i for i in p.issues if i.get("type") == "committee_unresolved"]

    _stub(monkeypatch, {**RULINGS, "unresolved": []})
    wp._run_planning(p, {})

    assert not [i for i in p.issues if i.get("type") == "committee_unresolved"], \
        f"上一轮的出口没清掉: {p.issues}"


def test_no_committee_means_no_crash(tmp_path, monkeypatch):
    """单模型兜底那条路没有 `fusion_meta` —— 不许把架构阶段整个带崩。"""
    p = _mk_project(tmp_path, monkeypatch)

    class _ER:
        raw_output = ARCH
        fusion_meta = None

    class _D:
        executor_result = _ER()
        agent_cfg = {"model": "fake-model"}

    monkeypatch.setattr(wp, "_safe_dispatch", lambda *a, **k: (_D(), ""))
    monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_phase_selection", lambda phase, project: (None, False))

    wp._run_planning(p, {})
    assert p.committee_fusion is None
