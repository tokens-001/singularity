"""model_registry.py 单元测试 — 两档后 for_phase / provider / fallback 查找逻辑。"""

import pytest
from singularity.scheduler.model_registry import ModelEntry


def _make_entry(mid, provider, recommended_for=None, cost="standard", speed="fast"):
    return ModelEntry(id=mid, provider=provider, display=mid,
                      recommended_for=recommended_for or [], cost=cost, speed=speed)


class TestForPhase:
    def test_filters_by_phase(self, monkeypatch):
        from singularity.scheduler.model_registry import for_phase
        models = {
            "a": _make_entry("a", "openai", ["实现"]),
            "b": _make_entry("b", "anthropic", ["架构"]),
            "c": _make_entry("c", "deepseek", ["实现", "架构"]),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = for_phase("实现")
        mids = [m.id for m in result]
        assert "a" in mids
        assert "c" in mids
        assert "b" not in mids

    def test_empty_phase_returns_all(self, monkeypatch):
        from singularity.scheduler.model_registry import for_phase
        models = {"a": _make_entry("a", "openai", ["实现"]), "b": _make_entry("b", "anthropic", [])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = for_phase("")
        assert len(result) == 2

    def test_excludes_unavailable(self, monkeypatch):
        from singularity.scheduler.model_registry import for_phase
        models = {"a": _make_entry("a", "openai", ["实现"])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available",
            lambda p: p != "openai")

        result = for_phase("实现", available_only=True)
        assert len(result) == 0

    def test_sorts_by_cost(self, monkeypatch):
        from singularity.scheduler.model_registry import for_phase
        models = {
            "prem": _make_entry("prem", "openai", ["实现"], cost="premium"),
            "budget": _make_entry("budget", "deepseek", ["实现"], cost="budget"),
            "std": _make_entry("std", "anthropic", ["实现"], cost="standard"),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = for_phase("实现")
        costs = [m.cost for m in result]
        assert costs == ["budget", "standard", "premium"]


class TestProviderForModel:
    def test_known_model(self, monkeypatch):
        from singularity.scheduler.model_registry import provider_for_model
        models = {"gpt-4": _make_entry("gpt-4", "openai", ["实现"])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        assert provider_for_model("gpt-4") == "openai"

    def test_unknown_model(self, monkeypatch):
        from singularity.scheduler.model_registry import provider_for_model
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: {})
        assert provider_for_model("nonexistent") == ""


class TestModelsForProvider:
    def test_filters_by_provider(self, monkeypatch):
        from singularity.scheduler.model_registry import models_for_provider
        models = {
            "a": _make_entry("a", "openai", ["实现"]),
            "b": _make_entry("b", "anthropic", ["架构"]),
            "c": _make_entry("c", "openai", ["交付"]),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        result = models_for_provider("openai")
        assert len(result) == 2


class TestFallbackForTier:
    def test_excludes_provider(self, monkeypatch):
        from singularity.scheduler.model_registry import fallback_for_tier
        models = {
            "a": _make_entry("a", "openai", ["实现"], cost="budget"),
            "b": _make_entry("b", "anthropic", ["实现"], cost="standard"),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = fallback_for_tier("实现", exclude_providers={"openai"})
        assert len(result) == 1
        assert result[0].id == "b"

    def test_all_excluded_returns_empty(self, monkeypatch):
        from singularity.scheduler.model_registry import fallback_for_tier
        models = {"a": _make_entry("a", "openai", ["实现"])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = fallback_for_tier("实现", exclude_providers={"openai"})
        assert result == []
