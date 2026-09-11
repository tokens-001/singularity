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
