"""静默失败的四条不变量 —— 锁住"下次不许再犯"。

来自 2026-09-11 一天之内挖出的六个案例（见 `docs/防御模式.md` #44~46）。
共同形状：**系统没做或做错了，但从外面看一切正常** —— 六个缺陷没有一个报错。

**刻意测不变量，而不是测路径。** 路径测试（monkeypatch 某个函数再看它被调没被调）
在这套代码里结构上抓不到 P0：上一轮审计的 P0-1（门禁取裸 `git diff` 恒空）就是
门禁"在跑、在记"，但证据源本身是坏的 —— 路径测试全绿。
"""

import dataclasses
import json

import pytest

from singularity.scheduler import project as P
from singularity.scheduler import witness
from singularity.scheduler.project import Phase


class TestFieldCoverage:
    """① 序列化字段全覆盖 —— 手抄清单漏一个键就是一次静默失败。"""

    def test_to_dict_covers_every_dataclass_field(self):
        d = P.ProjectState(id="x", name="y").to_dict()
        names = {f.name for f in dataclasses.fields(P.ProjectState)}
        missing = names - set(d)
        assert not missing, (
            f"to_dict 漏了字段 {sorted(missing)} —— 前端拿到 undefined 会当成「没有」，"
            f"全程不报错。GATE3 的 qa_report 就是这么丢的。")

    def test_roundtrip_preserves_everything(self):
        """往返之后**每个字段的值**都得跟原对象一样（不是拿 `to_dict` 跟它自己比）。

        ⚠️ 原来断言的是 `from_dict(p.to_dict()).to_dict() == p.to_dict()` —— 两边都过
        **同一个 `to_dict`** ⇒ 只要"序列化两半用同一张表"就恒真。
        2026-09-14 变异复核坐实：让 `to_dict` **漏掉 `issues`**，这条**照样绿**
        （只有隔壁那条扫描式用例红）。名字说"往返不丢东西"，就得真拿原对象的值比。
        """
        p = P.ProjectState(id="x", name="y", phase=Phase.EXECUTING,
                           issues=[{"type": "t"}], review_failures=2,
                           task_ids=["a"], owner_confirm={"gate2": "approved"},
                           lineage=[{"action": "phase"}])
        again = P.ProjectState.from_dict(p.to_dict())
        for f in dataclasses.fields(P.ProjectState):
            assert getattr(again, f.name) == getattr(p, f.name), (
                f"往返把 {f.name} 弄丢了："
                f"{getattr(p, f.name)!r} → {getattr(again, f.name)!r}")


class TestGate3LeavesEvidence:
    """② 进 GATE3 必须有验收结论 —— 跳过也要留痕，不能什么都不说。"""

    def _fresh(self):
        pid = P.create(name="t", template="feature", description="x").id
        p = P.load(pid)
        p.constraints_checklist = []          # 架构没产出约束清单 → 验收会走跳过分支
        p.issues = [{"type": "stale"}]
        P.save(p)
        return pid

    def test_skipped_verification_is_recorded(self):
        from singularity.scheduler import workflow
        pid = self._fresh()
        workflow.run_test_fix_loop(P.load(pid), {})

        after = P.load(pid)
        assert after.phase == Phase.GATE3
        kinds = [i.get("type") for i in after.issues]
        assert "verification_skipped" in kinds, \
            "验收被跳过却没留痕 —— GATE3 只能看到一个空 issues 和一份不存在的报告"

    def test_stale_issues_cleared_but_not_the_new_one(self):
        """清空必须发生在验收**之前**，否则会把刚记的擦掉（这就是原来的 bug）。"""
        from singularity.scheduler import workflow
        pid = self._fresh()
        workflow.run_test_fix_loop(P.load(pid), {})

        kinds = [i.get("type") for i in P.load(pid).issues]
        assert "stale" not in kinds, "上一轮的 issues 该清掉"
        assert "verification_skipped" in kinds


class TestQaVerdictIsNotFailOpen:
    """②·五 「QA 没产出结论」不许等于「QA 说没问题」。

    2026-09-13 真机（项目 `1789300044340`）：`qa_report.json` 是
    `{total_checks: 0, passed: 0, failed: 0, verdict: "go"}` —— **一条检查没跑、结论"放行"**；
    而同轮 `qa-report.md` 里存的是一段**没解析的 `<tool_call>` 原文**（模型想跑 pytest）。

    原因就一行：`qa_data.get("verdict", "go" if not issues else "no_go")` ——
    输出不是 JSON 时 `qa_data` 是 `{}`、`issues` 也是 `[]`，**默认值正好落到 `go`**。
    """

    MALFORMED = [
        ("", "完全空"),
        ("{}", "空对象"),
        ('{"passed": [], "issues": []}', "有结构但没结论"),
        ("<tool_call>run_command<arg_key>command</arg_key>"
         "<arg_value>python3 -m pytest -q</arg_value></tool_call>", "输出成了工具调用（真机现场）"),
        ("这不是 JSON", "根本不是 JSON"),
        ("[1, 2, 3]", "是 JSON，但是数组不是对象"),
    ]

    @pytest.mark.parametrize("raw,why", MALFORMED)
    def test_malformed_never_becomes_go(self, raw, why):
        from singularity.scheduler import workflow as W
        _, verdict, reason = W._qa_verdict_from_raw(raw)
        assert verdict != "go", f"【{why}】被当成了放行 —— 空结论不能算通过"
        assert verdict == W._QA_VERDICT_MISSING
        assert reason, "得说清它为什么没结论"

    def test_real_go_is_still_go(self):
        """真给了结论的照旧放行 —— 别顺手把正常路径卡死。"""
        from singularity.scheduler import workflow as W
        _, verdict, _ = W._qa_verdict_from_raw(
            '{"verdict": "go", "summary": {"total_checks": 3}}')
        assert verdict == "go"

    def test_issues_without_verdict_still_no_go(self):
        """报了问题却没给结论 ⇒ 按不通过（老的 fail-closed 分支，保留）。"""
        from singularity.scheduler import workflow as W
        _, verdict, _ = W._qa_verdict_from_raw('{"issues": [{"description": "挂了"}]}')
        assert verdict == "no_go"

    def test_flag_leaves_both_traces(self):
        """判据为真时**两件必做事**都得做：进 issues + 出声。

        ⚠️ 只测 `_qa_verdict_from_raw` 验的是"**判据对**"，验不到"**判据为真时真的有人记**"。
        这两件事分开 —— 2026-09-13 一天被这个形状咬过三次（见 `docs/防御模式.md` §65）。
        """
        from singularity.scheduler import workflow as W
        from singularity.scheduler import config

        p = P.ProjectState(id="qa1", name="t")
        p.issues = []
        W._flag_missing_qa_verdict(p)

        assert "qa_verdict_missing" in [i.get("type") for i in p.issues], \
            "没进 issues ⇒ GATE3 人审页上看不见（那份报告是给人看的）"

        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists(), "没出声 ⇒ 聚合视图里也看不见"
        assert "qa_verdict_missing" in log.read_text(encoding="utf-8")


class TestArchTasksMustBeOrdered:
    """⑥ 架构拆了多个任务却**一条依赖都没排** —— 要出声。

    2026-09-13 真机（项目 `1789300044340`）：架构把"实现"和"写测试"两个任务都留成
    `depends_on: []` ⇒ 调度循环**同时派发**。写测试的等不来实现，**自己把实现写了**。
    实现任务 900s 超时失败、零提交；写测试那笔提交里**同时带着实现和测试**
    ⇒ 机械检查 9/9 全过，但它证明的只是"**它跟自己一致**"。
    """

    def test_two_tasks_zero_deps_is_flagged(self):
        from singularity.scheduler import workflow as W
        assert W._arch_tasks_are_unordered(
            {"tasks": [{"id": "T1", "depends_on": []}, {"id": "T2", "depends_on": []}]})

    def test_any_dependency_silences_it(self):
        """只要有一条排了先后就不报 —— 判据是"**一条都没有**"，不是"排得不全"。"""
        from singularity.scheduler import workflow as W
        assert not W._arch_tasks_are_unordered(
            {"tasks": [{"id": "T1"}, {"id": "T2", "depends_on": ["T1"]}]})

    def test_depends_on_local_id_counts_too(self):
        """执行器那条路用的是 `depends_on_local_id` —— 认它，别只看 `depends_on`。"""
        from singularity.scheduler import workflow as W
        assert not W._arch_tasks_are_unordered(
            {"tasks": [{"id": "a"}, {"id": "b", "depends_on_local_id": [0]}]})

    def test_single_task_or_empty_never_flagged(self):
        from singularity.scheduler import workflow as W
        for arch in ({}, {"tasks": []}, {"tasks": [{"id": "T1"}]}, None, "不是 dict", {"tasks": "?"}):
            assert not W._arch_tasks_are_unordered(arch), f"{arch!r} 不该报"

    def test_flag_leaves_both_traces(self):
        """判据为真时**两件必做事**都得做：进 issues + 出声（同 QA 那条理由）。"""
        from singularity.scheduler import workflow as W
        from singularity.scheduler import config

        p = P.ProjectState(id="ar1", name="t")
        p.issues = []
        W._flag_unordered_architecture(p, {"tasks": [{"id": "T1"}, {"id": "T2"}]})

        assert "arch_no_dependency" in [i.get("type") for i in p.issues], \
            "没进 issues ⇒ GATE2 人审页上看不见"
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists() and "arch_no_dependency" in log.read_text(encoding="utf-8"), \
            "没出声 ⇒ 聚合视图里也看不见"


class TestAbstractionBacklog:
    """⑦ 待补的抽象**不能只挑最新的** —— 否则老账永远排不上。

    2026-09-13 真机量化：原来 `sort(-timestamp)` 取前 `limit` 条 + `limit=3`，
    等于每次大扫除只处理**刚产生的那 3 条**。实测：待补 25 条里
    **09-12 积压的 22 条一条没动**，覆盖率停在 8/33 = 24%，
    而且**补的速度 ≈ 新增的速度** ⇒ 老账永远排不上。
    """

    @staticmethod
    def _nodes(n: int):
        """**用真类型**（`EventNode.from_dict`），不手搭替身 ——
        替身不跟着真实类型长大，加个字段就 AttributeError（2026-09-13 踩过）。"""
        from singularity.scheduler._memory_core import EventNode
        return [EventNode.from_dict({
            "task_id": f"t{i}", "content": "x", "timestamp": 1000 + i,
            "emb": [], "attrs": {}, "trajectory": "t" * 20,
        }) for i in range(n)]

    def test_oldest_gets_a_slot(self):
        """**这一条就是那个 bug 的形状**：只挑最新的 ⇒ 最旧的永远轮不上。"""
        from singularity.scheduler import _memory_consolidator as C
        todo = self._nodes(10)                      # 已按"最新在前"排好
        picked = [n.task_id for n in C._pick_backfill_targets(todo, 3)]
        assert todo[-1].task_id in picked, \
            "最旧的那条没被轮到 —— 老账会永远排不上（这正是原来的毛病）"
        assert len(picked) == 3, "一次别多补，条数是要花钱的"

    def test_no_duplicate_picks(self):
        from singularity.scheduler import _memory_consolidator as C
        for limit in (2, 3, 4, 5):
            ids = [n.task_id for n in C._pick_backfill_targets(self._nodes(20), limit)]
            assert len(ids) == len(set(ids)), f"limit={limit} 挑重了：{ids}"

    def test_short_list_returns_everything(self):
        from singularity.scheduler import _memory_consolidator as C
        for n in (0, 1, 3):
            assert len(C._pick_backfill_targets(self._nodes(n), 3)) == n

    def test_backlog_warns_when_over_threshold(self, monkeypatch):
        from singularity.scheduler import _memory_consolidator as C
        from singularity.scheduler import config
        nodes = self._nodes(C._ABSTRACTION_BACKLOG_WARN_AT)
        monkeypatch.setattr(C, "_load_events", lambda: {n.task_id: n for n in nodes})
        monkeypatch.setattr(C, "abstract_trajectory", lambda *a, **k: None)  # 别真调模型
        C.backfill_abstractions(limit=3)
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists() and "abstraction_backlog" in log.read_text(encoding="utf-8"), \
            "欠账到阈值却没出声 —— 积压只有翻盘才看得见"

    def test_no_warn_below_threshold(self, monkeypatch):
        from singularity.scheduler import _memory_consolidator as C
        from singularity.scheduler import config
        nodes = self._nodes(3)
        monkeypatch.setattr(C, "_load_events", lambda: {n.task_id: n for n in nodes})
        monkeypatch.setattr(C, "abstract_trajectory", lambda *a, **k: None)
        C.backfill_abstractions(limit=3)
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert not (log.exists() and "abstraction_backlog" in log.read_text(encoding="utf-8")), \
            "欠账不多却也报警 ⇒ 又变成噪声源"


class TestDegradedDependencyIsVisible:
    """⑧ 上游失败、下游"降级运行" —— 人审时必须看得见。

    2026-09-13 真机（项目 `1789303369052`）：实现任务 900s 超时失败，
    写测试的被标"上游依赖 … 已失败 (降级运行)"**照常起跑**（有意设计，不级联失败）——
    而**上一轮它就是这么自己把实现写了的**。
    ⚠️ 当时那句 error **只写在 task 字段里**：验收不读、`project.issues` 是空的、
    GATE3 页上**一个字都没有**，人看到的是"机械检查 9/9 全过"。
    """

    def _task(self, error: str) -> str:
        from singularity.scheduler import tracker
        from singularity.scheduler.tracker import TaskStatus
        t = tracker.create("实现 txtstat", project_id="p1")
        tracker.transition(t.id, TaskStatus.FAILED, error=error)
        return t.id

    def test_degraded_task_leaves_both_traces(self):
        from singularity.scheduler import workflow as W
        from singularity.scheduler import config
        tid = self._task("上游依赖 1789303900782 已失败 (降级运行)")
        p = P.ProjectState(id="p1", name="t")
        p.task_ids = [tid]
        p.issues = []
        W._flag_degraded_tasks(p)

        assert "degraded_dependency" in [i.get("type") for i in p.issues], \
            "没进 issues ⇒ GATE3 人审页上看不见（人只看到『机械检查全过』）"
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists() and "degraded_dependency" in log.read_text(encoding="utf-8"), \
            "没出声 ⇒ 聚合视图里也看不见"

    def test_normal_task_does_not_flag(self):
        """普通失败（不是降级）不该被这条抓 —— 否则又是个噪声源。"""
        from singularity.scheduler import workflow as W
        tid = self._task("QA:fail: tests failed")
        p = P.ProjectState(id="p1", name="t")
        p.task_ids = [tid]
        p.issues = []
        W._flag_degraded_tasks(p)
        assert "degraded_dependency" not in [i.get("type") for i in p.issues]


class TestFileOverlapIsVisible:
    """⑨ 任务越界改了**兄弟任务的产出文件** —— 人审时必须看得见。

    2026-09-13 轮 5 真机（项目 `1789303369052`）：实现任务**顺手把测试也写了**
    （`changed_files = ['txtstat.py', 'test_txtstat.py']`）⇒ **写测试的那个任务空手**
    ⇒ 零文件改动 ⇒ `QA:fail: [completeness] 无文件改动` ⇒ 项目 `all_tasks_failed`、卡在 GATE2。
    **门禁判得对**，但它判的是"你没干活"，**真正的原因（活被兄弟抢了）当时没有出口**。

    ⚠️ **这是"报"不是"防"** —— 提示词那条（别替别的任务干活）是防，防不住的至少报得出来。
    """

    def _mk(self, desc: str, files: list[str] | None):
        from singularity.scheduler import tracker, neijinglu
        t = tracker.create(desc, project_id="p1")
        if files is not None:
            sql = neijinglu.config_trace_path(t.id)
            sql.parent.mkdir(parents=True, exist_ok=True)
            sql.write_text(json.dumps({"changed_files": files}), encoding="utf-8")
        return t.id

    def _proj(self, tids):
        p = P.ProjectState(id="p1", name="t")
        p.task_ids = list(tids)
        p.issues = []
        return p

    def test_stealing_a_siblings_file_is_flagged(self):
        """实现任务改了**只有测试任务点名**的文件 ⇒ 必须进 issues + 出声。"""
        from singularity.scheduler import workflow as W
        from singularity.scheduler import config
        t_impl = self._mk("实现 txtstat.py：流式计数核心", ["txtstat.py", "test_txtstat.py"])
        t_test = self._mk("编写 test_txtstat.py：口径与回归", [])
        p = self._proj([t_impl, t_test])
        W._flag_file_overlap(p)

        assert "task_file_overlap" in [i.get("type") for i in p.issues], \
            "没进 issues ⇒ 人审页上看不出『它其实是被兄弟抢了活』"
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists() and "task_file_overlap" in log.read_text(encoding="utf-8"), \
            "没出声 ⇒ 聚合视图里也看不见"

    def test_own_files_are_never_flagged(self):
        """**反例**：自己描述里点过名的文件，改多少都不算越界 —— 那正是它的活。

        ⚠️ **这条第一版是假绿**（变异验证抓的）：我原来的场景里 T2 描述**没点名**
        T1 的文件，于是"减不减自己点过的"结果一样 ⇒ 把 `- mine[tid]` 删掉它照样绿。
        **真场景是"两个任务的描述都点名了同一个文件"** —— 写测试的任务描述里
        几乎必然提到"测 `txtstat.py`"。这时没有那一减，T1 改自己的产出也会被误报。
        """
        from singularity.scheduler import workflow as W
        t1 = self._mk("实现 txtstat.py：流式计数核心", ["txtstat.py"])
        t2 = self._mk("编写 test_txtstat.py：直测 txtstat.py 的 count_stats", [])
        p = self._proj([t1, t2])
        W._flag_file_overlap(p)
        assert "task_file_overlap" not in [i.get("type") for i in p.issues], \
            "自己的产出被兄弟描述提了一嘴就误报 —— 这判据会立刻变噪声源"

    def test_single_task_project_is_never_flagged(self):
        """**反例**：单任务项目没有"兄弟"，谈不上越界（也防住误报）。"""
        from singularity.scheduler import workflow as W
        t1 = self._mk("实现 txtstat.py", ["txtstat.py", "test_txtstat.py"])
        p = self._proj([t1])
        W._flag_file_overlap(p)
        assert "task_file_overlap" not in [i.get("type") for i in p.issues]

    def test_no_changed_files_is_never_flagged(self):
        """**反例**：没有 trace / 没改文件 ⇒ 无从判断，不报（fail-quiet，不是 fail-loud）。"""
        from singularity.scheduler import workflow as W
        t1 = self._mk("实现 txtstat.py", None)
        t2 = self._mk("编写 test_txtstat.py", [])
        p = self._proj([t1, t2])
        W._flag_file_overlap(p)
        assert "task_file_overlap" not in [i.get("type") for i in p.issues]

    def test_decimal_numbers_are_not_files(self):
        """**反例**：描述里的 `0.55` 不是文件名 —— 判据窄一寸，误报就少一片。"""
        from singularity.scheduler import workflow as W
        assert W._files_named_in("置信度 0.55，阈值 0.85") == set()

    def test_wired_into_the_verification_path(self):
        """**接线**：它必须真的挂在验收那条路上 —— 函数对 ≠ 接线通。"""
        import inspect
        from singularity.scheduler import workflow as W
        src = inspect.getsource(W._run_verification)
        assert "_flag_file_overlap(project)" in src, \
            "没接进验收 ⇒ 人审页上永远不会出现这条 issue"


class TestKilledTaskIsNotSelfWrapup:
    """⑨ "被砍" ≠ "自己收尾" —— 后者是修复生效，前者是它没生效。

    执行器自带提前量（`TASK_DEADLINE_S − TASK_WRAPUP_MARGIN_S`），设计上该在
    外层那刀**之前**自己回来（走 `deadline_wrapup`，到不了收割那段）。
    ⇒ **走到收割 = 那条修复这轮没生效**，必须能分开 —— 否则两种情况盘上长得一样。
    """

    def test_killed_task_warns(self):
        from singularity.scheduler import orchestrator, config
        orchestrator._flag_killed_without_wrapup("t-abc")
        log = config.QIDIAN_DIR / "alerts.jsonl"
        assert log.exists() and "task_killed_no_wrapup" in log.read_text(encoding="utf-8"), \
            "被砍却不出声 ⇒ 分不出『修复没生效』和『这轮碰巧慢』"


class TestRatchetResetsOnHumanIntervention:
    """③ 人工批准 GATE2 = 人到场兜底，自动重试配额必须跟着恢复。"""

    def test_approving_gate2_resets_both_counters(self):
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2,
                           review_failures=3, integrate_failures=2)
        p.confirm_gate(Phase.GATE2, "approved")
        assert p.phase == Phase.EXECUTING
        assert p.review_failures == 0, \
            "棘轮没复位 —— 批准后集成合并成功又会因计数超限被打回 GATE2，用户永远出不去"
        assert p.integrate_failures == 0

    def test_only_gate2_resets(self):
        """别的门不碰这两个计数：只有 GATE2 是自动重试的兜底门。"""
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE1, review_failures=3)
        p.confirm_gate(Phase.GATE1, "approved")
        assert p.review_failures == 3


class TestCorruptFileDoesNotVanishSilently:
    """④ 读不出的项目文件不能凭空消失 —— 文件还在，要说出来。

    这条对应真实事故：两个进程写坏同一个 tmp，项目 JSON 末尾多出一个 `}`，
    `list_all()` 静默 continue，项目从界面上消失、没有任何提示。
    """

    def test_load_warns_and_keeps_the_file(self):
        pid = P.create(name="t", template="feature", description="x").id
        path = P._projects_dir() / f"{pid}.json"
        path.write_text(path.read_text(encoding="utf-8") + "}", encoding="utf-8")

        assert P.load(pid) is None, "读不出来返回 None 本身没问题"
        assert path.exists(), "文件绝不能删 —— 它是唯一的数据"

        msgs = [str(a.get("msg", "")) for a in witness.read_alerts(limit=200)]
        assert any("load_failed" in m for m in msgs), \
            "读失败却没有任何告警 —— 界面上就是「项目凭空少一个」，查不出为什么"

    def test_list_all_warns_on_unlistable(self):
        pid = P.create(name="t", template="feature", description="x").id
        (P._projects_dir() / f"{pid}.json").write_text("{ 这不是 json", encoding="utf-8")

        P.list_all()   # 不该抛 —— 但也不能一声不吭

        msgs = [str(a.get("msg", "")) for a in witness.read_alerts(limit=200)]
        assert any("unlistable" in m for m in msgs), \
            "磁盘上有个不认的项目文件，界面上少一个，告警里也什么都没有"


class TestAliasIsNotSecondOpinion:
    """⑤ 两个 id 指向同一个实际模型时，委员会不能拿它占两个席位。

    假多样性也是静默失败：界面上明明三个模型、看着是"多视角碰撞"，
    实际是同一个模型自己跟自己碰 —— 而那是唯一验证过有价值的那个能力。

    真实触发点：DeepSeek 2026-09-14 12:00 起把 `deepseek-v4-pro` 全部路由到
    V4.1-Flash。请求名不变，只有响应体的 `model` 字段说实话。
    """

    def test_alias_roundtrip(self):
        from singularity.scheduler import api_store as A
        A.record_alias("old-name", "new-name")
        assert A.canonical("old-name") == "new-name"
        assert A.canonical("new-name") == "new-name", "非别名不该被改"
        assert A.canonical("unrelated") == "unrelated"

    def test_alias_chain_follows(self):
        """A→B→C 也要归到 C；环不能死循环。"""
        from singularity.scheduler import api_store as A
        A.record_alias("a", "b")
        A.record_alias("b", "c")
        assert A.canonical("a") == "c"
        A.record_alias("c", "a")      # 成环
        assert A.canonical("a") in ("a", "b", "c")   # 只要不挂住就行

    def test_identity_not_recorded(self):
        from singularity.scheduler import api_store as A
        A.record_alias("same", "same")
        assert not (A._load_raw().get(A._ALIAS_KEY) or {}), "同名不该记账"

    def test_chain_keeps_canonical_not_stale_alias(self, monkeypatch):
        """撞车时留"名字就是实际模型"的那个 —— 留旧名会把能力评级也带错。"""
        from singularity.scheduler import dispatcher as D
        from singularity.scheduler import api_store as A
        from singularity.scheduler import _model_breaker as MB
        A.record_alias("stale-alias", "real-model")
        monkeypatch.setattr(D, "agent_api_available", lambda a: True)
        monkeypatch.setattr(MB, "is_available", lambda m: True)

        agents = {"any": [{"model": "stale-alias"}, {"model": "real-model"},
                          {"model": "other"}]}
        chain = [a["model"] for a in D.pick_agent_fallback_chain(agents, "any")]
        assert chain == ["real-model", "other"], \
            f"别名没去重或留错了那个：{chain}"


class TestConfigKeysAreAlive:
    """⑥ 配置文件里的键必须真的有人读 —— **死键比没配更糟**。

    真实事故：`fusion.toml` 的 `[custom]` 里躺着 `judge_model` + `call_model`，
    两个都是 v1 的角色名、v2 全仓无人读；而 v2 唯一读的 `extract_model` 不存在。
    于是永远静默走硬编码默认值，谁也看不出"我配了呀"配的是空气。
    代价：默认值恰是欠费的 `glm-5.3-flash` → 连撞两个 429 → 退到委员身上，
    「提取」一步单独烧 170 秒，整场融合 621 秒。
    """

    def test_every_key_in_fusion_toml_is_read_somewhere(self):
        import re
        from singularity.scheduler import config
        d = config.SCHEDULER_DIR
        keys = re.findall(r"^([a-z_]+)\s*=", (d / "fusion.toml").read_text(encoding="utf-8"), re.M)
        assert keys, "fusion.toml 一个键都没有 —— 那 [custom] 就是摆设"
        src = (d / "execution_judge.py").read_text(encoding="utf-8")
        dead = [k for k in keys if f'"{k}"' not in src and f"'{k}'" not in src]
        assert not dead, (
            f"fusion.toml 里的 {dead} 没有任何地方读 —— 你以为配上了，"
            f"实际走的是代码里的硬编码默认值，而且没有任何提示。")


class TestPhaseTrajectory:
    """⑦ 阶段流转必须有轨迹 —— 出事时要能一眼看出来。

    以前 21 处 `phase = X` 散在 4 个文件、两套驱动各写各的：出问题时落盘里
    只有**最终** phase，没有任何轨迹。"点通过永远弹回 GATE2"那个死锁，
    用户只能看到界面在重复，翻遍项目文件也看不出是谁、第几次把它推回去的。
    """

    def test_set_phase_records_trajectory(self):
        p = P.ProjectState(id="x", name="y")
        p.set_phase(Phase.PLANNING, "测试")
        p.set_phase(Phase.GATE2, "架构完成")
        traj = [(e["from"], e["to"]) for e in p.lineage if e.get("action") == "phase"]
        assert traj == [("template", "planning"), ("planning", "gate2")]

    def test_reason_is_kept(self):
        p = P.ProjectState(id="x", name="y")
        p.set_phase(Phase.GATE2, "审查自动修已达上限(2轮)")
        assert p.lineage[-1]["reason"] == "审查自动修已达上限(2轮)"

    def test_noop_transition_not_recorded(self):
        """同阶段重复设置不记 —— 否则轨迹会被空转刷满、真信号被淹。"""
        p = P.ProjectState(id="x", name="y")
        p.set_phase(Phase.PLANNING)
        p.set_phase(Phase.PLANNING)
        assert len([e for e in p.lineage if e.get("action") == "phase"]) == 1

    def test_loop_is_visible(self):
        """今天那个死锁的场景：反复回到 GATE2 必须在轨迹里看得见。"""
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2)
        for _ in range(3):
            p.set_phase(Phase.EXECUTING, "人工批准")
            p.set_phase(Phase.INTEGRATING, "任务全部到终态")
            p.set_phase(Phase.GATE2, "审查自动修已达上限")
        tos = [e["to"] for e in p.lineage if e.get("action") == "phase"]
        assert tos.count("gate2") == 3

    def test_no_raw_phase_assignment_anywhere(self):
        """全仓不该再有裸的 `.phase = X` —— 绕过 set_phase 就没有轨迹。

        这条是防回潮：新加的流转点如果图省事直接赋值，留痕就悄悄缺一块，
        而且缺得没有任何提示（这正是本文件在防的那一类）。
        """
        import re
        from singularity.scheduler import config
        bad = []
        for f in sorted(config.SCHEDULER_DIR.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "`" in line:
                    continue          # 反引号里的是文档/代码引用，不是真赋值
                if re.search(r"\.phase = ", line) and "self.phase = phase" not in line:
                    bad.append(f"{f.name}:{i}")
        assert not bad, f"有绕过 set_phase 的裸赋值（那样不留痕）：{bad}"


class TestUnknownKeysDoNotLoseProjects:
    """⑧ 文件里多余的键，不能让整个项目消失。

    真实形状：`from_dict` 结尾是裸的 `cls(**d)` —— 文件里多一个当前代码不认识的键
    （旧版本留下的、或手改的）就抛 `TypeError`，而 `load()` 捕的正是 TypeError，
    于是一句"项目不存在"、项目从界面上消失，只留一条 load_failed 告警。
    **删任何一个字段，所有存量文件都会立刻变成这样**（防御模式 #40）。
    """

    def test_unknown_key_dropped_not_fatal(self):
        d = P.ProjectState(id="x", name="y").to_dict()
        d["some_future_field"] = 1
        d["token_spent"] = 0.0        # 真删过的那个字段，存量文件里都有
        p = P.ProjectState.from_dict(d)
        assert p.id == "x"
        assert not hasattr(p, "token_spent"), "已删字段不该复活"
        assert "token_spent" not in p.to_dict()

    def test_such_a_file_still_loads_and_lists(self):
        pid = P.create(name="t", template="feature", description="x").id
        path = P._projects_dir() / f"{pid}.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        d["legacy_field"] = "old"
        path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

        assert P.load(pid) is not None, "文件里多个陌生键 → 整个项目消失"
        assert any(p.id == pid for p in P.list_all()), "list_all 里也不能少"

    def test_dropping_is_visible_not_silent(self):
        d = P.ProjectState(id="x", name="y").to_dict()
        d["legacy_field"] = 1
        P.ProjectState.from_dict(d)
        msgs = [str(a.get("msg", "")) for a in witness.read_alerts(limit=200)]
        assert any("unknown_fields_dropped" in m for m in msgs), \
            "丢字段本身是新旧兼容，该做；但悄悄丢不是"


class TestUnknownPhaseValueDoesNotCrash:
    """⑫ 文件里的 phase 值不认识时，不能让整个项目炸掉。

    防御模式 #40 那个坑换了个字段名：`Phase(d["phase"])` 抛的是 **ValueError**，
    而 `load()` 捕的是 `(JSONDecodeError, KeyError, TypeError)` —— ValueError 直接穿出去。
    **删任何一个枚举值（比如 FIXING），存量文件里的那个值就会立刻变成这种情况。**
    """

    def test_unknown_phase_falls_back_with_warning(self):
        d = P.ProjectState(id="x", name="y").to_dict()
        d["phase"] = "fixing"              # 真删过的那个值
        p = P.ProjectState.from_dict(d)
        assert p.phase == Phase.TEMPLATE, "认不出的 phase 该退回安全态，不是抛异常"
        msgs = [str(a.get("msg", "")) for a in witness.read_alerts(limit=200)]
        assert any("unknown_phase" in m for m in msgs), "退回了但没说 —— 用户会以为项目自己变回待开始了"

    def test_such_a_file_still_loads(self):
        pid = P.create(name="t", template="feature", description="x").id
        path = P._projects_dir() / f"{pid}.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        d["phase"] = "some_removed_phase"
        path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        assert P.load(pid) is not None, "认不出的 phase 值 → load 抛 ValueError 穿出去"
        assert any(p.id == pid for p in P.list_all())


class TestRunPhaseKnowsEveryPhase:
    """⑨ `run_phase` 必须认识枚举里的**每一个** Phase。

    真实事故：`INTEGRATING` / `DELIVERING` 是代码后加的两个阶段，
    而 `run_phase` 的 if 链没跟上 → 落到 `else: 未知 phase`。
    它明明是个正经阶段，报"未知"就是在骗人 —— 而且不报错、不告警，只印一行字。

    这里排除了 RESEARCHING / PLANNING（会真调模型）和 EXECUTING（会建任务）。
    剩下这些都是纯状态流转，不碰网络。
    """

    SAFE = [Phase.TEMPLATE, Phase.GATE1, Phase.GATE2, Phase.GATE3,
            Phase.REVIEWING, Phase.INTEGRATING,
            Phase.DELIVERING, Phase.DONE]

    @pytest.mark.parametrize("phase", SAFE)
    def test_does_not_fall_through_to_unknown(self, phase):
        from singularity.scheduler import workflow
        p = P.ProjectState(id="x", name="y", phase=phase)
        msg = workflow.run_phase(p, {})
        assert "未知 phase" not in msg, f"{phase.value} 又落到 else 了：{msg}"

    def test_every_phase_value_is_covered(self):
        """反向锁：枚举里出现新值而 run_phase 没跟上时，上面那条参数化会漏。

        这条直接数"哪些 Phase 值属于 SAFE 之外" —— 新增一个没人处理的阶段时，
        必须显式决定它归哪边（真调模型的 / 纯流转的），不能默默掉进 else。
        """
        uncovered = set(Phase) - set(self.SAFE)
        expected = {Phase.RESEARCHING, Phase.PLANNING, Phase.EXECUTING}
        assert uncovered == expected, (
            f"Phase 枚举变了：{sorted(p.value for p in uncovered - expected)} 新出现，"
            f"得决定它是否需要真跑（要的话加进排除名单，不要的话加进 SAFE）")


class TestArchValidationActuallyBlocks:
    """⑩ 架构校验不过，就不能放行。

    原来 `_validate_architecture` 的 blockers 只进 lineage 的一个**计数**、
    加一条返回文案（SSE 一闪而过）—— 项目状态里查不到、GATE2 面板上看不见。
    放行后流到执行层，以"拆不出任务、项目无声卡住"的形式爆出来
    （orchestrator 里记着那次：卡了 13 分钟，日志一行都没有）。
    """

    def test_validator_distinguishes_broken_from_complete(self):
        from singularity.scheduler._workflow_phases import _validate_architecture
        good = {"architecture": "x", "modules": [{"name": "a"}],
                "data_model": {"database": "sqlite"}, "tech_stack": {"language": "py"},
                "tasks": [{"id": "T1", "title": "t", "description": "d",
                           "complexity": "low", "layer": "backend", "acceptance": "a"}],
                "constraints": [{"rule": "r"}]}
        bad = {"architecture": "x"}
        blockers = lambda a: [i for i in _validate_architecture(a)
                              if "缺少" in i or "无效" in i or "应为" in i]
        assert blockers(good) == [], "完整架构不该有阻塞项"
        assert blockers(bad), "残缺架构必须被拦下 —— 否则校验器形同虚设"

    def test_arch_invalid_blocks_gate2_approval(self):
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2,
                           issues=[{"type": "arch_invalid", "detail": "缺少必填字段: modules"}])
        assert p.confirm_gate(Phase.GATE2, "approved") is None, "不合格的架构被放行了"
        assert p.phase == Phase.GATE2

    def test_cleared_issue_allows_approval(self):
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2,
                           issues=[{"type": "arch_invalid", "detail": "x"}])
        p.issues = []                       # 重新规划过、校验通过了
        assert p.confirm_gate(Phase.GATE2, "approved") == Phase.EXECUTING

    def test_other_gates_not_affected(self):
        """只有 GATE2 有这个前置条件 —— 别的门不该被架构问题卡住。"""
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE1,
                           issues=[{"type": "arch_invalid", "detail": "x"}])
        assert p.confirm_gate(Phase.GATE1, "approved") == Phase.PLANNING

    def test_warning_does_not_block(self):
        """**判据是"下一步还能不能干"，不是"字段全不全"**。

        缺 data_model / tech_stack / constraints 只记不拦 —— 一个单文件 CLI
        本来就没有 data_model，按"六字段齐全"拦会把好活挡在门外。
        只有 tasks 缺失/为空才致命：拆不出任务，执行层必然卡死。
        """
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2,
                           issues=[{"type": "arch_warning",
                                    "detail": "架构校验有缺项（3 项）：缺少必填字段: data_model；缺少必填字段: tech_stack"}])
        assert p.confirm_gate(Phase.GATE2, "approved") == Phase.EXECUTING, \
            "非致命缺项不该拦住放行"

    def test_auto_mode_does_not_spin_when_gate_is_blocked(self):
        """被拦时 auto_mode 必须**停下**，不能原地打转。

        真事故：`run_phase` 的 auto 分支原来无条件 `continue`，配上"不放行就原地不动"
        就成了死循环 —— auto_mode 下烧 CPU 烧到天荒地老，而且**不报错、不退出**，
        测试是"挂住"不是"失败"（实测跑了十几分钟才发现）。

        **所以这里刻意用线程 + 超时**：让回归表现为"红"，而不是"吊死"。
        一个挂住的测试比一个失败的测试坏得多 —— 它挡住整套。
        """
        import threading
        from singularity.scheduler import workflow
        p = P.ProjectState(id="x", name="y", phase=Phase.GATE2, auto_mode=True,
                           issues=[{"type": "arch_invalid", "detail": "缺少必填字段: tasks"}])
        box: dict = {}

        def go():
            box["msg"] = workflow.run_phase(p, {})

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "run_phase 5 秒没返回 —— auto_mode 又在原地打转了"
        assert "未放行" in box.get("msg", "")
        assert p.phase == Phase.GATE2

    def test_blocked_approval_reports_error_not_done(self):
        """被拦时必须报错，不能顺着写成 `next_phase: done` —— 那是在骗人。"""
        from singularity.web.app import app
        pid = P.create(name="_t", template="feature", description="x").id
        try:
            p = P.load(pid)
            p.set_phase(Phase.GATE2, "架构完成")
            p.issues = [{"type": "arch_invalid", "detail": "缺少必填字段: modules"}]
            P.save(p)
            r = app.test_client().post(f"/api/projects/{pid}/gate-confirm",
                                       json={"gate": "gate2", "decision": "approved"})
            assert r.status_code == 409, f"该拒绝放行，实际 {r.status_code}"
            assert P.load(pid).phase == Phase.GATE2
        finally:
            for f in P._projects_dir().glob(f"{pid}*"):
                f.unlink()


class TestAlertSinkFailureIsVisible:
    """⑥ 告警写不进去时，**必须从第二条通道出声**。

    形状和上面几条一样：**系统没做、但从外面看一切正常**。
    而 `witness.warn` 是**全仓告警的唯一汇聚点**（`alerts.jsonl` 是独立通道，
    心跳那条路早就证明过存不住告警）—— 它静默失败 = **观测整体失明**。

    原来的 `except OSError: pass` 连注释都在替自己辩护（"记告警失败不该再抛"）——
    **"不抛"是对的，"不吭声"不是**。这里锁的是后半句。
    """

    def test_写告警失败要走_logging_出声(self, monkeypatch, tmp_path, caplog):
        import logging
        # 让告警路径落在一个**文件**底下 ⇒ open('a') 抛 NotADirectoryError。
        # 不用 chmod：那在 root 下不成立，测试会变成"看运气"。
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a dir", encoding="utf-8")
        monkeypatch.setattr(witness, "_alerts_path",
                            lambda: blocker / "alerts.jsonl")

        with caplog.at_level(logging.ERROR, logger="witness"):
            witness.warn("test_scope", "这条写不进去")   # 不许抛

        assert caplog.records, "写告警失败却一声不吭 —— 观测整体失明而外表看着正常"
        assert any("失明" in r.getMessage() for r in caplog.records), \
            f"出声了但没说清后果（下一个读日志的人会以为只是个小毛病）：{[r.getMessage() for r in caplog.records]}"

    def test_写得进去时不许刷日志(self, monkeypatch, tmp_path, caplog):
        """对照：正常路径**一条 error 都不该有**，否则第二条通道会变成噪声源。"""
        import logging
        monkeypatch.setattr(witness, "_alerts_path",
                            lambda: tmp_path / "alerts.jsonl")
        with caplog.at_level(logging.ERROR, logger="witness"):
            witness.warn("test_scope", "正常写一条")
        assert not caplog.records, f"正常路径也在报错：{[r.getMessage() for r in caplog.records]}"
        assert (tmp_path / "alerts.jsonl").exists(), "正常路径没写进去"
