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
