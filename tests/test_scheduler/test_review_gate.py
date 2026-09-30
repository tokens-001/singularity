"""执行后审查门禁的决策语义（`_review.run_post_exec_checks`）。

这条门禁决定任务是重试还是放行合并，但实测覆盖率只有 4% —— 桩掉各步骤的
LLM/子进程调用后可以把决策逻辑独立测出来。

要锁的两类语义：
  1. **fail-closed**：任何一步异常都不能当"没问题"放行（09-08 修过一整轮）
  2. **软信号不硬拦**：需求符合性/加固建议只写信号，不触发 retry
"""
from types import SimpleNamespace

import pytest

from singularity.scheduler import _review as rv


# `_run` 会把这一轮发出去的告警收进来（桩掉了落盘，只留内容给用例断言）。
WARNS: list = []


def _run(monkeypatch, tmp_path, *, project_id="", changed=("a.py", "b.py"),
         pool=(), scan=None, tests=None, multi=None, crossover=None,
         qa=None, conformance=None, audit=None, constraints=None,
         trivial=False, tests_fn=None, confidence=0.5, baseline=None):
    """跑一次门禁，返回 (validation, quality)。默认全绿，各测试只覆盖关心的那一段。

    pool=() → 走单模型 crossover fallback；给 2 个模型才走多人审查。
    project_id="" → 跳过 QA 约束 / 需求符合性 / 审查失败计数（不碰磁盘）。
    baseline=... → 基线那一跑的返回；**默认 None = 这个桩世界里没有基线**
    （见下面那条注释，别让"当前这一跑"的桩自己兼任自己的基线）。
    """
    from singularity.scheduler import validator as val, dispatcher as disp, project as proj_mod

    WARNS.clear()
    monkeypatch.setattr(rv.witness, "warn", lambda *a, **k: WARNS.append((a, k)))
    # 🔴 **必须单独桩掉基线那一跑**（2026-09-29）：`run_project_tests_at_ref` 内部调的
    # 就是 `run_project_tests`，而上面那行把后者换成了固定返回同一个 dict 的假货 ⇒
    # **"开工前"和"开工后"拿到一模一样的结果** ⇒ 差分判成"红的都是基线就有的" ⇒
    # 真失败被静默放行。桩世界里没有真的历史版本，所以默认 `None`（= 算不出差分，
    # fail-closed 照旧判红）。要测差分的用例直接给 `baseline=`。
    monkeypatch.setattr(val, "run_project_tests_at_ref",
                        lambda *a, **k: baseline if baseline is not None else None)
    monkeypatch.setattr(rv, "_is_trivial_change", lambda *a, **k: trivial)
    monkeypatch.setattr(val, "security_review", lambda *a, **k: scan or {"issues": []})
    # tests_fn 优先于 tests：要在 _run 之外定制行为（比如让它卡住）时用
    monkeypatch.setattr(val, "run_project_tests", tests_fn or (
        lambda *a, **k: tests or {"passed": True, "runner": "pytest", "total": 1}))
    monkeypatch.setattr(val, "security_audit_review",
                        lambda *a, **k: audit or {"verdict": "clean", "findings": []})
    monkeypatch.setattr(disp, "load_agents", lambda: {})
    monkeypatch.setattr(disp, "_all_agents_list", lambda *_a: list(pool))
    monkeypatch.setattr(disp, "agent_api_available", lambda *a, **k: True)
    monkeypatch.setattr(val, "multi_model_review",
                        lambda *a, **k: multi or {"models_used": ["r1", "r2"], "issues": []})
    monkeypatch.setattr(val, "crossover_review",
                        lambda *a, **k: crossover or {"issues": [], "verdict": "pass",
                                                      "summary": ""})
    monkeypatch.setattr(val, "qa_acceptance_review",
                        lambda *a, **k: qa or {"verdict": "pass", "verifications": []})
    # 桩项目要带齐 _review_requirements 会读的字段，缺一个就在第 2 步抛 AttributeError，
    # 被那圈的 except 吞成 review_error —— 看起来像"审查炸了"，其实是桩不全。
    monkeypatch.setattr(proj_mod, "load",
                        lambda _pid: SimpleNamespace(
                            constraints_checklist=constraints or [],
                            description="d", architecture={}, review_failures=0))
    monkeypatch.setattr(proj_mod, "save", lambda *a, **k: None)
    import singularity.scheduler.supervisor as sup
    monkeypatch.setattr(sup, "check_requirement_conformance",
                        lambda *a, **k: conformance or SimpleNamespace(passed=True, evidence={}))

    # 必须造**真** git 仓库：门禁取 diff 走的是 git，非 git 目录下 diff 恒空，
    # 会命中"审查看到的 diff 为空"的披露（那是真实行为，但会让"全绿"用例失真）。
    # 先提交一版、再改内容且**不暂存** → `git diff <file>` 才有东西可看。
    import subprocess as _sp
    def _g(*a):
        return _sp.run(["git", *a], cwd=str(tmp_path), capture_output=True, text=True)
    _g("init", "-q")
    _g("config", "user.email", "t@t")
    _g("config", "user.name", "t")
    for f in changed:
        (tmp_path / f).write_text("x = 1\n")
    _g("add", "-A")
    _g("commit", "-qm", "base")
    for f in changed:
        (tmp_path / f).write_text("x = 2\n")

    validation = SimpleNamespace(action="pass", unverified=[])
    quality = {"warnings": [], "confidence": confidence, "quality_signals": {}}
    rv.run_post_exec_checks(
        validation=validation, quality=quality,
        exec_result=SimpleNamespace(raw_output=""),
        # `id` 是**生产里一定有**的字段（真 Task 必有）—— 桩里漏了它，`_review` 一旦
        # 引用就会 AttributeError 被吞成 "multi-review 异常"，把真实行为淹掉。
        task=SimpleNamespace(id="t1", project_id=project_id, description="d"),
        agent_cfg={"model": "writer"}, level="any", cwd=str(tmp_path),
        changed=list(changed),
        # 真实调用路径（_exec.py）一定带基准。不给的话会命中新增的
        # "审查基准不可用 → fail-closed 全量跑"披露，让"全绿"用例失真 ——
        # 那条披露本身是对的，只是不该在这个用例里出现。
        base_ref="HEAD")
    return validation, quality


# ── 放行路径 ────────────────────────────────────────────────

def test_all_green_passes(monkeypatch, tmp_path):
    v, q = _run(monkeypatch, tmp_path)
    assert v.action == "pass"
    assert not v.unverified
    assert q["quality_signals"]["tests_passed"] == 1


def test_trivial_change_runs_tests_but_skips_review(monkeypatch, tmp_path):
    """小改动**照跑项目测试**，只跳多模型审查（2026-09-29 改）。

    原来这条叫 `test_trivial_change_skips_everything`、断言"连测试都不跑"——
    🔴 **而它从来没测到过任何东西**：它先 `setattr(val, "run_project_tests", spy)`，
    紧接着 `_run` 又 setattr 了**同一个属性** ⇒ 桩被覆盖、`calls` 永远是空的
    ⇒ `calls == []` 恒成立（改不改生产代码都绿）。
    （同族的"自检自己掐不断"见 `test-wiring` 那条。）想拦截 `_run` 里那一跑，
    **只能走它自己的通道**（`tests_fn=`）。
    """
    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return {"passed": True, "runner": "pytest", "total": 1}

    v, _ = _run(monkeypatch, tmp_path, changed=("a.py",), trivial=True, tests_fn=_spy)
    assert calls == [1], (
        "小改动没跑项目测试 —— 那模型就拿不到失败原因，而飞着的重试是它唯一的纠正机会"
        "（`round-20260928j` 那个病）")
    assert any("多模型审查已跳过" in u for u in v.unverified), v.unverified


# ── 基线差分：整仓红里"基线本来就红"的那几条不记这个任务的账 ─────────
#
# 来历（2026-09-29，原「疑3」）：worktree 从开工快照建 ⇒ 基线里任何一条红测试
# 都会让后面每个任务都过不了，而它改不动也不该改。见 `防御模式.md` §102。

_基线红输出 = (
    "F                                                                        [100%]\n"
    "=================================== FAILURES ===================================\n"
    "____________________ test_base_red _____________________\n"
    "test_base_red.py:8: in test_base_red\n"
    "    assert False\n"
    "=========================== short test summary info ============================\n"
    "FAILED test_base_red.py::test_base_red\n"
    "1 failed in 0.01s\n")


def test_红的全是基线就有的就不判重试(monkeypatch, tmp_path):
    """**接线**：差分算过、且红的全是基线就有的 ⇒ **不判 retry**，但如实记进 unverified。

    把 `val_mod.has_own_test_failure(test_result)` 换回 `not test_result.get("passed")`
    ⇒ 本用例红。⚠️ 只测 `has_own_test_failure` 是不够的（"函数对 ≠ 接线通"）。
    """
    v, _ = _run(monkeypatch, tmp_path, changed=("strkit.py",),
                tests={"passed": False, "runner": "pytest", "exit_code": 1,
                       "failures": 1, "output": _基线红输出},
                baseline={"passed": False, "runner": "pytest", "exit_code": 1,
                          "failures": 1, "output": _基线红输出})
    assert v.action == "pass", f"红的全是基线就有的，却还判了重试：{v.unverified}"
    assert any("基线" in u for u in v.unverified), f"放行了却不说为什么：{v.unverified}"


def test_基线算不出来就照旧判重试(monkeypatch, tmp_path):
    """反方向（fail-closed）：**没有基线**时红的照旧算这个任务的 ⇒ 判 retry。"""
    v, _ = _run(monkeypatch, tmp_path, changed=("strkit.py",),
                tests={"passed": False, "runner": "pytest", "exit_code": 1,
                       "failures": 1, "output": _基线红输出})
    assert v.action == "retry", "没有基线对照却说'不是它弄红的'"


# ── fail-closed：异常一律不放行 ──────────────────────────────

@pytest.mark.parametrize("knob, kind", [
    ("tests", "test_error"),
    ("crossover", "review_error"),
    ("audit", "security_error"),
])
def test_exception_never_passes(monkeypatch, tmp_path, knob, kind):
    """任何一步抛异常都不能当"没问题"放行 —— 这是 09-08 修过的 fail-open。"""
    def boom(*a, **k):
        raise RuntimeError("炸了")
    v, q = _run(monkeypatch, tmp_path, **{knob: boom})
    assert v.action == "retry", f"{knob} 异常被当成通过了"
    assert q["failure_kind"] == kind, q["failure_kind"]
    assert q["confidence"] < 0.5


def test_crossover_failcloses_without_reviewer(monkeypatch, tmp_path):
    """拿不到 reviewer 时不能返回"通过" —— 造一条 critical 硬拦（测真函数，不打桩）。"""
    from singularity.scheduler import validator as val, dispatcher as disp
    # diff 为空时 crossover_review 会提前返回 "pass"（:278），根本走不到 chain 检查 ——
    # 得先给它一段非空 diff，这条才测得到 fail-closed。
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: SimpleNamespace(stdout="diff --git a/a.py b/a.py\n+x=1\n"))
    monkeypatch.setattr(disp, "load_agents", lambda: {})
    monkeypatch.setattr(disp, "pick_agent_fallback_chain", lambda *a, **k: [])
    # ⚠️ `cwd` 必须是**真目录**：2026-09-29 起 `cwd` 为空 = "不知道改动落在哪" ⇒
    # 直接返回 `unavailable`、根本走不到下面那条 fail-closed。原来这儿写 `cwd=""`
    # 是拿它当"不关心"用 —— 那层含义是旧代码的兜底**偷偷**给上的。
    r = val.crossover_review(task_desc="d", raw_output="o", changed_files=["a.py"],
                             writer_level="any", writer_model="m", cwd=str(tmp_path))
    assert r["verdict"] == "retry"
    assert r["issues"] and r["issues"][0]["severity"] == "critical"


# ── 硬拦：该 retry 的必须 retry ─────────────────────────────

def test_failing_tests_retry(monkeypatch, tmp_path):
    v, q = _run(monkeypatch, tmp_path,
                tests={"passed": False, "failures": 3, "runner": "pytest", "output": "boom"})
    assert v.action == "retry" and q["failure_kind"] == "test_failure"


def test_local_scan_hit_retry(monkeypatch, tmp_path):
    """正则前置防线命中危险代码 → retry，不进入后面的 LLM 审查。"""
    v, q = _run(monkeypatch, tmp_path,
                scan={"issues": [{"detail": "硬编码密钥"}]})
    assert v.action == "retry" and q["failure_kind"] == "security"


def test_multi_review_critical_retry(monkeypatch, tmp_path):
    v, q = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                multi={"models_used": ["r1", "r2"],
                       "issues": [{"severity": "critical", "detail": "越权"}]})
    assert v.action == "retry" and q["failure_kind"] == "review_critical"


def test_multi_review_warning_is_soft_quality(monkeypatch, tmp_path):
    v, q = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                multi={"models_used": ["r1", "r2"],
                       "issues": [{"severity": "warning", "detail": "命名差"}]})
    assert q["failure_kind"] == "soft_quality"
    # 审查按文件循环（changed[:3]），每个文件的 warning 各记一次
    assert q["quality_signals"]["soft_warnings"] == 2


def test_verdicts_retry_drops_confidence_below_cascade_accept(monkeypatch, tmp_path):
    """≥2 个模型判 retry/abort（issues 为空）不能**只**设 action=retry。

    下游 `_decide_cascade` 的第一判据是 `conf >= 0.75` → 直接 ok=True 接受并合并，
    action 只是它的后置条件。而基线 0.5 + 长输出 0.1 + 含 "passed" 0.15 **正好 0.75**，
    所以只设 action 的话，**审查层最明确的否定结论会被省钱分支吃掉**，
    任务照样进 DONE 并合进项目仓（2026-09-19 外派评审核出、本机复核成立）。
    同段的 critical 分支扣 0.25 才拦得住 —— 两支的差别说明这里只是漏了。
    """
    # 起始分必须给到 0.75 以上，否则"压到 0.7 以下"这件事在夹具里根本不可见 ——
    # 0.85 = `post_execution_hook` 的基线 0.5 + 长输出 0.1 + 含 "passed" 0.15
    # + 项目测试通过 0.1，就是真机那串加分的落点。
    v, q = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                confidence=0.85,
                multi={"models_used": ["r1", "r2"], "issues": [],
                       "verdicts": [{"model": "r1", "verdict": "retry"},
                                    {"model": "r2", "verdict": "abort"}]})
    assert v.action == "retry"
    assert q["confidence"] <= 0.7, "分数没压下去 → 下游 conf>=0.75 会直接放行合并"
    assert q["failure_kind"] == "review_needs_fix"
    assert any("retry/abort" in w for w in q["warnings"])
    assert any("retry/abort" in u for u in v.unverified), "交付报告看不到 = 没披露"


def test_single_verdict_retry_does_not_hard_block(monkeypatch, tmp_path):
    """**一个**模型判 retry 不算数（阈值是 ≥2）—— 别把上面的修法做成"一票否决"。"""
    v, q = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                multi={"models_used": ["r1", "r2"], "issues": [],
                       "verdicts": [{"model": "r1", "verdict": "retry"},
                                    {"model": "r2", "verdict": "pass"}]})
    assert v.action == "pass"
    assert q["confidence"] == 0.6, "0.5 + 测试通过 0.1：不该被这条分支扣分"


def test_short_circuited_checks_are_disclosed(monkeypatch, tmp_path):
    """被短路掉的检查必须如实进 unverified。

    短路本身是有意的（省钱：这一版马上会被重试覆盖），有测试钉着它
    （test_test_timeout_retries_and_returns）。缺的是**跳过了不说** ——
    只看 warnings 的那份 quality 出不了这个函数，交付报告/人审只读 unverified，
    不说就等于把"没跑"和"跑过了"混为一谈。
    """
    v, _ = _run(monkeypatch, tmp_path,
                tests={"passed": False, "failures": 3, "runner": "pytest", "output": "boom"})
    assert v.action == "retry"
    note = [u for u in v.unverified if "前面已判" in u]
    assert note, v.unverified
    for name in ("多模型审查", "QA 约束验收", "安全审计"):
        assert name in note[0], note[0]


def test_all_green_does_not_report_skipped(monkeypatch, tmp_path):
    """全绿时不许冒出"未跑"的噪声 —— 误报会让这条披露变得不可信。"""
    v, _ = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}])
    assert v.action == "pass" and not v.unverified, v.unverified


def test_crossover_abort_propagates(monkeypatch, tmp_path):
    """审查判 abort 必须传出去，不能被后面的步骤冲掉。"""
    v, _ = _run(monkeypatch, tmp_path,
                crossover={"issues": [], "verdict": "abort", "summary": "方向错了"})
    assert v.action == "abort"
    assert any("review abort" in u for u in v.unverified)


# ── 软信号：不该误伤 ────────────────────────────────────────

def test_requirement_conformance_miss_is_soft(monkeypatch, tmp_path):
    """需求符合性只写软信号 —— 机械关键词对账，设 hard gate 会大面积误伤。"""
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                conformance=SimpleNamespace(
                    passed=False,
                    evidence={"passed": 3, "total": 5, "failed_items": ["审计日志", "限流"]}))
    assert v.action == "pass", "需求符合性不该硬拦"
    assert q["quality_signals"]["requirement_conformance"]["passed"] == 3
    assert any("3/5" in w for w in q["warnings"])


def test_requirement_conformance_not_run_is_not_recorded_as_passed(monkeypatch, tmp_path):
    """🔴 **"没查"不许在 QA 报告里写成 "passed"**（2026-09-26，Qoder「判据层」那条的后果面）。

    变异：把 `_review.py` 里 `"status": "not_run" if ev.get("not_run") …` 那段换回
    原来的 `quality["quality_signals"]["requirement_conformance"] = "passed"` ⇒ 本条红。

    为什么值得一条命门：`passed=True` 里混着**三种完全不同**的处境 ——
    真数了 N/M 全过 · **没有追溯表**（跳过）· **调用方没给产出**（核验不了）。
    而 `requirement_conformance` 这个键**是有人读的**（进 `validation.quality_signals`
    → QA 报告），前一种和后两种在报告里长得一模一样，排查方向却正好相反。
    """
    for ev, why in (
        ({"hard": False, "not_run": "无追溯表"}, "没有追溯表"),
        ({"hard": False, "not_run": "调用方没给产出"}, "调用方没给产出"),
    ):
        v, q = _run(monkeypatch, tmp_path, project_id="p1",
                    conformance=SimpleNamespace(
                        passed=True, reason=f"需求符合性: {why}，无法核验 —— 这不是通过",
                        evidence=dict(ev)))
        sig = q["quality_signals"]["requirement_conformance"]
        assert sig["status"] == "not_run", \
            f"{why} ⇒ 报告里却写成 {sig.get('status')!r} —— 和'真数过了'分不开"
        assert sig["reason"], "得留下为什么没查"
        assert v.action == "pass", "没查不等于失败：不硬拦（软信号）"
        assert not any("需求符合性" in w for w in q["warnings"]), \
            f"没查不该报'未通过'的警：{q['warnings']}"


def test_requirement_conformance_pass_recorded_as_passed(monkeypatch, tmp_path):
    """**对照（防修过头）**：真数过了 ⇒ 照旧记 `passed`。"""
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                conformance=SimpleNamespace(
                    passed=True, reason="需求符合性: 5/5 全部通过",
                    evidence={"hard": True, "total": 5, "passed": 5, "all_passed": True}))
    assert q["quality_signals"]["requirement_conformance"]["status"] == "passed"
    assert v.action == "pass"


def test_qa_constraint_fail_retries(monkeypatch, tmp_path):
    """QA 约束验收是硬拦（补 multi_model_review 不查的约束维度）。"""
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}],
                qa={"verdict": "needs_fix",
                    "verifications": [{"status": "fail", "constraint": "必须用 PostgreSQL"}]})
    assert v.action == "retry" and q["failure_kind"] == "constraint_fail"


def test_qa_needs_fix_without_specifics_does_not_retry(monkeypatch, tmp_path):
    """needs_fix 却列不出任何具体项 → **结论不可用**，不据此重试。

    2026-09-11 探路轮实测：T1 卡在这上面 —— `action=retry` 但所有条目都不是
    fail/warning，消息成了 "QA 约束验收 0 条未满足: "（冒号后面空的）。
    重试方只知道"要修"却不知道修什么，下一轮必然同样结果，**循环收敛不了**
    直到撞 900s 超时失败。

    拦住不说 = 比不拦更坏：烧钱、耗时，还不给任何可动手的信息。
    所以这里既不 retry，也不假装"验收通过"—— 如实进 unverified。
    """
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}],
                qa={"verdict": "needs_fix", "verifications": []})
    assert v.action == "pass", "拿不到具体项的 needs_fix 不该驱动重试"
    assert any("结论不可用" in u for u in v.unverified), "必须如实披露，不能静默"
    assert q["quality_signals"]["qa_acceptance"] == "needs_fix_unspecified"
    assert "constraint_fail" != q.get("failure_kind"), "不可执行的结论不算 constraint_fail"


def test_qa_needs_fix_all_pass_entries_also_unusable(monkeypatch, tmp_path):
    """模型自相矛盾（summary 判 needs_fix、条目全标 pass）也归"不可用"。"""
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}],
                qa={"verdict": "needs_fix",
                    "verifications": [{"status": "pass", "constraint": "必须用 PostgreSQL"}]})
    assert v.action == "pass"
    assert any("结论不可用" in u for u in v.unverified)


def test_test_timeout_retries_and_returns(monkeypatch, tmp_path):
    """测试跑超时不能默认通过，且**不再往下走**（后面每一步都要花钱）。"""
    import time
    monkeypatch.setattr(rv, "_REVIEW_TIMEOUT_SEC", 0.05)
    v, q = _run(monkeypatch, tmp_path, tests_fn=lambda *a, **k: time.sleep(0.3) or {})
    assert v.action == "retry" and q["failure_kind"] == "review_timeout"
    assert "security_audit" not in q["quality_signals"], "超时后不该继续跑后面的步骤"


class TestReviewerPoolExpansion:
    """2 模型阵容下 reviewer 凑不齐 → 从模型注册表补人（会调用未启用的模型，真花钱）。

    必须在**两边**都生效：`_review` 补出候选名单，`multi_model_review` 还得认它。
    只改一边的话，传进去的名字被静默跳过 —— 扩了等于没扩，而外面看到的是
    "multi-review 跑过了"。
    """

    def _fake_registry(self, monkeypatch, available=("extra-a", "extra-b")):
        from singularity.scheduler import model_registry as mr, dispatcher as disp
        monkeypatch.setattr(mr, "load_models",
                            lambda: {"writer": object(), "extra-a": object(),
                                     "extra-b": object()})
        monkeypatch.setattr(disp, "agent_api_available",
                            lambda cfg: cfg.get("model") in available
                            and bool(cfg.setdefault("type", "openai-agent")))
        return disp

    def test_expands_and_stops_at_want(self, monkeypatch):
        from singularity.scheduler import _review as rv
        monkeypatch.delenv("QIDIAN_REVIEW_POOL_EXPAND", raising=False)
        disp = self._fake_registry(monkeypatch)
        assert rv._expand_review_pool(disp, "writer", []) == ["extra-a", "extra-b"]
        # 已经有一个 reviewer 时只补一个 —— 花钱的调用按需补，不铺满
        assert rv._expand_review_pool(disp, "writer", ["already"]) == ["extra-a"]

    def test_skips_writer_and_unavailable(self, monkeypatch):
        from singularity.scheduler import _review as rv
        monkeypatch.delenv("QIDIAN_REVIEW_POOL_EXPAND", raising=False)
        disp = self._fake_registry(monkeypatch, available=("extra-b",))   # 只有 b 可用
        out = rv._expand_review_pool(disp, "writer", [])
        assert out == ["extra-b"]           # writer 被排除；extra-a 不可用被跳过

    def test_kill_switch(self, monkeypatch):
        from singularity.scheduler import _review as rv
        monkeypatch.setenv("QIDIAN_REVIEW_POOL_EXPAND", "0")
        assert rv._expand_review_pool(self._fake_registry(monkeypatch), "writer", []) == []

    def test_multi_review_actually_uses_registry_model(self, monkeypatch, tmp_path):
        """补进来的注册表模型必须真的被 multi_model_review 用上。"""
        from singularity.scheduler import validator as val, dispatcher as disp
        monkeypatch.setattr("subprocess.run",
                            lambda *a, **k: SimpleNamespace(stdout="diff --git a/x.py b/x.py\n+x=1\n"))
        monkeypatch.setattr(disp, "load_agents", lambda: {})        # 启用池是空的
        monkeypatch.setattr(disp, "agent_api_available",
                            lambda cfg: bool(cfg.setdefault("type", "openai-agent")))
        monkeypatch.setattr(disp, "dispatch", lambda *a, **k: SimpleNamespace(
            executor_result=SimpleNamespace(
                raw_output='{"issues":[],"verdict":"pass","summary":"ok"}')))
        # 同上面那条：`cwd` 得给真目录（为空 = 不知道改动落在哪 ⇒ 直接 `unavailable`）
        r = val.multi_model_review("x.py", models=["registry-only"],
                                   cwd=str(tmp_path), diff_only=True)
        assert r.get("error") != "no models available", r
        assert "registry-only" in r.get("models_used", []), r


class TestNoCwdIsNotSilent:
    """拿不到 `cwd` ⇒ **不审、也不装作审过**（2026-09-29）。

    同 `_hard_diff_rules` 那次拆兜底（2026-09-27，把**新建**的测试判成"已删除"）：
    `cwd or config.PROJECT_ROOT` 会让 diff 在**奇点自己那个仓**里算 ⇒ 恒为空 ⇒
    `crossover_review` 早退 `pass`、`multi_model_review` 返回 `{"issues": []}`
    ⇒ **审查在"一条改动都没看到"的情况下放行**。
    生产调用点都显式传 `cwd`（`_exec.py` 那句恒为真值）⇒ 现在走不到，
    拆的是「将来新调用点不传就当场复发」这个必然性。
    """

    def test_交叉审查拿不到cwd不许装通过(self, monkeypatch):
        from singularity.scheduler import validator as val
        # 就算 subprocess 能返回一段像样的 diff，也不许拿别的根去算
        monkeypatch.setattr("subprocess.run",
                            lambda *a, **k: SimpleNamespace(stdout="diff --git a/a.py b/a.py\n+x=1\n"))
        r = val.crossover_review(task_desc="d", raw_output="o", changed_files=["a.py"],
                                 writer_level="any", writer_model="m", cwd="", base_ref="HEAD")
        assert r["verdict"] == "unavailable", f"没拿到 cwd 却给了个像样的裁决：{r}"
        assert "没拿到任务工作目录" in r["summary"], r
        assert r["issues"] == [], "「没做」不该造出 issues（那是判失败）"

    def test_多模型审查拿不到cwd要能被上层认出来(self):
        from singularity.scheduler import validator as val
        r = val.multi_model_review("x.py", models=["m"], cwd="", diff_only=True, base_ref="HEAD")
        assert r.get("unavailable") == "没拿到任务工作目录(cwd)", r
        assert r["models_used"] == [] and r["issues"] == []

    # ── 接线：上面那两个「函数对」不算数，得看 `_review` 有没有把它说出来 ──
    def test_多模型审查没做成要进未经审查(self, monkeypatch, tmp_path):
        """**接线**：`unavailable` ⇒ 必须进 `unverified`（否则交付报告照写 `delivered`）。

        把 `if review.get("unavailable"):` 那支去掉 ⇒ 落进下面那句写死的
        "审查输入为空"，**原因就变成了错的**（真问题是没传 cwd，不是输入为空）⇒ 本用例红。
        """
        v, _ = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                    multi={"unavailable": "没拿到任务工作目录(cwd)",
                           "models_used": [], "issues": [], "verdicts": [], "summaries": []})
        assert any("没拿到任务工作目录" in u for u in v.unverified), v.unverified

    def test_交叉审查没做成要进未经审查(self, monkeypatch, tmp_path):
        """**接线**：单模型那条路（`pool=()`）走 `crossover_review`，同样要披露。"""
        v, _ = _run(monkeypatch, tmp_path, pool=(),
                    crossover={"issues": [], "verdict": "unavailable",
                               "summary": "没拿到任务工作目录(cwd) —— diff 没算"})
        assert any("没拿到任务工作目录" in u for u in v.unverified), v.unverified


class TestNeedsFixCarriesReason:
    """🔴 **"判重做"必须把理由一起发出去**（2026-09-30）。

    prompt 契约允许 `issues:[]` + `verdict:retry` —— 那种情形下**理由只存在于 `summaries`**，
    而这个键**全历史没人读过**（`docs/防御模式.md` §107）。原来发出去的只有
    `r1:retry; r2:abort`：模型被告知重做、一个字原因都没有 ⇒ 只能盲改（同 `50337081` 那个病）。
    """

    def test_没给issues时理由要从summaries带出来(self, monkeypatch, tmp_path):
        """变异：拿掉把 `summaries` 拼进 `_nf` 那一段 ⇒ 本用例红。"""
        v, _ = _run(monkeypatch, tmp_path,
                    pool=[{"model": "r1"}, {"model": "r2"}],
                    multi={"models_used": ["r1", "r2"], "issues": [],
                           "verdicts": [{"model": "r1", "verdict": "retry"},
                                        {"model": "r2", "verdict": "abort"}],
                           "summaries": [{"model": "r1", "summary": "空文件那条路没处理"},
                                         {"model": "r2", "summary": "退出码不是 3"}]})
        joined = " ".join(v.unverified)
        assert "空文件那条路没处理" in joined, v.unverified
        assert "退出码不是 3" in joined, v.unverified

    def test_给了issues就不叠理由(self, monkeypatch, tmp_path):
        """反方向：有 `issues` 时那一跳本来就把细节送出去了，别再加一层噪音。"""
        v, _ = _run(monkeypatch, tmp_path,
                    pool=[{"model": "r1"}, {"model": "r2"}],
                    multi={"models_used": ["r1", "r2"],
                           "issues": [{"model": "r1", "severity": "warning",
                                       "line": 3, "detail": "没处理空文件"}],
                           "verdicts": [{"model": "r1", "verdict": "retry"},
                                        {"model": "r2", "verdict": "abort"}],
                           "summaries": [{"model": "r1", "summary": "不该出现的理由"},
                                         {"model": "r2", "summary": "不该出现的理由"}]})
        assert not any("不该出现的理由" in u for u in v.unverified), v.unverified


class TestReviewFailLimitUsesStreak:
    """🔴 **触顶判据只许看"连续"计数，不许看历史累计**（2026-09-30 `round-20260929e` 真机撞出来的）。

    拿 `review_failures`（只增不减）去比阈值 2 ⇒ **任何 6 任务的项目，集成合并成功后必被
    顶回 GATE2 人工兜底** ⇒ §四 明写"停在 gate2 兜底不算" ⇒ **③ 结构上不可达**。
    盘上对照（不是推理）：**唯一走到 `done` 的项目 `review_failures = 0`**；
    `e` 轮 37 · `i` 轮 21 · `f` 轮 18 —— **前八轮是没跑到这堵墙就死了，`e` 是第一个跑到的**。

    变异：把 `orchestrator` 那处的入参换回 `review_failures` ⇒
    `test_累计再大也不该顶回GATE2` 红。
    """

    def test_累计再大也不该顶回GATE2(self, monkeypatch, tmp_path):
        from singularity.scheduler import project as P
        import singularity.scheduler.orchestrator as orch
        from singularity.scheduler.orchestrator import _run_integration_merge_async

        monkeypatch.setattr(orch, "_run_integration_merge", lambda proj: (True, "ok"))
        # 后一支会去跑验收循环 —— 那是另一件事（而且很重），这里只关心**有没有被顶回兜底门**
        monkeypatch.setattr("singularity.scheduler.workflow.run_test_fix_loop",
                            lambda proj, agents: "（测试替身）")
        p = P.create(name="_t_review_streak", template="feature", description="x")
        p.phase = P.Phase.INTEGRATING
        p.review_failures = 37            # 历史累计很大（round-e 的真实值）
        p.review_failures_streak = 0      # 但**期间有任务成功过** ⇒ 早就清零了
        P.save(p)

        _run_integration_merge_async(p.id, {})

        after = P.load(p.id)
        assert str(getattr(after.phase, "value", after.phase)) != "gate2", (
            f"历史累计 37 又把项目顶回兜底门了 —— 那就是 e 轮撞的那堵墙：{after.phase}")

    def test_连续没修好才顶(monkeypatch):
        rv = _review_of()
        assert rv.check_review_fail_limit("p1", rv._REVIEW_MAX_AUTO_FIX - 1)["blocked"] is False
        assert rv.check_review_fail_limit("p1", rv._REVIEW_MAX_AUTO_FIX)["blocked"] is True

    def test_任务成功要清零_失败不清(self, tmp_path, monkeypatch):
        """**接线**：清零挂在 `_archive_task_outcome` 上，而且**只看 DONE**。

        失败也清零的话，一个一直失败的项目会被自己的每次收尾重置 ⇒ **永远到不了触顶**。
        变异：把判据改成"只要收了尾就清" ⇒ 本条红。
        """
        import singularity.scheduler._task_runner as tr
        from singularity.scheduler import config, project as P, tracker
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
        monkeypatch.setattr(P, "_projects_dir", lambda: tmp_path, raising=False)
        monkeypatch.setattr(tr.mem_mod, "archive_experience", lambda **k: None)
        monkeypatch.setattr(tr, "record_tokens", lambda **k: None)
        monkeypatch.setattr(tr.rl_mod, "record_outcome", lambda **k: None)

        proj = P.ProjectState(id="p_streak", name="n", phase=P.Phase.EXECUTING)
        proj.review_failures_streak = 5
        P.save(proj)

        failed = tracker.create("失败的任务", project_id="p_streak")
        failed.status = tracker.TaskStatus.FAILED
        tr._archive_task_outcome(failed, None, _Disp())
        assert P.load("p_streak").review_failures_streak == 5, \
            "失败的任务把'连续没修好'的计数清零了 ⇒ 一个一直失败的项目永远到不了触顶"

        done = tracker.create("成功的任务", project_id="p_streak")
        done.status = tracker.TaskStatus.DONE
        tr._archive_task_outcome(done, None, _Disp())
        assert P.load("p_streak").review_failures_streak == 0, \
            "任务做成了（前进了一步）却没清零 ⇒ 触顶会误伤全绿的项目"


def _review_of():
    from singularity.scheduler import _review as rv
    return rv


class _Disp:
    """最小 disp_result 替身（`_archive_task_outcome` 只读这几个字段）。"""
    class _Res:
        token_count = 0
        elapsed = 0.0
        changed_files: list = []
    executor_result = _Res()
    agent_cfg = {"model": "m"}


class TestReviewFailLimit:
    """审查自动修触顶 → 升 GATE2 人工兜底。失效就等于无限自动重试，烧钱不止。"""

    def test_blocks_at_limit(self):
        r = rv.check_review_fail_limit("p1", rv._REVIEW_MAX_AUTO_FIX)
        assert r["blocked"] and r["action"] == "escalate_to_gate2" and r["remaining"] == 0

    def test_continues_below_limit(self):
        r = rv.check_review_fail_limit("p1", 0)
        assert not r["blocked"] and r["action"] == "continue"
        assert r["remaining"] == rv._REVIEW_MAX_AUTO_FIX


def test_qa_status_casing_does_not_dodge_the_gate(monkeypatch, tmp_path):
    """QA 返回 "Fail"（大写）同样算未满足 —— 精确匹配会让它溜过去。"""
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}],
                qa={"verdict": "needs_fix",
                    "verifications": [{"status": "FAIL", "constraint": "必须用 PostgreSQL"}]})
    assert v.action == "retry" and q["failure_kind"] == "constraint_fail"


def test_qa_constraint_error_retries(monkeypatch, tmp_path):
    """QA 验收自己炸了也不能放行（fail-closed）。"""
    def boom(*a, **k):
        raise RuntimeError("qa 炸了")
    v, q = _run(monkeypatch, tmp_path, project_id="p1",
                constraints=[{"text": "必须用 PostgreSQL"}], qa=boom)
    assert v.action == "retry" and q["failure_kind"] == "constraint_error"


# ── 审查截断的诚实披露 ──────────────────────────────────────

def test_more_than_three_files_records_unreviewed(monkeypatch, tmp_path):
    """只审前 3 个文件是**有意为之**（成本控制），但必须记进 unverified。

    本模块的原则：「可以放行，但不把'通过'和'已验证'混为一谈」—— verdict=通过 +
    unverified 非空 = 诚实放行（交付状态变 delivered_unverified）。
    旧代码只发一条告警就完事：排障的人看得到，**用户看的交付报告仍写 delivered**，
    而 1/4 的改动没有任何人看过。
    """
    v, _ = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                changed=("a.py", "b.py", "c.py", "d.py"))
    assert any("未审查" in u and "d.py" in u for u in v.unverified), v.unverified
    # 告警要带**主体**（2026-09-19）：09-17 那个悬案（"46 个 trace 里一条『未审查』
    # 都没有，查不出是哪个任务触发的"）卡的就是这条告警只有条数。
    _msg = " ".join(str(a) for a, _ in WARNS)
    assert "review_files_truncated" in _msg and "t1" in _msg, WARNS


def test_three_files_not_flagged_as_unreviewed(monkeypatch, tmp_path):
    """刚好 3 个是全审了的，不该误报。"""
    v, _ = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                changed=("a.py", "b.py", "c.py"))
    assert not any("未审查" in u for u in v.unverified), v.unverified


# ── 测试失败的**证据**要能看出"哪条、为什么" ────────────────────
#
# 🔴 来历（`round-20260928j` 真机，2026-09-28）：T1 挂了，而落盘的唯一证据是
# `output[:200]` —— 那 200 字符**正好**是 pytest `-q` 的开头：进度行 + `FAILURES`
# 标题 + **失败测试名的前半截** ⇒ 存的是一句
# `tests failed: .......F......  ____ test_common_options_ma`，
# 测试名断在中间、断言一行都没有。真正的原因从第 264 字符才开始。
# ⇒ **判了失败，却说不出哪条、为什么**，人只能把产物捞出来重跑一遍。

def test_测试失败时落盘的证据要能看出哪条为什么(monkeypatch, tmp_path):
    """**接线**：`_review` 得把失败证据**完整**放进 `unverified`。

    把 `failed_output_tail(...)` 换回 `output[:200]` ⇒ 本用例红。
    ⚠️ 只测 `failed_output_tail` 是没用的 —— 那正是"函数对 ≠ 接线通"。
    """
    真机输出 = (
        ".......F......................                                           [100%]\n"
        "=================================== FAILURES ===================================\n"
        "________________ test_common_options_may_precede_the_subcommand ________________\n"
        "tests/test_cli_parse_args.py:145: in test_common_options_may_precede_the_subcommand\n"
        "    assert args.format == \"tsv\"\n"
        "E   AssertionError: assert 'json' == 'tsv'\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_cli_parse_args.py::test_common_options_may_precede_the_subcommand\n"
        "1 failed, 29 passed in 0.73s\n")
    v, q = _run(monkeypatch, tmp_path, tests={
        "passed": False, "runner": "pytest", "exit_code": 1, "failures": 1,
        "output": 真机输出})

    assert v.action == "retry"
    证据 = " ".join(str(u) for u in v.unverified)
    assert "test_common_options_may_precede_the_subcommand" in 证据, \
        f"测试名被截断了 = 说不出是哪条：{证据[:200]}"
    assert "assert 'json' == 'tsv'" in 证据, "断言行没了 = 说不出为什么"
    assert "1 failed" in 证据, "汇总行没了 = 说不出几条"
