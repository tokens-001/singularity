"""分层抽象：把一条轨迹压成「具体 / 套路 / 道理」三层。

论文（arXiv 2607.29658）的核心那步 —— 消融：**完整分层 80.0 vs 原始轨迹 57.6**。
这里用一次便宜模型的调用**在线**做，不做论文那种离线批量（479 条 ≈ $211）。
⚠️ 效果**没验证**（样本太少）；立刻的价值是**省 prompt**：9000 字原文 → ~330 字。

不真调模型：`_pick_api` / `_chat` 都打桩。
"""
import json

import pytest

from singularity.scheduler import _memory_consolidator as cons
from singularity.scheduler import _memory_graph as mg


@pytest.fixture(autouse=True)
def _stub_api(monkeypatch):
    monkeypatch.setattr(cons, "_pick_api",
                        lambda: ("m1", "FAKE_KEY", "https://api.example.com/v1"))
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    monkeypatch.setattr(cons, "_record", lambda model, data: None)


def _reply(payload) -> dict:
    """构造一个 OpenAI 形状的返回。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 10}}


class TestAbstractTrajectory:
    def test_three_levels_returned(self, monkeypatch):
        monkeypatch.setattr(cons, "_chat", lambda *a, **k: _reply(
            {"concrete": "改了 a.py", "strategy": "先加前缀", "principle": "边界要显式"}))
        got = cons.abstract_trajectory("x" * 500, task="任务甲")
        assert got["concrete"] == "改了 a.py"
        assert got["strategy"] == "先加前缀"
        assert got["principle"] == "边界要显式"
        assert got["model"] == "m1" and got["ts"] > 0

    def test_empty_trajectory_returns_none(self, monkeypatch):
        monkeypatch.setattr(cons, "_chat",
                            lambda *a, **k: pytest.fail("空轨迹不该调模型"))
        assert cons.abstract_trajectory("") is None
        assert cons.abstract_trajectory("   ") is None

    def test_no_api_returns_none(self, monkeypatch):
        monkeypatch.setattr(cons, "_pick_api", lambda: ("", "", ""))
        assert cons.abstract_trajectory("x" * 100) is None

    def test_missing_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(cons, "_pick_api", lambda: ("m1", "NOT_SET_ANYWHERE", ""))
        assert cons.abstract_trajectory("x" * 100) is None

    def test_unparseable_reply_returns_none(self, monkeypatch):
        monkeypatch.setattr(cons, "_chat", lambda *a, **k: _reply("模型今天不想输出 JSON"))
        assert cons.abstract_trajectory("x" * 100) is None

    def test_all_empty_levels_returns_none(self, monkeypatch):
        monkeypatch.setattr(cons, "_chat",
                            lambda *a, **k: _reply({"concrete": "", "strategy": "", "principle": ""}))
        assert cons.abstract_trajectory("x" * 100) is None

    def test_nested_json_is_parsed(self, monkeypatch):
        """返回里除了那三层还夹别的对象 —— 用 [^}]+ 的正则会切坏。"""
        monkeypatch.setattr(cons, "_chat", lambda *a, **k: _reply(
            '```json\n{"concrete":"c","strategy":"s","principle":"p",'
            '"extra":{"nested":1}}\n```'))
        got = cons.abstract_trajectory("x" * 100)
        assert got["concrete"] == "c"

    def test_prompt_carries_task_and_stages(self, monkeypatch):
        seen = {}

        def _spy(base, key, model, prompt, max_tokens, timeout=60.0):
            seen["prompt"] = prompt
            seen["max_tokens"] = max_tokens
            return _reply({"concrete": "c", "strategy": "s", "principle": "p"})

        monkeypatch.setattr(cons, "_chat", _spy)
        cons.abstract_trajectory("x" * 100, task="实现 count_chunks",
                                 tool_seq=[{"tool": "read_file", "elapsed": 0.1},
                                           {"tool": "write_file", "elapsed": 0.2}])
        assert "实现 count_chunks" in seen["prompt"]
        assert "定位" in seen["prompt"], "动作分段要带进 prompt"
        assert seen["max_tokens"] >= 2000, "思考也吃预算，太小会返回空 content"


class TestBackfill:
    def test_only_untouched_nodes_and_respects_limit(self, monkeypatch, tmp_path):
        from singularity.scheduler import _memory_core as mc
        ev = {}
        for i in range(4):
            n = mc.EventNode(task_id=f"t{i}", content=f"任务{i}", timestamp=1000.0 - i,
                             emb=[], attrs={}, trajectory=f"轨迹{i}" * 50)
            ev[f"t{i}"] = n
        monkeypatch.setattr(cons, "_load_events", lambda: ev)
        done = {}
        monkeypatch.setattr(cons, "update_attrs",
                            lambda tid, **kw: done.__setitem__(tid, kw))

        calls = []
        monkeypatch.setattr(cons, "abstract_trajectory",
                            lambda *a, **k: (calls.append(1), {"concrete": "c"})[1])

        got = cons.backfill_abstractions(limit=2)
        assert got == 2, "limit 要生效"
        assert len(calls) == 2
        assert all("abstraction" in v for v in done.values())

    def test_nodes_without_trajectory_are_skipped(self, monkeypatch, tmp_path):
        from singularity.scheduler import _memory_core as mc
        ev = {"a": mc.EventNode(task_id="a", content="x", timestamp=1.0, emb=[], attrs={},
                                trajectory=""),
              "b": mc.EventNode(task_id="b", content="y", timestamp=2.0, emb=[], attrs={},
                                trajectory="有轨迹")}
        monkeypatch.setattr(cons, "_load_events", lambda: ev)
        monkeypatch.setattr(cons, "update_attrs", lambda tid, **kw: None)
        monkeypatch.setattr(cons, "abstract_trajectory", lambda *a, **k: {"concrete": "c"})
        assert cons.backfill_abstractions(limit=5) == 1

    def test_already_abstracted_not_redone(self, monkeypatch):
        from singularity.scheduler import _memory_core as mc
        ev = {"a": mc.EventNode(task_id="a", content="x", timestamp=1.0, emb=[],
                                attrs={"abstraction": {"concrete": "已有"}},
                                trajectory="有轨迹")}
        monkeypatch.setattr(cons, "_load_events", lambda: ev)
        monkeypatch.setattr(cons, "abstract_trajectory",
                            lambda *a, **k: pytest.fail("已经抽象过的不该重做"))
        assert cons.backfill_abstractions(limit=5) == 0


class TestExpansionPrefersAbstraction:
    """展开时：有分层摘要就用它（~330 字），没有才退回原文（3000 字）。"""

    def _item(self, tid, score, **kw):
        base = {"task_id": tid, "description": f"任务{tid}", "score": score,
                "tool_seq": [], "timestamp": 0}
        base.update(kw)
        return base

    def test_prefers_abstraction_over_raw(self):
        out = mg.synthesize([
            self._item("t1", 1.0, trajectory="x" * 9000,
                       abstraction={"concrete": "c", "strategy": "s", "principle": "p"}),
            self._item("t2", 0.9, trajectory="y" * 9000),
        ], "任务", include_full=True)
        nar = out["narrative"]
        assert nar[0]["from_abstraction"] is True
        assert "套路：s" in nar[0]["full_text"]
        assert "from_abstraction" not in nar[1], "没抽象的走原文那条路"
        assert len(nar[1]["full_text"]) == mg.EXPAND_CHARS

    def test_candidate_material_never_leaks_out(self):
        out = mg.synthesize([
            self._item("t1", 1.0, trajectory="x" * 100,
                       abstraction={"concrete": "c", "strategy": "s", "principle": "p"}),
            self._item("t2", 0.9, trajectory="y" * 100),
        ], "任务", include_full=True)
        for it in out["narrative"]:
            for k in ("trajectory", "tool_seq", "abstraction"):
                assert k not in it, f"{k} 不该跟着结果往外传"

    def test_default_still_does_not_expand(self):
        out = mg.synthesize([
            self._item("t1", 1.0, trajectory="x" * 100,
                       abstraction={"concrete": "c", "strategy": "s", "principle": "p"}),
        ], "任务")
        assert "full_text" not in out["narrative"][0]
