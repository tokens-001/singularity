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
