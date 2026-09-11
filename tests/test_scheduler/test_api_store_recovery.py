"""provider 标记的半开恢复（2026-09-11 审计）。

**这个 bug**：`is_available` 是 `entry.status != "active" → False` —— 一次 429/欠费
把 provider 永久摘出池子。而恢复路径全断：

- 前端没接 `setStatus`（api.ts 只有 add/delete/scan），CLI 也没有 → 只能手敲 curl
- 自动发现恢复也没戏：非 active 的 provider 之后没人再问它

实测线上：智谱被一次 `http429`（body 含"余额不足"）标成 `quota_exhausted` 后一直没恢复。

修法：冷却期后**半开**放行一次（与 `_model_breaker` 同思路）——
真欠费会被 `note_api_error` 重新标记、冷却重新计时；只是被偶发限流误伤的则自然回归。
`disabled` 是**人工显式关闭**，不自动放行（别覆盖用户意图）。

**在旧代码上会红、且红得对**（断言失败）：`test_recovers_after_cooldown` 会因
永远返回 False 而失败。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from singularity.scheduler import api_store as A          # noqa: E402
from singularity.scheduler import config as cfg           # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "QIDIAN_DIR", tmp_path)
    monkeypatch.setenv("FAKE_KEY", "x")
    for pid in ("p_active", "p_just_marked", "p_old_marked", "p_disabled", "p_nokey"):
        A.add(pid, "P", "http://x", "FAKE_KEY")
    A.set_status("p_just_marked", "quota_exhausted")
    A.set_status("p_old_marked", "quota_exhausted")
    A.set_status("p_disabled", "disabled")
    ents = A._load()
    ents["p_old_marked"].updated_at = time.time() - (A._RECOVERY_COOLDOWN + 60)
    A._save(ents)
    return A


class TestProviderRecovery:
    def test_active_is_available(self, store):
        assert store.is_available("p_active") is True

    def test_just_marked_stays_out(self, store):
        """冷却期内不放行 —— 否则刚欠费就又被撞一次。"""
        assert store.is_available("p_just_marked") is False

    def test_recovers_after_cooldown(self, store):
        """核心回归：过冷却期后半开放行（旧代码这里永远 False）。"""
        assert store.is_available("p_old_marked") is True, \
            "被标记的 provider 永不恢复 —— 一次偶发限流 = 永久摘除"

    def test_disabled_never_auto_recovers(self, store):
        """人工显式关闭的不能自动放行，否则覆盖了用户意图。"""
        ents = store._load()
        ents["p_disabled"].updated_at = time.time() - 10 * store._RECOVERY_COOLDOWN
        store._save(ents)
        assert store.is_available("p_disabled") is False

    def test_missing_key_still_unavailable(self, store):
        store.set_status("p_nokey", "active")
        ents = store._load()
        ents["p_nokey"].api_key_env = "DEFINITELY_NOT_SET_XYZ"
        store._save(ents)
        assert store.is_available("p_nokey") is False

    def test_unknown_id(self, store):
        assert store.is_available("nope") is False


class TestModelLevelAvailability:
    """provider 欠费**不该连坐**同厂商还能用的模型。

    实测线上：智谱 8 个付费模型欠费 → 整个 provider 被标死 → `glm-4.7`
    （同账号、明明能调）也一起从候选链消失，而且**没有任何提示** ——
    表现就是"怎么又少了几个模型"。

    provider 级熔断对"账号级配额"（DeepSeek 那种）是对的；对"同一账号混着
    免费和付费模型"的厂商就是一刀切错杀。所以调度改看模型级。
    """

    def _mark_provider(self, store, monkeypatch, provider="p_just_marked"):
        from singularity.scheduler import model_registry as mr
        monkeypatch.setattr(mr, "provider_for_model", lambda m: provider)

    def test_sibling_survives_when_only_one_model_is_out_of_quota(self, store, monkeypatch):
        self._mark_provider(store, monkeypatch)
        store.note_api_error("paid-model", 400, '{"code":"1113","message":"余额不足"}')

        assert store.is_model_available("paid-model") is False, "欠费的那个该被跳过"
        assert store.is_model_available("free-model") is True, \
            "同一个 provider 下没欠费的模型被连坐踢掉 —— 正是这条修掉的"
        # provider 状态仍然标记：界面要显示"这个厂商欠费了"，只是调度不再据它连坐
        assert store.is_available("p_just_marked") is False

    def test_model_recovers_after_cooldown(self, store, monkeypatch):
        self._mark_provider(store, monkeypatch)
        store.note_api_error("paid-model", 400, "余额不足")
        assert store.is_model_available("paid-model") is False

        data = store._load_raw()
        data[store._QUOTA_DEAD_KEY]["paid-model"] = time.time() - (store._RECOVERY_COOLDOWN + 60)
        store._store_path().write_text(json.dumps(data))

        assert store.is_model_available("paid-model") is True, "充值后不该还要等重启"

    def test_no_key_blocks_every_model_of_that_provider(self, store, monkeypatch):
        """没有 key 是厂商级事实 —— 这种时候仍然要挡，别浪费调用。"""
        self._mark_provider(store, monkeypatch, provider="p_nokey")
        ents = store._load()
        ents["p_nokey"].api_key_env = "DEFINITELY_NOT_SET_XYZ"
        store._save(ents)
        assert store.is_model_available("any-model-of-it") is False

    def test_disabled_provider_blocks(self, store, monkeypatch):
        """人工显式关闭的 provider，其下所有模型都挡。"""
        self._mark_provider(store, monkeypatch, provider="p_disabled")
        assert store.is_model_available("any-model-of-it") is False
