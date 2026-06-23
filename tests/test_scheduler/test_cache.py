"""TTL Cache tests."""
import time
from singularity.scheduler._cache import TTLStore


class TestTTLCache:
    """内存缓存。"""

    def test_set_get(self):
        c = TTLStore(ttl_seconds=60)
        c.set("k1", {"a": 1})
        assert c.get("k1") == {"a": 1}

    def test_expiry(self):
        c = TTLStore(ttl_seconds=0.01)
        c.set("k1", {"a": 1})
        time.sleep(0.02)
        assert c.get("k1") is None

    def test_invalidate(self):
        c = TTLStore(ttl_seconds=60)
        c.set("k1", {"a": 1})
        c.invalidate("k1")
        assert c.get("k1") is None
