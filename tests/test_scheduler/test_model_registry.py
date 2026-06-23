"""model_registry.py 单元测试 — for_tier / provider_for_model / fallback 查找逻辑。"""

import pytest
from singularity.scheduler.model_registry import ModelEntry


def _make_entry(mid, provider, tiers, cost="standard", speed="fast"):
    return ModelEntry(id=mid, provider=provider, display=mid, tiers=tiers, cost=cost, speed=speed)


class TestForTier:
    def test_filters_by_tier(self, monkeypatch):
        from singularity.scheduler.model_registry import for_tier
        models = {
            "a": _make_entry("a", "openai", ["E"]),
            "b": _make_entry("b", "anthropic", ["D"]),
            "c": _make_entry("c", "deepseek", ["E", "D"]),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = for_tier("E")
        mids = [m.id for m in result]
        assert "a" in mids
        assert "c" in mids
        assert "b" not in mids

    def test_excludes_unavailable(self, monkeypatch):
        from singularity.scheduler.model_registry import for_tier
        models = {"a": _make_entry("a", "openai", ["E"])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available",
            lambda p: p != "openai")

        result = for_tier("E", available_only=True)
        assert len(result) == 0

    def test_sorts_by_cost(self, monkeypatch):
        from singularity.scheduler.model_registry import for_tier
        models = {
            "prem": _make_entry("prem", "openai", ["E"], cost="premium"),
            "budget": _make_entry("budget", "deepseek", ["E"], cost="budget"),
            "std": _make_entry("std", "anthropic", ["E"], cost="standard"),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = for_tier("E")
        costs = [m.cost for m in result]
        assert costs == ["budget", "standard", "premium"]


class TestProviderForModel:
    def test_known_model(self, monkeypatch):
        from singularity.scheduler.model_registry import provider_for_model
        models = {"gpt-4": _make_entry("gpt-4", "openai", ["E"])}
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
            "a": _make_entry("a", "openai", ["E"]),
            "b": _make_entry("b", "anthropic", ["D"]),
            "c": _make_entry("c", "openai", ["E+"]),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        result = models_for_provider("openai")
        assert len(result) == 2


class TestFallbackForTier:
    def test_excludes_provider(self, monkeypatch):
        from singularity.scheduler.model_registry import fallback_for_tier
        models = {
            "a": _make_entry("a", "openai", ["E"], cost="budget"),
            "b": _make_entry("b", "anthropic", ["E"], cost="standard"),
        }
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = fallback_for_tier("E", exclude_providers={"openai"})
        assert len(result) == 1
        assert result[0].id == "b"

    def test_all_excluded_returns_empty(self, monkeypatch):
        from singularity.scheduler.model_registry import fallback_for_tier
        models = {"a": _make_entry("a", "openai", ["E"])}
        monkeypatch.setattr("singularity.scheduler.model_registry.load_models", lambda: models)
        monkeypatch.setattr("singularity.scheduler.api_store.is_available", lambda p: True)

        result = fallback_for_tier("E", exclude_providers={"openai"})
        assert result == []
