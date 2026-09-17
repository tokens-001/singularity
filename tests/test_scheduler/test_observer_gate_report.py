"""观察者**主动汇报**：项目停在门上就跟用户说一段白话（2026-09-17 用户提）。

用户原话：「奇点的用户不可能都是专业开发者……专业架构方案看不懂，
可以让观察者**翻译为白话在对话框汇报**，当然原本架构方案汇报还是保留」。

在此之前观察者**只在被问时才开口** —— 而且连项目内容都看不见（同一天下午才接上）。

⚠️ 判据是「**最后一次进门** vs **最后一次汇报**的先后**」，不是"有没有报过"：
   同一道门可能进两次（打回重跑再回来），第二次也该说一遍。
⚠️ "报过了"落**盘**（项目 json 的 `lineage`），不放内存 —— 否则
   "改完代码到下一道门重启"这个例行操作正好会把该说的话吞掉。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import _observer_worker as ow
from singularity.scheduler.project import Phase


def _mk(tmp_path, monkeypatch, phase, lineage=None, text="大白话汇报"):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    p = proj_mod.ProjectState(
        id="p1", name="测试项目", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=list(lineage or []), handoffs=[], agent_lineup={})
    p.phase = phase
    monkeypatch.setattr(proj_mod, "list_all", lambda: [p])
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    # 观察者调用要桩掉（真调会花钱、也不会在测试里返回）
    import singularity.scheduler._observer_answer as oa
    calls = []
    monkeypatch.setattr(oa, "_answer_question_inner",
                        lambda prompt, pid: (calls.append((prompt, pid)) or text))
    # 推送出口
    from singularity.scheduler import orchestrator
    pushed = []
    monkeypatch.setattr(orchestrator, "_pending_sse_events", pushed)
    return p, calls, pushed


class TestGateReport:
    def test_停在门上就汇报_并推给那个项目(self, tmp_path, monkeypatch):
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1)

        ow._maybe_report_gates()

        assert len(calls) == 1, "没叫观察者说话"
        assert calls[0][1] == "p1", "没把 project_id 传下去（它要靠这个才看得见项目）"
        assert "大白话" in calls[0][0], f"提示词没要求白话：{calls[0][0][:80]}"
        assert len(pushed) == 1, "没推到 SSE 那条通道"
        ev = pushed[0]
        assert ev["kind"] == "observer_report"
        assert ev["project_id"] == "p1", "推送没带 project_id ⇒ 前端会塞进别人家的对话框"
        assert "大白话汇报" in json.loads(ev["msg"])["text"]

    def test_报过的门不再重复说(self, tmp_path, monkeypatch):
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1, lineage=[
            {"action": "phase", "to": "gate1", "reason": "调研完成"},
            {"action": "observer_report", "gate": "gate1"},
        ])

        ow._maybe_report_gates()

        assert calls == [], "同一道门说了两遍（用户会看到重复的话）"

    def test_同一道门第二次进还要说(self, tmp_path, monkeypatch):
        """打回重跑又回到这道门 —— 那是**新的一次审批**，不能因为"报过"就不吭声。"""
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1, lineage=[
            {"action": "phase", "to": "gate1", "reason": "第一次"},
            {"action": "observer_report", "gate": "gate1"},
            {"action": "gate1_rejected", "feedback": "重来"},
            {"action": "phase", "to": "gate1", "reason": "第二次"},
        ])

        ow._maybe_report_gates()

        assert len(calls) == 1, "第二次进同一道门却不说话了"

    def test_不在门上就不打扰_而且不是崩了才不打扰(self, tmp_path, monkeypatch):
        """⚠️ **断言要能分清"没说话"和"崩了所以没说话"**（2026-09-17 被变异抓出来）。

        第一版只断言 `calls == []` —— 而"去掉门的判断"这个变异**不会让它红**：
        去掉之后 `_GATE_LABEL[phase]` 直接 `KeyError`，异常被 `except` 吞掉，
        于是 `calls` 照样是空的。**"正确地沉默"和"崩在沉默里"长得一模一样。**
        ⇒ 所以还要断言**没出错**。
        """
        import singularity.scheduler._observer_worker as _w
        warns = []
        monkeypatch.setattr(_w.witness, "warn",
                            lambda scope, msg, **k: warns.append(msg))
        for phase in (Phase.EXECUTING, Phase.RESEARCHING, Phase.DONE):
            p, calls, pushed = _mk(tmp_path, monkeypatch, phase)
            ow._maybe_report_gates()
            assert calls == [], f"{phase} 不是门，不该说话"
            assert warns == [], f"{phase} 下报错了（异常被吞 ⇒ 看着像「没说话」）: {warns}"
            assert pushed == []

    def test_汇报之后要落痕_否则每圈都会重复说(self, tmp_path, monkeypatch):
        """`add_lineage({"action":"observer_report"})` 是**下一圈不再说的唯一依据**。
        不写它 = 每 30 秒对同一个门说一遍，用户会看到刷屏。
        （第一版测试的 lineage 是**手写的**，所以从没验过这条真的会写。）"""
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1)

        ow._maybe_report_gates()

        acts = [e for e in p.lineage if e.get("action") == "observer_report"]
        assert len(acts) == 1, f"没落痕 ⇒ 下一圈还会再说一遍：{p.lineage}"
        assert acts[0]["gate"] == "gate1"

    def test_观察者没话说就不推空消息(self, tmp_path, monkeypatch):
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1, text="")
        ow._maybe_report_gates()
        assert pushed == [], "推了一条空消息（界面上会冒出一个空气泡）"

    def test_汇报塌了不连累巡检(self, tmp_path, monkeypatch):
        """汇报是附加动作 —— 它抛了不该把停滞检测一起带走。"""
        p, calls, pushed = _mk(tmp_path, monkeypatch, Phase.GATE1)
        import singularity.scheduler._observer_answer as oa

        def boom(*a, **k):
            raise RuntimeError("模型挂了")
        monkeypatch.setattr(oa, "_answer_question_inner", boom)

        ow._maybe_report_gates()      # 不该抛
