"""api_store 测试 — API Key 库的 CRUD 和状态管理。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scheduler import api_store


class TestAPIStoreCRUD:
    def test_seed_creates_default_entries(self):
        """首次加载应自动从 agents.toml + 环境变量探活建库。"""
        entries = api_store.list_all()
        # 至少应有 deepseek / zhipu / kimi 之一
        assert len(entries) >= 1
        for e in entries.values():
            assert e.id
            assert e.provider
            assert e.base_url or e.api_key_env
            assert e.status in ("active", "disabled")

    def test_add_and_get(self):
        api_store.add("test-api", "TestCorp",
                      "https://api.test.com/v1", "TEST_KEY", "测试用")
        entry = api_store.get("test-api")
        assert entry is not None
        assert entry.provider == "TestCorp"
        assert entry.base_url == "https://api.test.com/v1"
        assert entry.notes == "测试用"

    def test_remove(self):
        api_store.add("remove-me", "X", "https://x.com", "X_KEY")
        assert api_store.get("remove-me") is not None
        assert api_store.remove("remove-me")
        assert api_store.get("remove-me") is None

    def test_remove_nonexistent(self):
        assert not api_store.remove("does-not-exist")

    def test_set_status(self):
        api_store.add("status-test", "X", "https://x.com", "X_KEY")
        entry = api_store.set_status("status-test", "quota_exhausted",
                                       "额度用完了")
        assert entry.status == "quota_exhausted"
        assert entry.notes == "额度用完了"
        api_store.remove("status-test")

    def test_set_status_nonexistent(self):
        assert api_store.set_status("no-such", "disabled") is None

    def test_is_available_with_key(self, monkeypatch):
        monkeypatch.setenv("TEST_AVAIL_KEY", "sk-real")
        api_store.add("avail-test", "X", "https://x.com", "TEST_AVAIL_KEY")
        assert api_store.is_available("avail-test")
        api_store.remove("avail-test")

    def test_is_available_without_key(self, monkeypatch):
        monkeypatch.delenv("NO_SUCH_KEY", raising=False)
        api_store.add("no-key-test", "X", "https://x.com", "NO_SUCH_KEY")
        # status 可能是 active (自动设置) 但 key 不存在
        assert not api_store.is_available("no-key-test")
        api_store.remove("no-key-test")

    def test_is_available_when_disabled(self, monkeypatch):
        monkeypatch.setenv("DISABLED_KEY", "sk-xxx")
        api_store.add("disabled-test", "X", "https://x.com", "DISABLED_KEY")
        api_store.set_status("disabled-test", "disabled")
        assert not api_store.is_available("disabled-test")
        api_store.remove("disabled-test")

    def test_available_apis(self, monkeypatch):
        monkeypatch.setenv("AVAIL_KEY_1", "sk-1")
        monkeypatch.setenv("AVAIL_KEY_2", "sk-2")
        monkeypatch.delenv("UNAVAIL_KEY", raising=False)
        api_store.add("avail1", "X", "https://x.com", "AVAIL_KEY_1")
        api_store.add("avail2", "Y", "https://y.com", "AVAIL_KEY_2")
        api_store.add("unavail", "Z", "https://z.com", "UNAVAIL_KEY")
        available = api_store.available_apis()
        ids = {e.id for e in available}
        assert "avail1" in ids
        assert "avail2" in ids
        assert "unavail" not in ids
        for eid in ["avail1", "avail2", "unavail"]:
            api_store.remove(eid)
