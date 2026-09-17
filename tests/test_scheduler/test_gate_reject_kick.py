"""打回之后**必须有人点火**，而且用户写的理由要真的送到。

2026-09-17。改之前打回这条路有三个洞，全都没有任何测试盯着：

1. **退到没人推的地方** —— `_REJECT_FALLBACK` 把 GATE1 打回退到 `TEMPLATE`，
   而 `TEMPLATE` 全仓没有驱动（`run_phase` 只打印"等待 Owner 填写需求"就 break，
   调度循环只管 EXECUTING 之后）⇒ **打回 = 项目永久停在原地**。
2. **不点火** —— 退到 `RESEARCHING` / `PLANNING` 也没用：这两档同样只有 `run_phase`
   能推，而前端没有 run-phase 调用者。批准那条路早就踩过这个坑（实测空等 14 分钟），
   打回这条路是**同一个形状复发**（防御模式 #28）。
3. **理由丢掉** —— `project_gate_confirm(..., feedback)` 的形参一直有、
   `handle_gate3_reject` 也一直收它，但 HTTP 路由 `app.py` 只传了两个参数
   ⇒ `feedback` **恒为 `""`**，用户写了等于没写（防御模式 #70）。

⚠️ 这些用例断言的是**真实副作用**（phase 真的变了 + 后台真的被点火 + 理由真的落盘），
不是返回值里那几个字段 —— 返回值好看而项目不动的，正是这一族 bug 的形态（#77）。
"""
import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import _api_projects as ap
from singularity.scheduler.project import Phase


def _mk(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    p = proj_mod.ProjectState(
        id="proj1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={})
    p.phase = phase
    monkeypatch.setattr(proj_mod, "load", lambda _id: p)
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    return p


def _spy_kicks(monkeypatch, ok=True):
    """记下点火调用。**不 patch confirm_gate** —— 这条链要跑真的。"""
    kicks = []

    def fake_start(pid, label, fn, *a):
        kicks.append({"pid": pid, "label": label, "fn": fn})
        return ok

    monkeypatch.setattr(ap, "_start_background", fake_start)
    from singularity.scheduler import dispatcher
    monkeypatch.setattr(dispatcher, "load_agents", lambda: {})
    return kicks


class TestGate1Reject:
    def test_退到调研_并且真的点了火(self, tmp_path, monkeypatch):
        p = _mk(tmp_path, monkeypatch, Phase.GATE1)
        kicks = _spy_kicks(monkeypatch)

        data, code = ap.project_gate_confirm("proj1", "gate1", "rejected")

        assert code == 200
        # ① 真的退到了"会重新跑"的那一档
        assert p.phase == Phase.RESEARCHING
        assert data["next_phase"] == "researching"
        # ② **真的点火了**，而且点的是 run_phase（不是随便一个函数）
        from singularity.scheduler import workflow
        assert len(kicks) == 1, "打回后没点火 —— 项目会永久停在 researching"
        assert kicks[0]["label"] == "researching"
        assert kicks[0]["fn"] is workflow.run_phase

    def test_理由要落进lineage(self, tmp_path, monkeypatch):
        p = _mk(tmp_path, monkeypatch, Phase.GATE1)
        _spy_kicks(monkeypatch)

        ap.project_gate_confirm("proj1", "gate1", "rejected", "竞品只有 3 家")

        acts = [e for e in p.lineage if e.get("action") == "gate1_rejected"]
        assert len(acts) == 1, f"打回没记进 lineage: {p.lineage}"
        assert acts[0]["feedback"] == "竞品只有 3 家"

    def test_理由超长要截断(self, tmp_path, monkeypatch):
        """照 `gate3_rejected` 的先例截 500 —— lineage 有上限，别让它被一句话撑爆。"""
        p = _mk(tmp_path, monkeypatch, Phase.GATE1)
        _spy_kicks(monkeypatch)

        ap.project_gate_confirm("proj1", "gate1", "rejected", "长" * 900)

        e = [x for x in p.lineage if x.get("action") == "gate1_rejected"][0]
        assert len(e["feedback"]) == 500


class TestGate2Reject:
    def test_退到架构_点火_并且清干净(self, tmp_path, monkeypatch):
        """GATE2 打回 = 重规划。退回调研是错的一层（调研没问题，还得再审一次 GATE1）。"""
        p = _mk(tmp_path, monkeypatch, Phase.GATE2)
        p.architecture = {"tasks": [{"id": "T1"}]}
        p.constraints_checklist = [{"type": "x", "rule": "上一版的约束"}]
        p.review_failures = 2
        p.integrate_failures = 2
        kicks = _spy_kicks(monkeypatch)

        data, code = ap.project_gate_confirm("proj1", "gate2", "rejected", "模块太粗")

        assert code == 200
        assert p.phase == Phase.PLANNING
        assert data["next_phase"] == "planning"
        assert len(kicks) == 1 and kicks[0]["label"] == "planning", "重规划没人点火"
        # 旧产物必须**一起**清：`effective_constraints()` 在清单非空时直接返回它
        # ⇒ 只清 architecture 会让验收跑**上一版约束**，比空清单更坏（防御模式 §60）
        assert p.architecture is None
        assert p.constraints_checklist == []
        # 计数器是单向棘轮（#45）：不清的话重规划出来的架构下一次集成失败就直接弹回来
        assert p.review_failures == 0
        assert p.integrate_failures == 0
        assert [e for e in p.lineage if e.get("action") == "gate2_rejected"][0]["feedback"] == "模块太粗"


class TestKickFailureIsNotSwallowed:
    def test_点火失败必须说出来(self, tmp_path, monkeypatch):
        """防御模式 #28：**没点着火却回一个像成功的包**，就是让调用方以为项目在动。

        （批准那条路至今还是丢掉 `_start_background` 的返回值 —— 这里不重复那个错。）
        """
        _mk(tmp_path, monkeypatch, Phase.GATE1)
        _spy_kicks(monkeypatch, ok=False)

        data, code = ap.project_gate_confirm("proj1", "gate1", "rejected")

        assert code == 200
        assert data["started"] is False
        assert "warning" in data, "没启动却只说 ok —— 用户会以为项目在跑"


class TestFeedbackSurvivesTheHttpLayer:
    """🔴 **本次改动的核心回归点**。

    整条链唯一断掉的地方就是 `web/app.py` 那一行只传了 gate 和 decision。
    这条走**真 Flask**（`test_client`），**删掉那行里的 `body.get("feedback", "")` 它会红**。
    """

    def _client(self):
        from singularity.web.app import app
        return app.test_client()

    def test_理由穿过HTTP落进lineage(self, tmp_path, monkeypatch):
        p = _mk(tmp_path, monkeypatch, Phase.GATE1)
        monkeypatch.setattr(ap, "_start_background", lambda *a, **k: True)  # 别真起线程调模型
        from singularity.scheduler import dispatcher
        monkeypatch.setattr(dispatcher, "load_agents", lambda: {})

        r = self._client().post("/api/projects/proj1/gate-confirm",
                                json={"gate": "gate1", "decision": "rejected",
                                      "feedback": "竞品太少，补到 5 家"})

        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["next_phase"] == "researching"
        e = [x for x in p.lineage if x.get("action") == "gate1_rejected"]
        assert e and e[0]["feedback"] == "竞品太少，补到 5 家", \
            f"理由没穿过 HTTP 层: {p.lineage}"

    def test_不传理由也不炸(self, tmp_path, monkeypatch):
        """老前端（或 curl）只发 gate+decision 时照常工作，理由记成空串。"""
        p = _mk(tmp_path, monkeypatch, Phase.GATE1)
        monkeypatch.setattr(ap, "_start_background", lambda *a, **k: True)
        from singularity.scheduler import dispatcher
        monkeypatch.setattr(dispatcher, "load_agents", lambda: {})

        r = self._client().post("/api/projects/proj1/gate-confirm",
                                json={"gate": "gate1", "decision": "rejected"})

        assert r.status_code == 200
        e = [x for x in p.lineage if x.get("action") == "gate1_rejected"]
        assert e and e[0]["feedback"] == ""


class TestObserverChatEntry:
    """聊天里说「不通过」是**第二条入口**，必须走和按钮同一条路（防御模式 #5）。

    改之前它自己 `proj.confirm_gate(...) + save(...)`，看着等价，实际漏了打回之后
    **点火重跑**那一步 ⇒ phase 变了而项目一动不动，回话却写着"将重新生成架构方案"。
    GATE3 更糟：`_REJECT_FALLBACK` 里没有 GATE3 ⇒ phase 原地不动，照样回"已退回实现阶段"。
    """

    def test_聊天打回转调共享入口(self, monkeypatch):
        from singularity.scheduler import _observer_answer as oa
        seen = []

        def fake_confirm(pid, gate, decision, feedback=""):
            seen.append((pid, gate, decision, feedback))
            return {"ok": True, "next_phase": "planning"}, 200

        monkeypatch.setattr(ap, "project_gate_confirm", fake_confirm)
        msg = oa._gate_reject_reply("proj1", "gate2")

        assert seen == [("proj1", "gate2", "rejected", "")], \
            "聊天那条路没走共享入口 —— 点了打回项目不会重新跑"
        assert "planning" in msg, f"回话该照实说退到哪: {msg}"

    def test_没启动就照实说_不编一句将重新生成(self, monkeypatch):
        from singularity.scheduler import _observer_answer as oa
        monkeypatch.setattr(ap, "project_gate_confirm",
                            lambda *a, **k: ({"ok": True, "next_phase": "researching",
                                              "started": False,
                                              "warning": "该项目已有阶段在跑"}, 200))
        msg = oa._gate_reject_reply("proj1", "gate1")
        assert "该项目已有阶段在跑" in msg, "点火没成功却回了一句漂亮的成功话术"

    def test_失败要透出原因(self, monkeypatch):
        from singularity.scheduler import _observer_answer as oa
        monkeypatch.setattr(ap, "project_gate_confirm",
                            lambda *a, **k: ({"ok": False, "error": "项目不存在"}, 404))
        assert "项目不存在" in oa._gate_reject_reply("proj1", "gate2")

    def test_聊天里说的那句话就是打回理由(self, monkeypatch):
        """用户对着观察者说「不通过，竞品太少」—— 那个「竞品太少」要跟着重跑走。

        ⚠️ 第一版这里传的是**硬编码空串**（路走通了、话没带上）：
        于是"和观察者说"还不如"在按钮旁边填个框"，用户 09-17 当场指出。
        """
        from singularity.scheduler import _observer_answer as oa
        seen = []
        def fake(pid, gate, decision, feedback=""):
            seen.append(feedback)
            return {"ok": True, "next_phase": "planning"}, 200

        monkeypatch.setattr(ap, "project_gate_confirm", fake)
        oa._gate_reject_reply("proj1", "gate2", oa._reject_reason_from("不通过，竞品太少"))
        assert seen == ["竞品太少"], f"聊天里的理由没被带上: {seen}"


class TestRejectReasonFrom:
    """从人话里剥理由。**剥不掉就说没有** —— 别把「不通过」当成修改意见送给模型。"""

    def test_光杆否定没有理由(self):
        from singularity.scheduler._observer_answer import _reject_reason_from as f
        for q in ("不通过", "不通过。", "不行", "重来", "不继续"):
            assert f(q) == "", f"{q!r} 被当成了理由: {f(q)!r}"

    def test_理由在前在后都剥得掉(self):
        from singularity.scheduler._observer_answer import _reject_reason_from as f
        assert f("不通过，竞品太少") == "竞品太少"
        assert f("竞品太少，不通过") == "竞品太少"

    def test_不许把夸的话啃坏(self):
        """`_REJECT_WORDS` 里有「改」「修改」，拿它做前缀剥离会把「改一下竞品」啃成「一下竞品」；
        `_BARE_REJECT` 里有「不」，「不错，但竞品太少」会被啃掉开头。两条都不许发生。"""
        from singularity.scheduler._observer_answer import _reject_reason_from as f
        assert f("改一下竞品") == "改一下竞品"
        assert f("不错，但竞品太少") == "不错，但竞品太少"


# ═══════════════════════════════════════════════════════════════
# 观察者要能看见它负责汇报的那个项目
# ═══════════════════════════════════════════════════════════════
# 用户原话：「本意是像 zcode 那样的独立会话，观察者汇报项目阶段，
# 包括调研、架构，等等。还有出现问题，也由观察者独立汇报。」
#
# 查下来不是"职责被瓜分"，是**那个角色从来没实现过**：
# `research_report` / `architecture` / `issues` / `lineage` **一个都没进过它的 prompt**
# （grep 零命中），`project_id` 只被用来①过滤任务②判 GATE
# ⇒ 用户问「这调研怎么样」，它答不上来 —— 它压根没看过。

class TestProjectBriefing:
    def _mk(self, tmp_path, monkeypatch, **over):
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
        (tmp_path / "qidian").mkdir(exist_ok=True)
        p = proj_mod.ProjectState(
            id="p1", name="测试项目", raw_constraints=[], owner_confirm={},
            constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
            lineage=[], handoffs=[], agent_lineup={})
        p.phase = Phase.GATE1
        for k, v in over.items():
            setattr(p, k, v)
        monkeypatch.setattr(proj_mod, "load", lambda _id: p)
        return p

    def test_没有project_id就不加这段(self):
        from singularity.scheduler._observer_definition import project_section
        assert project_section("") == ""

    def test_汇报材料要齐(self, tmp_path, monkeypatch):
        """阶段 + 调研摘要 + 架构摘要 + 未决问题 + 最近流转 —— 这几样缺了就没法汇报。"""
        self._mk(tmp_path, monkeypatch,
                 research_report={"competitive_analysis": {"products": [{"name": "jq"}]},
                                  "pitfalls": ["坑1"], "recommendation": "用标准库"},
                 architecture={"modules": [{"name": "parser"}], "tasks": [{"id": "T1"}],
                               "constraints": [{"type": "x"}]},
                 issues=[{"type": "arch_invalid", "detail": "任务没排序"}],
                 lineage=[{"action": "phase", "from": "researching", "to": "gate1",
                           "reason": "调研完成"}])
        from singularity.scheduler._observer_definition import project_section
        s = project_section("p1")
        for want in ("测试项目", "gate1", "用标准库", "parser", "任务没排序", "调研完成"):
            assert want in s, f"汇报材料里少了「{want}」:\n{s}"
        # ⚠️ **必须钉住"值"，不能只钉键名** —— 只查 `"竞品数" in s` 的话，
        #    把数改成 0、把推荐方案换成空串，测试**照样绿**（本仓栽过这个形状）。
        assert '"竞品数": 1' in s, f"竞品数没照实报:\n{s}"
        assert '"关键坑数": 1' in s, f"关键坑数没照实报:\n{s}"
        assert '"任务数": 1' in s, f"任务数没照实报:\n{s}"
        assert '"未决问题"' not in s or "任务没排序" in s

    def test_调研解析失败要说出来_不能静默当没有(self, tmp_path, monkeypatch):
        """解析失败时兜底对象里没有 recommendation —— 静默跳过的话，
        观察者就会像用户一样以为"调研没写推荐方案"（同一个坑，换个消费者）。"""
        self._mk(tmp_path, monkeypatch, research_report={"parse_error": True, "raw_output": "x"})
        from singularity.scheduler._observer_definition import project_section
        s = project_section("p1")
        assert "解析失败" in s, s
        assert "调研没给" not in s

    def test_项目读不到也要说出来(self, tmp_path, monkeypatch):
        """「没有项目」和「项目读坏了」不能长得一样 —— 本仓老病。"""
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
        (tmp_path / "qidian").mkdir(exist_ok=True)
        monkeypatch.setattr(proj_mod, "load", lambda _id: None)
        from singularity.scheduler._observer_definition import project_section
        assert "读不到" in project_section("nope")


class TestProjectSectionReachesThePrompt:
    """🔴 **接线**：那段材料必须真的出现在**要发出去的**系统提示里。

    ⚠️ **两条路都要测** —— 直连那条（无 api_key）和带工具那条（有 api_key）。
    这个仓反复栽在"同一件事两个入口，有一条忘了做全套"（§60）。
    """

    def _capture(self, monkeypatch, tmp_path, *, with_key):
        from singularity.scheduler import _observer_answer as oa
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
        (tmp_path / "qidian").mkdir(exist_ok=True)
        p = proj_mod.ProjectState(
            id="p1", name="被汇报的项目", raw_constraints=[], owner_confirm={},
            constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
            lineage=[], handoffs=[], agent_lineup={})
        p.phase = Phase.GATE2
        p.research_report = {"recommendation": "推荐用标准库", "pitfalls": []}
        monkeypatch.setattr(proj_mod, "load", lambda _id: p)
        monkeypatch.setattr(oa, "_get_observer_cfg", lambda: {
            "api_key": "k" if with_key else "", "base_url": "http://observer.test",
            "model": "m", "max_turns": 1})
        seen = []

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "好"}}], "usage": {}}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, url, **kw):
                seen.append(kw.get("json") or {})
                return _Resp()

        monkeypatch.setattr(oa.httpx, "Client", _Client)
        oa._answer_question_inner("这个项目的调研怎么样", "p1")
        return " ".join(str(m.get("content", "")) for m in seen[0].get("messages", []))

    def test_直连那条路(self, tmp_path, monkeypatch):
        sys_msg = self._capture(monkeypatch, tmp_path, with_key=False)
        assert "当前项目" in sys_msg and "被汇报的项目" in sys_msg, sys_msg[-600:]
        assert "推荐用标准库" in sys_msg, "调研摘要没进提示词"

    def test_带工具那条路(self, tmp_path, monkeypatch):
        sys_msg = self._capture(monkeypatch, tmp_path, with_key=True)
        assert "当前项目" in sys_msg and "被汇报的项目" in sys_msg, sys_msg[-600:]
        assert "推荐用标准库" in sys_msg, "调研摘要没进提示词"
