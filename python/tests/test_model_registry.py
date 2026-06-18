"""model_registry 测试 — 模型能力查询和容灾备选。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scheduler import model_registry as mr


class TestModelRegistry:
    def test_load_models(self):
        models = mr.load_models()
        assert len(models) >= 4  # flash, pro, kimi, turbo, glm-5.2, opus...
        # 验证必有字段
        for mid, m in models.items():
            assert m.id == mid
            assert m.provider
            assert m.tiers
            assert m.speed in ("fast", "medium", "slow")
            assert m.cost in ("budget", "standard", "premium")

    def test_for_tier_e(self):
        e_models = mr.for_tier("E")
        assert len(e_models) >= 1
        # 按成本排序: budget first
        costs = [m.cost for m in e_models]
        cost_order = {"budget": 0, "standard": 1, "premium": 2}
        for i in range(len(costs) - 1):
            assert cost_order[costs[i]] <= cost_order[costs[i + 1]]

    def test_for_tier_d(self):
        d_models = mr.for_tier("D")
        assert len(d_models) >= 1
        for m in d_models:
            assert "D" in m.tiers

    def test_get_existing(self):
        m = mr.get("deepseek-v4-flash")
        assert m is not None
        assert m.provider == "deepseek"
        assert "E" in m.tiers

    def test_get_nonexistent(self):
        assert mr.get("no-such-model") is None

    def test_provider_for_model(self):
        assert mr.provider_for_model("deepseek-v4-flash") == "deepseek"
        assert mr.provider_for_model("glm-5.2") == "zhipu"
        assert mr.provider_for_model("claude-opus-4-8") == "anthropic"
        assert mr.provider_for_model("no-such") == ""

    def test_models_for_provider(self):
        ds_models = mr.models_for_provider("deepseek")
        assert len(ds_models) >= 1
        for m in ds_models:
            assert m.provider == "deepseek"

    def test_fallback_for_tier_e(self):
        """E 层容灾链: 按价格排序。"""
        # available_only=True 需要 API key，测试环境可能没有
        chain = mr.fallback_for_tier("E")
        assert len(chain) >= 1
        # 容灾链应按成本排序 (预算→标准→高级)
        cost_order = {"budget": 0, "standard": 1, "premium": 2}
        for i in range(len(chain) - 1):
            assert cost_order[chain[i].cost] <= cost_order[chain[i + 1].cost]

    def test_fallback_for_tier_e_excluding(self):
        """排除 deepseek → 容灾链不应包含 deepseek 的模型。"""
        chain = mr.fallback_for_tier("E", exclude_providers={"deepseek"})
        for m in chain:
            assert m.provider != "deepseek"
