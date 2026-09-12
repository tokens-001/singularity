#!/usr/bin/env python3
"""_exec.run() 回归测试 — 守护 worktree 生命周期对称性 + 8 条退出路径语义。

为什么存在:
  run() 是全仓最复杂的函数 (while→for→cascade 多层)。任何重构 (P2-1 拆函数)
  极易把代码挪出 try/finally 边界, 导致 worktree 泄漏, 而 smoke 测不出 (loop 不跑)。
  本测试用 monkeypatch 桩掉所有外部依赖 (不碰真 API / 真 git / 真 worktree),
  对每条退出路径断言:
    ① 语义正确 (返回的 BatchOutput.action / term_reason / planner_decomposed / merge_request)
    ② 不变量: 每个被创建的 worktree 都被清理 (created_ids == cleaned_ids)

跑法:  QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py
绿 = 重构没破坏 run() 行为。红 = 立刻停手, 看哪条路径断了。
不依赖 pytest。退出码 0=全过, 1=有失败。
"""
import os, sys, tempfile
os.environ["QIDIAN_SKIP_EMBED"] = "1"

from pathlib import Path
from singularity.scheduler import _exec
from singularity.scheduler._types import RunContext

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

# ── 桩: worktree 生命周期记账 ──────────────────────────────
CREATED, CLEANED = [], []
class FakeWT:
    _seq = 0
    def __init__(self):
        FakeWT._seq += 1
        self.id = FakeWT._seq
        self.path = Path(f"/tmp/fake_wt_{self.id}")

def reset_wt():
    CREATED.clear(); CLEANED.clear()

# ── 桩: executor / validation 结果 ─────────────────────────
class FakeExec:
    def __init__(self, success=True, raw_output="结果", changed_files=None,
                 error_kind="exec", error="boom"):
        self.success = success; self.raw_output = raw_output
        self.changed_files = changed_files or []; self.tool_events = []
        self.error_kind = error_kind; self.error = error
        self.elapsed = 0.0; self.tokens = 0; self.token_count = 0

class FakeDispResult:
    def __init__(self, exec_result):
        self.executor_result = exec_result
        self.agent_cfg = {"model": "fake"}; self.level = "any"; self.attempts = 1

class FakeVal:
    """模拟 val_mod.ValidationReport 的最小接口。"""
    def __init__(self, action="pass", confidence=0.9):
        self.action = action; self.confidence = confidence
        self.verdict = "通过" if action == "pass" else "阻断"
        self.unverified = []; self.evidence = {}; self.quality_signals = {}
        self.turns_used = 0

# ── 场景容器: 每个用例编排 dispatch / validate 的逐次行为 ──
class Scenario:
    def __init__(self):
        self.dispatch_queue = []   # 每次 dispatch() 弹一个: ("ok",FakeExec)|("fail",FakeExec)|("raise",)
        self.validate_queue = []   # 每次 validate() 弹一个 FakeVal
        self.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 3}]
        self.escalate_to = None    # escalate(level) 返回值
        self.task = None
        self.last_chain_kw = None  # 最近一次 pick_agent_fallback_chain 收到的 kwargs
        self.last_dispatch_kw = None  # 最近一次 dispatch 收到的 kwargs
S = Scenario()

# ── 安装桩 (打到 _exec 命名空间) ───────────────────────────
def install_stubs():
    _tmp = Path(tempfile.mkdtemp())
    _exec.config.CANCEL_DIR = _tmp / "cancel"
    (_exec.config.CANCEL_DIR).mkdir(parents=True, exist_ok=True)
    # 这个脚本是独立跑的、不走 tests/conftest.py —— `_persist_partial_usage` 会
    # 调 `ensure_dirs()`，而 PARTIAL_USAGE_DIR 是**导入时**算好的真路径：
    # 不指走，一跑就把生产 `.qidian/partial_usage/` 建出来（§56 同族）。
    _exec.config.PARTIAL_USAGE_DIR = _tmp / "partial_usage"

    _exec.witness.heartbeat = lambda *a, **k: None
    _exec._inject_memory = lambda d: ""
    _exec._build_project_context = lambda t: ""
    _exec._save_planner_patch = lambda tid, c: None
    _exec.tracker.read_task = lambda tid: S.task

    def fake_create(*a, **k):
        wt = FakeWT(); CREATED.append(wt.id); return wt
    _exec._maybe_create_worktree = fake_create
    _exec._lock_wt = lambda wt: None
    def fake_cleanup(wt):
        if wt is not None: CLEANED.append(wt.id)
    _exec._cleanup_wt = fake_cleanup

    _exec.commit_wt = lambda wt: "fakebranchref"
    _exec._anchor_ref = lambda tid, ref, repo_root=None: None
    _exec._build_merge_request = lambda task, br, base, repo_root=None: "FAKE_MR"
    _exec.wt_merge_back = lambda wt, repo_root=None: type("MR", (), {"ok": False, "reason": "冲突", "conflicts": ["f"]})()

    # dispatcher
    class FakeDispMod:
        def pick_agent_fallback_chain(self, agents, level, **k):
            S.last_chain_kw = k
            return list(S.chain)
        def escalate(self, level): return S.escalate_to
        def dispatch(self, *a, **k):
            S.last_dispatch_kw = k
            kind, *rest = S.dispatch_queue.pop(0)
            if kind == "raise":
                raise RuntimeError("模拟: pick_agent 无可用 agent")
            return FakeDispResult(rest[0])
    _exec.disp_mod = FakeDispMod()

    # validator —— validate 弹 FakeVal 并记录其置信度; post_hook 回填同一置信度,
    # 模拟真代码 validation.confidence = post_hook().confidence, 但保留用例设定的值。
    _exec.val_mod.pre_execution_hook = lambda d, s: []
    def _validate(**k):
        v = S.validate_queue.pop(0)
        S._cur_conf = v.confidence
        return v
    _exec.val_mod.validate = _validate
    _exec.val_mod.post_execution_hook = lambda er, s: {
        "confidence": getattr(S, "_cur_conf", 0.9),
        "quality_signals": {}, "warnings": [], "failure_kind": "ok"}

    # supervisor QA 门禁: 用例切 S.qa_verdict ("pass"/"fail"/"escalate")
    import singularity.scheduler.supervisor as _sup_mod
    S.qa_verdict = "pass"
    _sup_mod.supervise = lambda *a, **k: type(
        "V", (), {"verdict": S.qa_verdict, "issues": ["stub QA 问题"]})()

def make_task():
    return type("T", (), {
        "id": "1234567890123", "description": "测试任务", "route_level": "any",
        "route_gate": False, "route_type": "default", "depends_on": [],
        "retry_count": 0, "max_retries": 0, "depth": 0, "project_id": "",
        "status": None,
    })()

def make_ctx(v3=True):
    return RunContext(batch_id="b", snapshot_ref="snapref",
                      merge_queue=object() if v3 else None)

def run_case(v3=True):
    S.task = make_task()
    return _exec.run(S.task, make_ctx(v3), {"any": list(S.chain)})

# ═══════════════════════════════════════════════════════════
# 用例
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    install_stubs()

    print("── 路径1: 校验通过 (v3, 带 merge_request) ──")
    reset_wt()
    S.dispatch_queue = [("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="pass")]
    b = run_case(v3=True)
    check("返回 pass", b.validation.action == "pass")
    check("带 merge_request", b.merge_request == "FAKE_MR")
    check("非 planner_decomposed", b.planner_decomposed is False)
    check("worktree 对称 (建=清)", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 接线: 每轮把累计用量落盘 (防御模式 §59) ──")
    # 单独一条是因为：`_persist_partial_usage` 自己有单测，但**函数对 ≠ 接线通** ——
    # 超时的账之所以丢，坏的就是接线（run 根本不记）。这条钉的是"run() 真会调"。
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
    _e = FakeExec(success=True); _e.token_count = 4321
    S.dispatch_queue = [("ok", _e)]
    S.validate_queue = [FakeVal(action="pass")]
    _t = make_task()
    S.task = _t
    _exec.run(_t, make_ctx(v3=True), {"any": list(S.chain)})
    _tok, _mdl = _exec.read_partial_usage(_t.id)
    check("run() 真把用量落进 sidecar", _tok == 4321, f"读到 {_tok}")
    # 取的是**chain 里那个** model（agent_cfg["model"]），不是 FakeDispResult 里那个 ——
    # _exec 手上只有前者。对，因为真要记账的就是这条链上跑的模型。
    check("模型名也带上 (记账要按单价算钱, 空串会进 unpriced)", _mdl == "m1", f"读到 {_mdl!r}")

    print("── 路径2: 用户取消 ──")
    reset_wt()
    S.task = make_task()
    (_exec.config.CANCEL_DIR / f"{S.task.id}.json").write_text("{}")
    S.dispatch_queue = []; S.validate_queue = []
    b = _exec.run(S.task, make_ctx(), {"any": list(S.chain)})
    check("term_reason=cancelled", b.term_reason == "cancelled_by_user")
    check("turn_count=0", b.turn_count == 0)
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径3: executor 失败, 无 fallback → abort ──")
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
    S.escalate_to = None
    S.dispatch_queue = [("fail", FakeExec(success=False))]
    S.validate_queue = []
    b = run_case()
    check("action=abort", b.validation.action == "abort")
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径4 (关键 P1-1): dispatch 抛异常 → worktree 必被清理 ──")
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
    S.dispatch_queue = [("raise",)]
    S.validate_queue = []
    raised = False
    try:
        run_case()
    except RuntimeError:
        raised = True
    check("异常向上传播 (交 orchestrator fut.result 兜底)", raised)
    check("worktree 仍被清理 (无泄漏)", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径5: planner 分解 → planner_decomposed ──")
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2, "mode": "planner"}]
    plan = '方案\n```json\n[{"desc":"子任务A","suggested_level":"any"}]\n```'
    S.dispatch_queue = [("ok", FakeExec(success=True, raw_output=plan))]
    S.validate_queue = []
    b = run_case()
    check("planner_decomposed=True", b.planner_decomposed is True)
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径6: 中置信 retry → 复用同一 wt, 下一轮 pass ──")
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 3}]
    S.dispatch_queue = [("ok", FakeExec(success=True)), ("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="retry", confidence=0.5), FakeVal(action="pass")]
    b = run_case()
    check("最终 pass", b.validation.action == "pass")
    check("retry 不重建 wt (只建1个)", len(CREATED) == 1, f"建了{len(CREATED)}个")
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径7: 低置信 cascade_skip → 升级 (escalate=None 则无路可升) ──")
    # 真实行为: cascade_skip break 后走 escalate; escalate=None → 终态如实报
    # `no_escalation_path`。**这条断言 2026-09-11 才改对**：原来断言的是
    # `escalation_exhausted`，而 `_exec.py` 早就不写那个词了 —— 它故意说
    # "没有升级路径"而不是"升级用尽"（两档制合并成单档后根本没有下一档，
    # 假装"用尽"是撒谎）。字符串对不上，于是这条一直红着，谁也没注意。
    # m2 不在同层被启用 (代码用升级换 agent, 非同层 fallback)。建1清1。
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2},
               {"model": "m2", "sandbox": "worktree", "max_turns": 2}]
    S.escalate_to = None
    S.dispatch_queue = [("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="retry", confidence=0.2)]
    b = run_case()
    check("term_reason 含 no_escalation_path", "no_escalation_path" in b.term_reason, b.term_reason)
    check("cascade_skip 后只建1个 wt (升级换 agent 非同层)", len(CREATED) == 1, f"建了{len(CREATED)}个")
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径8: v2 merge 冲突 → abort ──")
    reset_wt()
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
    S.dispatch_queue = [("ok", FakeExec(success=True))]
    S.validate_queue = []   # merge 冲突在 validate 之前 return, 不会调 validate
    b = run_case(v3=False)  # v2: merge_queue=None → 走 wt_merge_back
    check("term_reason 含 merge_conflict", "merge_conflict" in b.term_reason, b.term_reason)
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径9: QA 门禁 — 硬失败不合并 (A/B 对照) ──")
    S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
    S.escalate_to = None

    reset_wt()
    S.qa_verdict = "pass"
    S.dispatch_queue = [("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="pass")]
    b_qa_pass = run_case(v3=True)
    check("QA pass → 保留 merge_request", b_qa_pass.merge_request == "FAKE_MR",
          f"实际 {b_qa_pass.merge_request!r}")

    reset_wt()
    S.qa_verdict = "fail"
    S.dispatch_queue = [("ok", FakeExec(success=True)), ("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="pass"), FakeVal(action="pass")]
    b_qa_fail = run_case(v3=True)
    check("QA fail → merge_request 被清空", b_qa_fail.merge_request is None,
          f"实际 {b_qa_fail.merge_request!r}")
    check("QA fail → ok=False", b_qa_fail.ok is False, f"实际 {b_qa_fail.ok}")
    check("QA fail → 记入 unverified",
          any("QA 硬证据失败" in u for u in b_qa_fail.validation.unverified),
          str(b_qa_fail.validation.unverified))
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")

    print("── 路径10: QA 软信号 escalate → 不拦合并 ──")
    reset_wt()
    S.qa_verdict = "escalate"
    S.dispatch_queue = [("ok", FakeExec(success=True))]
    S.validate_queue = [FakeVal(action="pass")]
    b_qa_esc = run_case(v3=True)
    check("QA escalate → 仍保留 merge_request", b_qa_esc.merge_request == "FAKE_MR",
          f"实际 {b_qa_esc.merge_request!r}")
    check("worktree 对称", sorted(CREATED) == sorted(CLEANED), f"建{CREATED} 清{CLEANED}")
    S.qa_verdict = "pass"   # 复位

    # ── 11/12: 「阶段 → 模型」的接线 ──
    # **必须把 QIDIAN_DIR 指到临时目录**：这个脚本是独立跑的、没有 tests/conftest.py
    # 那个 _isolate_qidian_dir，不指的话会读到**用户真实的** .qidian/phase_models.json
    # —— 结果就随用户配没配而变（2026-09-11 真踩了：用户配完，这两条立刻红）。
    from singularity.scheduler import phase_models
    _old_qdir = _exec.config.QIDIAN_DIR

    def _run_with_phase_config(cfg: dict):
        """在隔离目录里写 cfg 跑一遍，返回 (建链处 kwargs, dispatch 处 kwargs)，跑完还原。"""
        _exec.config.QIDIAN_DIR = Path(tempfile.mkdtemp())
        try:
            if cfg:
                phase_models.save(cfg)
            reset_wt()
            S.chain = [{"model": "m1", "sandbox": "worktree", "max_turns": 2}]
            S.last_chain_kw = S.last_dispatch_kw = None
            S.dispatch_queue = [("ok", FakeExec(success=True))]
            S.validate_queue = [FakeVal(action="pass")]
            run_case()
            return S.last_chain_kw, S.last_dispatch_kw
        finally:
            _exec.config.QIDIAN_DIR = _old_qdir

    print("── 路径11: 实现阶段没配 → 两处都收到 (None, False) ──")
    # 「阶段 → 模型」没配置时必须与配置前逐字节一致。
    chain_kw, disp_kw = _run_with_phase_config({})
    check("建链处 lineup=None", chain_kw.get("project_lineup") is None, str(chain_kw))
    check("建链处 restrict=False", chain_kw.get("restrict_to_lineup") is False, str(chain_kw))
    check("dispatch 处 lineup=None", disp_kw.get("project_lineup") is None, str(disp_kw))
    check("dispatch 处 restrict=False", disp_kw.get("restrict_to_lineup") is False, str(disp_kw))

    print("── 路径12: 实现阶段配了名单 → **两处都**收到 ──")
    # 这是本仓库最容易只改一半的地方：建链处和 dispatch 处是两次独立选链，
    # 只传一处 = 外层按 A 建 worktree、内层实际调 B。
    chain_kw, disp_kw = _run_with_phase_config({"executing": ["m1"]})
    check("建链处 restrict=True", chain_kw.get("restrict_to_lineup") is True, str(chain_kw))
    check("建链处 lineup 是那份名单", chain_kw.get("project_lineup") == {"any": ["m1"]}, str(chain_kw))
    check("dispatch 处 restrict=True", disp_kw.get("restrict_to_lineup") is True, str(disp_kw))
    check("dispatch 处 lineup 是那份名单", disp_kw.get("project_lineup") == {"any": ["m1"]}, str(disp_kw))

    print("\n" + "=" * 48)
    total = PASS + FAIL
    print(f"{'✅ 全通过!' if FAIL == 0 else '❌ 有失败'}  通过 {PASS} / 失败 {FAIL} / 总 {total}")
    print("=" * 48)
    sys.exit(0 if FAIL == 0 else 1)
