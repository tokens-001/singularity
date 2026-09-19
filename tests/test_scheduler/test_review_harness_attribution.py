"""**审查器自己没答上来** ≠ 「被审的代码有 critical」（2026-09-19 复核判据错位审计 B1）。

`validator` 那边为了让"审查没产出"不静默，会合成一条 `severity: critical` 的 issue
（吐不出 JSON / 调用失败 / 没有审查员）。`_review` 原来把它和真发现合成一处，
记成「多模型审查**发现 N 处** critical」⇒ 查的人去翻被审的代码，而该看的是审查那条链路。

🔴 **这次只改"说法"，拦的强度一个字没动** —— 所以下面的断言里有两条都要成立：
  ① 仍然 `retry`（别把红藏起来）；② 不再声称"发现了 N 处 critical"。

变异验证（删哪一行会红）：
  · 去掉 `_review` 里按 `source == "review_harness"` 的拆分 → 第 1 条红；
  · 把 harness 那一堆从 `crit` 里剔出去（等于不再拦）→ 第 1 条的 retry 断言红；
  · `validator` 的合成 issue 不再带 `source` → 第 1 条红（会退化成"发现 1 处"）。
"""
from types import SimpleNamespace

import pytest

from singularity.scheduler import _review as rv
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import validator as val_mod


def _stub(monkeypatch, issues):
    """把 run_post_exec_checks 的多模型审查那一段换成固定的 issues。"""
    monkeypatch.setattr(rv, "_is_trivial_change", lambda *a, **k: False)
    monkeypatch.setattr(rv, "_pick_reviewers", lambda *a, **k: (["m1"], ["m1", "m2"]))
    monkeypatch.setattr(disp_mod, "load_agents", lambda: {})
    monkeypatch.setattr(disp_mod, "_all_agents_list", lambda _a: [])
    monkeypatch.setattr(val_mod, "run_project_tests",
                        lambda *a, **k: {"runner": "none", "passed": True, "total": 0})
    monkeypatch.setattr(val_mod, "multi_model_review",
                        lambda **k: {"issues": issues, "models_used": ["m1"],
                                     "verdicts": []})


def _run(tmp_path, monkeypatch, issues):
    _stub(monkeypatch, issues)
    validation = SimpleNamespace(action="pass", unverified=[])
    quality = {"warnings": [], "confidence": 0.5, "quality_signals": {},
               "failure_kind": "ok", "failure_reason": ""}
    rv.run_post_exec_checks(
        validation=validation, quality=quality, exec_result=None,
        task=SimpleNamespace(project_id="", description="t"),
        agent_cfg={"model": "w"}, level="any", cwd=str(tmp_path),
        changed=["a.py", "b.py"], base_ref="")
    return validation, quality


def test_审查器没答上来_不算发现critical_但仍然拦(tmp_path, monkeypatch):
    validation, quality = _run(tmp_path, monkeypatch, [{
        "severity": "critical", "line": 0, "source": "review_harness",
        "detail": "chunk review output not JSON: x", "model": "m1"}])

    assert validation.action == "retry", (
        "审查器没答上来**也必须拦** —— 把这堆从 crit 里剔掉就是把红藏起来")
    blob = " ".join(quality["warnings"] + validation.unverified)
    assert "0 critical" in blob, f"不该声称发现了 critical：{blob}"
    assert "审查器自己没答上来" in blob, blob


def test_真发现照旧报数(tmp_path, monkeypatch):
    """**对照**：审查员真发现了，那句"发现 N 处"一个字不能变。"""
    validation, quality = _run(tmp_path, monkeypatch, [{
        "severity": "critical", "line": 3,
        "detail": "除零没判", "model": "m1"}])

    assert validation.action == "retry"
    blob = " ".join(quality["warnings"] + validation.unverified)
    assert "1 critical" in blob, blob
    assert "审查器自己没答上来" not in blob, blob


def test_两堆混在一起时分别报数(tmp_path, monkeypatch):
    validation, quality = _run(tmp_path, monkeypatch, [
        {"severity": "critical", "line": 3, "detail": "除零没判", "model": "m1"},
        {"severity": "critical", "line": 0, "source": "review_harness",
         "detail": "chunk review failed: boom", "model": "m2"},
    ])

    assert validation.action == "retry"
    blob = " ".join(quality["warnings"] + validation.unverified)
    assert "1 critical" in blob and "审查器自己没答上来" in blob, blob


def test_validator合成的issue带出来源标记():
    """标记是 `_review` 分辨两堆的**唯一**依据 —— 没了这行，上面三条全退化成"发现 N 处"。"""
    import inspect
    src = inspect.getsource(val_mod)
    assert src.count('"source":"review_harness"') + src.count('"source": "review_harness"') >= 5, (
        "合成的 issue 少带了来源标记（单模型 3 处 + 多模型 2 处）")


def test_机械检查超时_不算通过也不算它的失败(tmp_path, monkeypatch):
    """B4：GATE3 那句「机械检查 x/y 通过」原来把**我们给的 60 秒不够**也算进了分母。

    🔴 **`passed` 不许变** —— 超时绝不能算通过（fail-closed）。这里同时钉两条：
       ① 超时那条仍然**不通过**；② 但它要跟"跑了没过"分开说。
    """
    from singularity.scheduler import config
    from singularity.scheduler import project as proj_mod
    from singularity.scheduler import workflow
    from singularity.scheduler import _machine_checks as mchk

    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _id: tmp_path)
    p = proj_mod.ProjectState(id="mk1", name="机械检查归因")
    p.architecture = {"constraints": [{
        "type": "security", "rule": "别硬编码密钥",
        "check": {"argv": ["python3", "-m", "pytest", "-q"], "expect_exit": 0}}]}
    proj_mod.save(p)

    monkeypatch.setattr(mchk, "run_check",
                        lambda *a, **k: {"ran": True, "passed": False, "exit": None,
                                         "stdout": "", "stderr": "", "reason": "超时 60.0s"})
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass  # 后面几段要真依赖；这里只关心机械检查那一段

    note = [i for i in p.issues if i.get("type") == "machine_checks"][-1]["detail"]
    assert "0/1 条通过" in note, f"超时被算成通过了 —— 门被放松了：{note}"
    assert "我们没跑完" in note, f"没跟我们自己的超时分开说：{note}"


def test_触顶文案说清不全是模型没改好():
    """B2：计数的构成里混着**审查侧自身**的超时/异常。计数没动，只是别让人查错方向。"""
    r = rv.check_review_fail_limit("p1", rv._REVIEW_MAX_AUTO_FIX)
    assert r["blocked"] is True
    assert "含审查侧自身" in r["reason"], r["reason"]
