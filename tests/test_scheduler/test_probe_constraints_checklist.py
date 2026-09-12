"""§60 探针的自检 —— **探针本身也是代码，也会坏成"永远不报"**。

被探的东西：`project.constraints_checklist` 在 `_run_execution` 里赋了值，
走到 `_run_verification` 却是空的 ⇒ 机械检查一条都跑不了。

探针分两头打，因为**只在写入点打证明不了"被覆盖"**：
  · 写入点（`_run_execution`）记 in_memory 和**存盘后读回来**的 on_disk
  · 读取点（`_run_verification`）记它当时看到的是什么
两头对不上（on_disk 有值、读取点 0）⇒ 中间有人拿旧副本 save 覆盖了。

⚠️ 定案后探针和这个文件一起删。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow
from singularity.scheduler import _workflow_phases as wp


def _mk_project(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="probe1", name="探针", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={}, **kw,
    )
    proj_mod.save(p)
    return p


def _probes(p):
    return [e for e in p.lineage if e.get("action") == "probe_constraints_checklist"]


def test_write_probe_fires_and_round_trips(tmp_path, monkeypatch):
    """写入点：赋值 → 存盘 → 读回来，两个数都得是 8。"""
    p = _mk_project(tmp_path, monkeypatch)
    p.architecture = {
        "constraints": [{"type": "security", "rule": f"r{i}",
                         "check": {"argv": ["pytest"], "expect_exit": 0},
                         "covers": [i]} for i in range(8)],
        "tasks": [{"id": "T1", "title": "t", "description": "d", "layer": "backend"}],
    }
    wp._run_execution(p, agents={})

    w = [e for e in _probes(p) if e["at"] == "write"]
    assert len(w) == 1, f"写入点探针没打：{[e.get('action') for e in p.lineage]}"
    assert w[0]["in_memory"] == 8
    assert w[0]["same_obj"], "赋进去的跟架构里那份不是同一个列表 —— 后面对它的改动不回流"
    assert w[0]["on_disk"] == 8, "赋值没落盘 —— 那就是'没赋上'那一支，不是被覆盖"
    assert len(p.constraints_checklist) == 8, "内存对象自己也得有"


def test_read_probe_fires_only_when_constraints_are_really_absent(tmp_path, monkeypatch):
    """读取点：**架构里也真没有**约束时才算"验收真的没得跑"。

    ⚠️ 这条的语义 2026-09-12 变了 —— 加了 `effective_constraints()` 兜底之后，
    "清单为空"**不再等于**"被覆盖"：
      · 清单空 + 架构里有 → 兜底捞走，**不**打探针（改由
        `constraints_checklist_fallback` 那条告警来报"覆盖源还没找到"）
      · 清单空 + 架构里也空 → 真没有，打探针，并走 `verification_skipped`
    所以这里必须把架构也弄成空的，否则测的是兜底、不是探针。
    """
    p = _mk_project(tmp_path, monkeypatch)
    p.architecture = {"constraints": []}    # 架构里也真没有
    p.constraints_checklist = []
    workflow._run_verification(p, agents={})

    r = [e for e in _probes(p) if e["at"] == "read"]
    assert len(r) == 1, "读取点探针没打"
    assert r[0]["in_memory"] == 0
    assert r[0]["arch_constraints"] == 0, "这里应该记到「架构里也是 0」"


def test_read_probe_stays_quiet_when_fallback_can_rescue(tmp_path, monkeypatch):
    """反向：清单空但架构里有 → 兜底救走，探针**不该**打（那是两回事）。"""
    p = _mk_project(tmp_path, monkeypatch)
    p.architecture = {"constraints": [{"rule": f"r{i}"} for i in range(8)]}
    p.constraints_checklist = []            # 模拟被覆盖之后的状态
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass                                # 真往下走会缺依赖，这里只看探针落没落
    assert [e for e in _probes(p) if e["at"] == "read"] == [], \
        "兜底能救的情况不该打读取点探针 —— 否则跟「真没有」混成一团"


def test_read_probe_stays_quiet_when_checklist_has_content(tmp_path, monkeypatch):
    """反向：清单非空时不该打 —— 否则探针本身成了噪声，跟 §62 那族告警一样。"""
    p = _mk_project(tmp_path, monkeypatch)
    p.architecture = {"constraints": [{"rule": "r"}]}
    p.constraints_checklist = [{"rule": "r", "check": {"argv": ["pytest"], "expect_exit": 0}}]
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass                                # 走真验收分支会缺依赖，这里只关心探针不落
    assert _probes(p) == []
