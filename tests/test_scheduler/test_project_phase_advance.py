"""项目阶段推进：`executing` 下的任务全到终态之后，往哪走。

`FAILED` 和 `DONE` 在推进判据里**同属"终态"** —— 所以必须先分一次"有没有成功的"。
不分的话，7 个任务全失败的项目会一路推到 DONE 并播报"交付完成!"，
用户看到的和事实完全相反（2026-09-11 流水线探针实测）。
"""
import json

import pytest

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler import tracker


def _mk_task(tmp_path, tid: str, status: tracker.TaskStatus) -> None:
    t = tracker.Task(id=tid, description=f"任务 {tid}")
    t.status = status
    (tmp_path / "tasks").mkdir(exist_ok=True)
    (tmp_path / "tasks" / f"{tid}.json").write_text(
        json.dumps(t.to_dict() if hasattr(t, "to_dict") else t.__dict__, default=str),
        encoding="utf-8")


def _setup(tmp_path, monkeypatch, statuses: list[tracker.TaskStatus]):
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.EXECUTING
    for i, st in enumerate(statuses):
        tid = f"t{i}"
        _mk_task(tmp_path, tid, st)
        p.task_ids.append(tid)
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    return p


def test_all_tasks_failed_does_not_advance(tmp_path, monkeypatch):
    """一个都没成功 → 不许推进到交付，并记一条 issue。

    旧代码在这里直接推进（FAILED 也算终态），项目最后停在 done —— 就是探针看到的
    "终态 done、7 个任务全 failed"。
    """
    p = _setup(tmp_path, monkeypatch,
               [tracker.TaskStatus.FAILED] * 3)
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.EXECUTING, "全失败还推进 = 会谎报交付完成"
    assert any(i.get("kind") == "all_tasks_failed" for i in p.issues)


def test_issue_recorded_only_once(tmp_path, monkeypatch):
    """调度循环每 tick 都会走到这里 —— issue 只能记一条，否则刷屏。"""
    p = _setup(tmp_path, monkeypatch, [tracker.TaskStatus.FAILED] * 2)
    for _ in range(5):
        orch._auto_trigger_test_fix({}, [])
    assert len([i for i in p.issues if i.get("kind") == "all_tasks_failed"]) == 1


def test_partial_success_still_advances(tmp_path, monkeypatch):
    """有任务成功就该交付 —— 失败的不能连累整个项目卡死。"""
    p = _setup(tmp_path, monkeypatch,
               [tracker.TaskStatus.DONE, tracker.TaskStatus.FAILED])
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.INTEGRATING


def test_all_done_advances(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch, [tracker.TaskStatus.DONE] * 2)
    orch._auto_trigger_test_fix({}, [])
    assert p.phase is proj_mod.Phase.INTEGRATING


def test_decompose_fallback_reads_project_architecture(tmp_path, monkeypatch):
    """兜底拆解必须读 `proj.architecture`。

    它以前读 `<项目目录>/architecture.json` —— **全仓没有任何代码写这个文件**，
    所以永远卡在第一步 `if not arch_path.exists(): return`，一次都没救成功过。
    真进入"executing 且没任务"的项目只能一直卡着（2026-09-11 真流水线实测：
    卡 13 分钟、零日志）。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: tmp_path)

    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.architecture = {"tasks": [{"id": "T1", "title": "实现 X",
                                 "description": "创建 x.py", "layer": "impl"}]}
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)

    orch._decompose_and_create_tasks(p, {})
    assert len(p.task_ids) == 1, "架构里有任务却没建出来 = 兜底还在读那个没人写的文件"


def test_decompose_fallback_writes_constraints_checklist(tmp_path, monkeypatch):
    """一条建任务的路，必须自己把 `constraints_checklist` 写上。

    清单的唯一写点在 `_run_execution`，而这条路**恰好不跑它**：批准 GATE2 时
    `project_gate_confirm` 只启 planning、不启 executing（executing 归调度循环推），
    于是建任务的是 `_decompose_and_create_tasks` —— 而它以前只建任务、不写清单。
    后果（2026-09-12 真机 · 项目 1789223754637 实测）：清单恒空 ⇒
    `_run_verification` 进门第一句就早退 ⇒ **机械检查一条都跑不了**（§60）。

    它的 docstring 当时写着"正常路径用不到它" —— 那句前提是假的，这就是守卫。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(proj_mod, "ensure_repo", lambda _id: tmp_path)

    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    cons = [{"type": "test", "rule": "pytest 全绿",
             "check": {"argv": ["python3", "-m", "pytest", "-q"], "expect_exit": 0}}]
    p.architecture = {"constraints": cons,
                      "tasks": [{"id": "T1", "title": "实现 X",
                                 "description": "创建 x.py", "layer": "impl"}]}
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)

    orch._decompose_and_create_tasks(p, {})
    assert p.constraints_checklist == cons, (
        "这条路建了任务却没写约束清单 ⇒ 验收时机械检查整段跳过（§60）")


def test_no_decomposable_tasks_is_surfaced_not_silently_stuck(tmp_path, monkeypatch):
    """架构拆不出任务 → 必须留痕，不能无声卡死。

    实测（2026-09-11 真流水线）：融合失败（模型欠费）→ 通用合成 → 产物不是合法
    架构 JSON（`{"parse_error": true}`）→ decompose 得 0 个任务 → 项目停在
    executing：没任务可跑、推进判据要求 task_ids 非空所以两条分支都不进
    —— **卡了 13 分钟，日志一行都没有**。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    p.phase = proj_mod.Phase.EXECUTING           # 一个任务都没有
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    monkeypatch.setattr(orch, "_decompose_and_create_tasks", lambda _p, _a: None)

    for _ in range(3):
        orch._auto_trigger_test_fix({}, [])

    assert any(i.get("kind") == "no_decomposable_tasks" for i in p.issues)
    assert len([i for i in p.issues if i.get("kind") == "no_decomposable_tasks"]) == 1


# ── GATE3 入门票（2026-09-11 外派评审）────────────────────

def _mk_proj():
    return proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )


class TestGate3Admission:
    """进 GATE3 必须带验收结论，否则补一条记录 + 告警。

    REVIEWING 被两套驱动同时认识但行为不同：orchestrator 跑
    `run_test_fix_loop`（真跑 QA+安全审计），`run_phase` 的 REVIEWING 分支
    直接跳。异步验收线程炸了 / 用户手快先点了 / auto_mode 且循环没开 ——
    三条路的结局都是"验收整段没跑、零记录"，人审时看不出来。
    """

    def test_no_evidence_adds_ticket(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        warns = []
        import singularity.scheduler.witness as w
        monkeypatch.setattr(w, "warn", lambda *a, **k: warns.append(a))

        p = _mk_proj()
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "手点下一步")

        assert any(i.get("type") == "gate3_no_evidence" for i in p.issues), p.issues
        assert any("gate3_no_evidence" in str(x) for x in warns)
        assert p.phase == proj_mod.Phase.GATE3, "只补票，不阻断"

    def test_ran_marker_clears_the_ticket(self, tmp_path, monkeypatch):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.issues = [{"type": "verification_ran", "detail": "QA + 安全审计已执行"}]
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "验收完成")
        assert not any(i.get("type") == "gate3_no_evidence" for i in p.issues)

    def test_skipped_marker_also_counts_as_evidence(self, tmp_path, monkeypatch):
        """`verification_skipped` 是"有结论"——结论就是没跑。不该再补一张票。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.issues = [{"type": "verification_skipped", "detail": "架构没产出约束清单"}]
        p.phase = proj_mod.Phase.REVIEWING
        p.set_phase(proj_mod.Phase.GATE3, "跳过")
        assert not any(i.get("type") == "gate3_no_evidence" for i in p.issues)

    def test_other_phases_unaffected(self, tmp_path, monkeypatch):
        """门票只管 GATE3 —— 别的流转不该被它碰。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        p = _mk_proj()
        p.phase = proj_mod.Phase.GATE2
        p.set_phase(proj_mod.Phase.EXECUTING, "架构通过")
        assert p.issues == []


def test_merge_submit_failure_does_not_strand_project(tmp_path, monkeypatch):
    """`submit` 抛了 → 占位必须**撤回**，否则这个项目**再也不会被合并**。

    `_merge_inflight` 是"该项目正在合并"的防重入标记，而**清理写在后台函数
    `_run_integration_merge_async` 的 `finally` 里** —— `submit` 一抛，那个函数
    根本没起来 ⇒ 标记永远挂着 ⇒ 两处调用点的 `if proj.id not in _merge_inflight`
    恒为假 ⇒ 集成合并不再被派发，项目**无声卡在 integrating**。
    （`submit` 会抛不是猜的：`_merge_executor` 上面那句注释写的就是它。）

    这条钉的是**接线**：把 `_submit_integration_merge` 里的 `discard` 删掉会红。
    """
    p = _setup(tmp_path, monkeypatch, [tracker.TaskStatus.DONE])
    p.phase = proj_mod.Phase.INTEGRATING
    orch._merge_inflight.discard(p.id)

    class _Boom:
        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(orch, "_get_merge_executor", lambda: _Boom())
    warns: list[str] = []
    monkeypatch.setattr(orch.witness, "warn", lambda scope, msg, **kw: warns.append(msg))

    try:
        orch._auto_trigger_test_fix({}, [])
        assert p.id not in orch._merge_inflight, \
            "submit 失败后占位没撤 ⇒ 这个项目再也不会被合并（无声卡死）"
        assert any("merge_submit_failed" in w for w in warns), f"没出声，只剩静默: {warns}"
    finally:
        orch._merge_inflight.discard(p.id)   # 别把这个 id 漏给后面的用例


def test_delivery_no_code_ref_仍然算成功但必须出声(tmp_path, monkeypatch):
    """打 tag 和取 HEAD **双双失败**时：仍算交付成功，但**必须出声**。

    `_run_delivery` 结尾是**无条件** `return True`，`code_ref` 落成 `"unknown"` 也照样
    推 DONE + 账本记 `delivery: ok` —— 于是"这次交付归档的是哪个 commit"这件事
    原来只存在于 detail 串里，界面上和正常交付长得一模一样。
    真机上还没触发过（8/8 都是真 `release/*` tag），但触发的那一刻正是它最要紧的时候。

    ⚠️ 故意**不改判成失败**：tag 打不上 ≠ 代码没交付，判失败会把好项目卡死在 delivering。
    变异验证：删掉那句 `witness.warn` → 红。
    """
    import subprocess

    from singularity.scheduler import project as proj_mod

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _id: root)

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def _fake_run(cmd, **kw):
        if kw.get("check"):
            raise subprocess.CalledProcessError(1, cmd)
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)     # 所有 git 调用都失败

    p = proj_mod.ProjectState(
        id="p_deliver", name="测试交付", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    warns: list[str] = []
    monkeypatch.setattr(orch.witness, "warn", lambda scope, msg, **kw: warns.append(msg))

    ok, detail = orch._run_delivery(p)

    assert ok is True, "tag 打不上 ≠ 代码没交付 —— 判失败会把好项目卡死在 delivering"
    assert "unknown" in detail, f"detail 里该如实写出来: {detail}"
    assert any("delivery_no_code_ref" in w for w in warns), f"没出声，只剩 detail 串: {warns}"


def test_交付清单的报告栏不该永远空(tmp_path, monkeypatch):
    """报告在 `.qidian/projects/<id>.<名字>` 里，**不在项目仓库里**。

    真机（2026-09-15）：用户点完 GATE3 通过问"交付的东西呢" —— 代码交付是好的
    （`release/<id>-…` tag 真打上了），但交付清单三项全空，其中 `reports` 是
    **永远空**：收报告时用的是 `_Path(root) / "qa_report.json"`，而 `root` 是
    **项目仓库**（`_repo_dir(proj.id)`），报告全在 `.qidian/projects/` 下、
    名字还带 `<id>.` 前缀。⇒ 一栏永远空，界面上却跟"正常交付"长得一模一样。

    ⚠️ 报告**只放在 `.qidian/projects/`**，项目仓库里一份都不放 ——
    这样"改回 `_Path(root)`"必然红（放一份在仓库里会让旧代码也过，
    那条断言就白钉了）。
    变异验证：`_pop(proj.id, fname)` 改回 `_Path(root) / fname` → 红。
    """
    import subprocess

    from singularity.scheduler import config
    from singularity.scheduler import project as proj_mod

    pid = "p_reports"
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _id: root)

    # 报告落在**项目数据目录**，名字带 `<id>.` 前缀
    # ⚠️ 这里直接拼 `config.QIDIAN_DIR` 而不是调 `_phase_output_path()` ——
    # 把"约定"本身钉死，免得跟被测代码用同一个函数、一起错。
    pdir = config.QIDIAN_DIR / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    for name in ("qa_report.json", "qa-report.md", "security-report.md", "machine-checks.json"):
        (pdir / f"{pid}.{name}").write_text("{}", encoding="utf-8")
    # 项目状态文件也在同一个目录 —— 它**不是**报告，不许被收进去
    (pdir / f"{pid}.json").write_text("{}", encoding="utf-8")

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _R())

    p = proj_mod.ProjectState(
        id=pid, name="测试报告归档", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )

    ok, detail = orch._run_delivery(p)

    assert ok is True
    manifest = config.QIDIAN_DIR / "deliverables" / pid / "delivery_manifest.json"
    reports = json.loads(manifest.read_text(encoding="utf-8"))["reports"]
    assert reports, f"报告栏不该是空的（那就是原 bug）: {reports}"
    assert "qa-report.md" in reports and "security-report.md" in reports, reports
    assert f"{pid}.json" not in reports, "项目状态文件不是报告，别扫进来"
    assert "报告=" in detail, f"报告数该进 detail，否则日志里看不出报告栏空没空: {detail}"
