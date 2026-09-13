"""第三批修复（2026-09-14 深夜）—— 四条，全部来自"复核我的核实"。

出处：`~/Desktop/ZCode审阅/复核核实-01.md`（独立复核）＋ `D`/`E`/`E'` 三轮外派
撞上的同几处。每条都在**当前树**上重读过、不是照抄报告。

  ① 人审门只修了一半：「不同意」仍被判批准（否定词没管，「不通过」只是特例）
  ② GATE3 的 `verification_ran` 标记**无条件**撒 —— QA 报告没落盘也照样撒
  ③ 取消的任务**不早退** ⇒ `_check_cancelled` 已把标记删了，重试那轮查不到东西，
     任务照跑满 N 轮；`finalize` 里还有"重试耗尽 → 自动拆分"那条路会在
     用户已经叫停之后再派一批子任务出去
  ④ `post_execution_hook` 抛异常被写死成 `{"failure_kind": "ok"}` —— 钩子炸了不留痕

这些测试都**钉接线**：删掉对应的那一行判据，测试必须红。
"""
import pytest

from tests.test_scheduler.test_exec_internals import TestFinalizeResult


# ═══════════════════════════════════════════════════════════════
# ① 人审门：否定词 + 疑问句
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("q", ["不同意", "不同意这个方案", "不确认", "不继续", "不太同意"])
def test_否定_加_批准词_一律打回(q):
    """「不」+ **任何**批准词都算打回。

    上一轮的修法只把「不通过」加进打回词表 —— 而子串匹配下「不同意」里含
    「同意」，于是人在门里说"不同意"，结果是**批准并推进阶段**。
    """
    from singularity.scheduler._observer_answer import _is_gate_reply
    assert _is_gate_reply(q) == "rejected", f"{q!r} 又被判成批准了 —— 门等于不存在"


@pytest.mark.parametrize("q", ["行不行", "行不行？", "可以吗", "可以吗？", "好吗？", "是吗"])
def test_疑问句不许开门(q):
    """短的疑问句不能当批准。

    原来短词用 `startswith` 判，而「行不行」正好 3 个字、没超长度上限、
    又以批准短词「行」开头 ⇒ 一句"行不行？"就把门放过去了。
    """
    from singularity.scheduler._observer_answer import _is_gate_reply
    assert _is_gate_reply(q) is None, f"{q!r} 被判成批准 —— 问一句就过门"


@pytest.mark.parametrize("q", ["好", "好的！", "可以", "ok", "行吧", "通过", "我同意这个方案"])
def test_真批准还是批准(q):
    """对照：别把门修成"什么都不放行"。"""
    from singularity.scheduler._observer_answer import _is_gate_reply
    assert _is_gate_reply(q) == "approved"


@pytest.mark.parametrize("q", ["不错的方案", "nothing works", "这个方案不行吗"])
def test_不能误伤(q):
    """前缀匹配的假阳性：「不错的方案」是夸、「nothing works」是抱怨，都不是门回复。

    这条钉的是**别把闭表改成前缀匹配** —— 那样两条都会被误判成打回。
    """
    from singularity.scheduler._observer_answer import _is_gate_reply
    assert _is_gate_reply(q) is None


# ═══════════════════════════════════════════════════════════════
# ② GATE3：`verification_ran` 必须跟 QA 报告对账
# ═══════════════════════════════════════════════════════════════

def _mk_project(tmp_path, monkeypatch):
    from singularity.scheduler import project as proj_mod
    monkeypatch.setattr(proj_mod.config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="g3", name="GATE3对账", raw_constraints=[], owner_confirm={},
        task_ids=[], issues=[], supervision_log=[], lineage=[],
        handoffs=[], agent_lineup={})
    p.constraints_checklist = [{"type": "security", "rule": "r0",
                                "check": {"argv": ["pytest"], "expect_exit": 0}, "covers": [0]}]
    proj_mod.save(p)
    return p


def _quiet_flags(monkeypatch):
    from singularity.scheduler import workflow
    monkeypatch.setattr(workflow, "_flag_degraded_tasks", lambda p: None)
    monkeypatch.setattr(workflow, "_flag_file_overlap", lambda p: None)
    monkeypatch.setattr(workflow, "_flag_missing_qa_verdict", lambda p: None)


def test_QA报告没落盘就不许撒验证标记(tmp_path, monkeypatch):
    """**正题**：报告写失败 ⇒ 不能记"验收跑过"，得让 `_gate3_admission` 去报缺证据。

    原来那条 `except Exception: pass` 一吞，标记照样撒 ⇒ 进 GATE3 零告警，
    人审页上"验收整段没产出"和"验收通过了"长得一模一样（D/E 两轮独立撞上）。
    """
    from singularity.scheduler import workflow
    p = _mk_project(tmp_path, monkeypatch)
    _quiet_flags(monkeypatch)

    def boom(*a, **k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(workflow, "_save_phase_output", boom)
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass    # 后面还依赖一堆东西，这里只关心"标记撒没撒"

    assert not [i for i in p.issues if i.get("type") == "verification_ran"], \
        "QA 报告根本没写出来，却记了「验收跑过」—— GATE3 的门对这条完全瞎"
    assert p.has_verification_evidence() is False


def test_QA报告落了盘才撒验证标记(tmp_path, monkeypatch):
    """对照：正常路径必须照撒（不然 `_gate3_admission` 会对每次正常验收都误报）。"""
    from singularity.scheduler import workflow
    p = _mk_project(tmp_path, monkeypatch)
    _quiet_flags(monkeypatch)
    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass
    assert [i for i in p.issues if i.get("type") == "verification_ran"], \
        f"正常跑完却没撒标记，GATE3 会误报缺证据：{p.issues}"
    assert p.has_verification_evidence() is True


# ═══════════════════════════════════════════════════════════════
# ③ 人工取消：不重试、不拆分、不跑 QA
# ═══════════════════════════════════════════════════════════════

def test_取消的批次不许重试(monkeypatch):
    """`_run_with_retry` 必须把 `cancelled_by_user` 当终态。

    不早退的后果：`_check_cancelled` 命中时**已经把标记文件删了**，
    于是重试那一轮再查标记 = 查不到 ⇒ 任务照常跑满 `max_retries` 轮，
    用户点了取消，token 继续烧。
    """
    from types import SimpleNamespace as NS
    from singularity.scheduler import _exec

    calls = []

    def fake_run(task, ctx, agents):
        calls.append(1)
        return NS(ok=False, planner_decomposed=False,
                  term_reason="cancelled_by_user", dispatch_result=None)

    monkeypatch.setattr(_exec, "run", fake_run)
    task = NS(id="t_cancel", max_retries=3, description="x")
    ctx = NS(retry_count=0, merge_queue=None, snapshot_ref="", batch_id="b1")

    out = _exec._run_with_retry(task, ctx, agents={})

    assert len(calls) == 1, f"取消之后又跑了 {len(calls) - 1} 轮 —— 标记早被删了，重试查不到"
    assert out.term_reason.startswith("cancelled_by_user")


class Test取消后的收尾(TestFinalizeResult):
    """`finalize` 里取消必须是**独立分支**，不能落进"重试耗尽 → 自动拆分"。"""

    def test_取消的任务不许再拆分子任务(self, monkeypatch):
        """重试已耗尽的任务若走原路径，会 `decompose()` 再派一批子任务 ——
        用户已经叫停了，这批孩子会继续烧钱。"""
        from singularity.scheduler._types import _pending_sse_events
        _pending_sse_events.clear()

        decomposed = []

        def rec_decompose(desc):
            decomposed.append(desc)
            return [{"desc": "a"}, {"desc": "b"}]

        transitions = []

        def rec_transition(tid, status, **kw):
            transitions.append((tid, getattr(status, "name", str(status)), kw))

        reason, _results, _ = self._call(
            monkeypatch,
            # retry_count 已达上限 ⇒ 老代码正好落进"自动拆分"那条路
            task=self._make_task(retry_count=3, max_retries=3, depth=0),
            batch=self._make_batch(
                ok=False, term_reason="cancelled_by_user",
                validation=type("V", (), {"action": "abort", "verdict": "阻断", "evidence": {}})(),
            ),
            **{"decompose": rec_decompose, "tracker.transition": rec_transition},
        )

        assert decomposed == [], "用户已经取消，任务还被拆成子任务继续派活"
        assert any(s == "FAILED" for _, s, _ in transitions), transitions
        assert "cancel" in reason.lower(), reason


# ═══════════════════════════════════════════════════════════════
# ④ 质量钩子自己炸了，不许伪装成"没问题"
# ═══════════════════════════════════════════════════════════════

def test_质量钩子异常不许报_ok(monkeypatch):
    """D 核外派点名叫它"编造的 0.5"。

    原代码在 `except` 里写死 `{"warnings": [], "failure_kind": "ok", "confidence": 0.5}`；
    下游 `_exec.py` 按 `failure_kind != "ok"` 决定要不要给模型加失败反馈 ⇒
    钩子崩溃在整条链路上不留痕。
    """
    import ast
    import inspect
    from singularity.scheduler import _exec
    src = inspect.getsource(_exec.run)
    # 用 AST 找**真的字典字面量** —— 直接搜字符串会把注释里复述旧代码的那句也算上。
    bad = [n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Dict)
           and any(isinstance(k, ast.Constant) and k.value == "failure_kind" for k in n.keys)
           and any(isinstance(v, ast.Constant) and v.value == "ok" for v in n.values)]
    assert not bad, "except 分支里又把钩子异常写成 failure_kind=ok 了（行号见上）"
    assert "post_exec_hook_failed" in src, "钩子炸了却没告警"
