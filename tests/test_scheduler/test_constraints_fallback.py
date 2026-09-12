"""§60 的**症状修复**：约束清单被覆盖时，验收不能因此整条早退。

背景：`_run_execution` 里 `constraints_checklist = architecture["constraints"]`
紧跟着 `save()`，可真机上落到盘里的却是 `[]` —— 而**同一份 json 里
`architecture.constraints` 完好有 8 条**。覆盖源**没定位到**（全仓只有那一处写它，
`architecture_redo()` 是死代码），读代码定不了案。

所以这里不赌是谁覆盖的，改成让症状不可能发生：`ProjectState.effective_constraints()`
在清单为空、而架构里有约束时**兜底用架构那份**，并且**出声告警**。

⚠️ 告警是刻意留的，别当成噪声清掉 —— 没有它，覆盖源就永远查不出来了。
"""
from singularity.scheduler import project as proj_mod
from singularity.scheduler import witness
from singularity.scheduler import workflow


def _mk(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(proj_mod.config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="c1", name="约束兜底", raw_constraints=[], owner_confirm={},
        task_ids=[], issues=[], supervision_log=[], lineage=[],
        handoffs=[], agent_lineup={}, **kw)
    proj_mod.save(p)
    return p


_CONSTRAINTS = [{"type": "security", "rule": f"r{i}",
                 "check": {"argv": ["pytest"], "expect_exit": 0}, "covers": [i]}
                for i in range(8)]


def test_uses_the_field_when_it_is_populated(tmp_path, monkeypatch):
    """正常情况：清单里有东西就用它，**不许**去兜底（否则会绕过 Gate2 确认）。"""
    p = _mk(tmp_path, monkeypatch)
    p.architecture = {"constraints": _CONSTRAINTS}
    p.constraints_checklist = _CONSTRAINTS[:2]
    assert len(p.effective_constraints()) == 2


def test_falls_back_to_architecture_and_warns(tmp_path, monkeypatch):
    """正题：清单被覆盖成空 —— 用架构里那份，并且**出声**。"""
    seen = []
    monkeypatch.setattr(witness, "warn", lambda *a, **k: seen.append(a))
    p = _mk(tmp_path, monkeypatch)
    p.architecture = {"constraints": _CONSTRAINTS}
    p.constraints_checklist = []                      # 模拟被覆盖之后的状态

    got = p.effective_constraints()
    assert len(got) == 8, "兜底没生效 —— 验收又会早退"
    assert any("constraints_checklist_fallback" in str(a) for a in seen), \
        f"兜底了却没告警，覆盖源就永远查不出来：{seen}"


def test_both_empty_returns_empty_without_warning(tmp_path, monkeypatch):
    """架构里也真没有 → 返回空、**不告警**（告警只留给"该有却没有"那种）。"""
    seen = []
    monkeypatch.setattr(witness, "warn", lambda *a, **k: seen.append(a))
    p = _mk(tmp_path, monkeypatch)
    p.architecture = {}
    p.constraints_checklist = []
    assert p.effective_constraints() == []
    assert seen == [], seen


def test_no_architecture_at_all_is_safe(tmp_path, monkeypatch):
    """架构都还没有（比如模板阶段）→ 空，不许崩。"""
    p = _mk(tmp_path, monkeypatch)
    p.architecture = None
    assert p.effective_constraints() == []


def test_verification_does_not_skip_when_checklist_was_clobbered(tmp_path, monkeypatch):
    """**这条才是 §60 的正题**：清单被覆盖时，`_run_verification` 不许早退。

    早退的后果不是"少跑一步" —— 它会 append 一条 `verification_skipped`，
    **机械检查一条都跑不了**，"信任上限 = 机械证据覆盖的验证面比例"分子恒 0。
    """
    p = _mk(tmp_path, monkeypatch)
    p.architecture = {"constraints": _CONSTRAINTS}
    p.constraints_checklist = []
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass        # 真往下走会缺依赖（这里只关心"有没有早退"，不关心跑完）

    assert not [i for i in p.issues if i.get("type") == "verification_skipped"], \
        "还是早退了 —— 机械检查这条路仍然是断的"
