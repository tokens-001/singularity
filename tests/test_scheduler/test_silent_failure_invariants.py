"""静默失败的四条不变量 —— 锁住"下次不许再犯"。

来自 2026-09-11 一天之内挖出的六个案例（见 `docs/防御模式.md` #44~46）。
共同形状：**系统没做或做错了，但从外面看一切正常** —— 六个缺陷没有一个报错。

**刻意测不变量，而不是测路径。** 路径测试（monkeypatch 某个函数再看它被调没被调）
在这套代码里结构上抓不到 P0：上一轮审计的 P0-1（门禁取裸 `git diff` 恒空）就是
门禁"在跑、在记"，但证据源本身是坏的 —— 路径测试全绿。
"""

import dataclasses

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
        p = P.ProjectState(id="x", name="y", phase=Phase.EXECUTING,
                           issues=[{"type": "t"}], review_failures=2,
                           task_ids=["a"], owner_confirm={"gate2": "approved"},
                           lineage=[{"action": "phase"}])
        again = P.ProjectState.from_dict(p.to_dict())
        assert again.to_dict() == p.to_dict(), "序列化往返丢东西"


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
