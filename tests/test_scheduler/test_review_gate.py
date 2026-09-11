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


def _run(monkeypatch, tmp_path, *, project_id="", changed=("a.py", "b.py"),
         pool=(), scan=None, tests=None, multi=None, crossover=None,
         qa=None, conformance=None, audit=None, constraints=None,
         trivial=False, tests_fn=None):
    """跑一次门禁，返回 (validation, quality)。默认全绿，各测试只覆盖关心的那一段。

    pool=() → 走单模型 crossover fallback；给 2 个模型才走多人审查。
    project_id="" → 跳过 QA 约束 / 需求符合性 / 审查失败计数（不碰磁盘）。
    """
    from singularity.scheduler import validator as val, dispatcher as disp, project as proj_mod

    monkeypatch.setattr(rv.witness, "warn", lambda *a, **k: None)
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
    quality = {"warnings": [], "confidence": 0.5, "quality_signals": {}}
    rv.run_post_exec_checks(
        validation=validation, quality=quality,
        exec_result=SimpleNamespace(raw_output=""),
        task=SimpleNamespace(project_id=project_id, description="d"),
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


def test_trivial_change_skips_everything(monkeypatch, tmp_path):
    """单文件小改动不该为审查付 90s 开销（跳过后连测试都不跑）。"""
    calls = []
    from singularity.scheduler import validator as val
    monkeypatch.setattr(val, "run_project_tests",
                        lambda *a, **k: calls.append(1) or {"passed": True, "runner": "pytest"})
    v, _ = _run(monkeypatch, tmp_path, changed=("a.py",), trivial=True)
    assert v.action == "pass" and calls == []


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


def test_crossover_failcloses_without_reviewer(monkeypatch):
    """拿不到 reviewer 时不能返回"通过" —— 造一条 critical 硬拦（测真函数，不打桩）。"""
    from singularity.scheduler import validator as val, dispatcher as disp
    # diff 为空时 crossover_review 会提前返回 "pass"（:278），根本走不到 chain 检查 ——
    # 得先给它一段非空 diff，这条才测得到 fail-closed。
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: SimpleNamespace(stdout="diff --git a/a.py b/a.py\n+x=1\n"))
    monkeypatch.setattr(disp, "load_agents", lambda: {})
    monkeypatch.setattr(disp, "pick_agent_fallback_chain", lambda *a, **k: [])
    r = val.crossover_review(task_desc="d", raw_output="o", changed_files=["a.py"],
                             writer_level="any", writer_model="m", cwd="")
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

    def test_multi_review_actually_uses_registry_model(self, monkeypatch):
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
        r = val.multi_model_review("x.py", models=["registry-only"], cwd="", diff_only=True)
        assert r.get("error") != "no models available", r
        assert "registry-only" in r.get("models_used", []), r


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


def test_three_files_not_flagged_as_unreviewed(monkeypatch, tmp_path):
    """刚好 3 个是全审了的，不该误报。"""
    v, _ = _run(monkeypatch, tmp_path, pool=[{"model": "r1"}, {"model": "r2"}],
                changed=("a.py", "b.py", "c.py"))
    assert not any("未审查" in u for u in v.unverified), v.unverified
