"""test_benchmark.py — 模型基准评测的评分定档 + add_model strengths 透传修复。"""
import pytest
from singularity.scheduler import _benchmark
from singularity.scheduler.model_registry import ModelEntry


def _m(**kw):
    return ModelEntry(id="x", provider="deepseek", display="x", **kw)


def _r(passed, elapsed=10.0, turns=2, label="基础编码"):
    return {"key": "k", "label": label, "passed": passed,
            "elapsed": elapsed, "turns": turns, "error": ""}


class TestSummarize:
    def test_all_pass(self):
        s = _benchmark._summarize([_r(True, 5.0), _r(True, 6.0, 3), _r(True, 7.0)], _m())
        assert s["rating"] == "S"
        assert s["speed"] == "fast"          # avg 6s < 90
        assert len(s["strengths"]) == 3
        assert s["max_turns"] == 5           # max(3, min(8, 3+2))

    def test_two_pass(self):
        s = _benchmark._summarize([_r(True), _r(False), _r(True)], _m())
        assert s["rating"] == "A+"
        assert len(s["strengths"]) == 2

    def test_none_pass_fallback_turns(self):
        # 全失败且 turns 全 0（模型没跑起来）→ 回退原 max_turns
        s = _benchmark._summarize([_r(False, turns=0), _r(False, turns=0), _r(False, turns=0)], _m(max_turns=5))
        assert s["rating"] == "?"
        assert s["strengths"] == []
        assert s["max_turns"] == 5

    def test_slow_speed(self):
        s = _benchmark._summarize([_r(True, 300.0), _r(True, 300.0), _r(True, 300.0)], _m())
        assert s["speed"] == "slow"

    def test_max_turns_clamp(self):
        s = _benchmark._summarize([_r(True, 1.0, 20), _r(True, 1.0, 20), _r(True, 1.0, 20)], _m())
        assert s["max_turns"] == 8           # clamp 上限 8


class TestAddModelStrengths:
    def test_strengths_preserved(self, monkeypatch):
        from singularity.scheduler import model_registry
        store = {}
        monkeypatch.setattr(model_registry, "_load_custom", lambda: store)
        monkeypatch.setattr(model_registry, "_save_custom", lambda c: store.update(c))
        model_registry.add_model("bench-x", "deepseek", strengths=["编码", "调试"])
        assert store["bench-x"].strengths == ["编码", "调试"]

    def test_strengths_default_empty(self, monkeypatch):
        from singularity.scheduler import model_registry
        store = {}
        monkeypatch.setattr(model_registry, "_load_custom", lambda: store)
        monkeypatch.setattr(model_registry, "_save_custom", lambda c: store.update(c))
        model_registry.add_model("bench-y", "deepseek")
        assert store["bench-y"].strengths == []


class TestAppendNote:
    def test_first_run(self):
        assert _benchmark._append_note("", {"n_pass": 3}) == "轻量基准 3/3 通过(非权威)"

    def test_no_duplicate_tag(self):
        # 已有旧标签 → 替换而非追加
        out = _benchmark._append_note("foo | 轻量基准 2/3 通过(非权威)", {"n_pass": 3})
        assert out == "foo | 轻量基准 3/3 通过(非权威)"
