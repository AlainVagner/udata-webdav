"""Tests for the TTLCache class."""

import dataprovider as dp


class TestTTLCache:
    def test_get_missing_returns_none(self):
        c = dp.TTLCache(ttl=1.0)
        assert c.get("nope") is None

    def test_set_and_get(self):
        c = dp.TTLCache(ttl=1.0)
        c.set("k", "v")
        assert c.get("k") == "v"

    def test_expiry(self, monkeypatch):
        # A controllable clock; must be in place before set() so the recorded
        # expiration uses the same time base lookups use.
        clock = {"now": 0.0}

        def fake_now():
            return clock["now"]

        monkeypatch.setattr("dataprovider.time.monotonic", fake_now)
        c = dp.TTLCache(ttl=1.0)
        c.set("k", "v")
        clock["now"] = 0.5
        assert c.get("k") == "v"  # still fresh
        clock["now"] = 1.0  # exactly at expiry -> fresh (expires strictly after)
        assert c.get("k") == "v"
        clock["now"] = 1.0 + 1e-9  # just past expiry
        assert c.get("k") is None

    def test_expired_entry_is_removed(self, monkeypatch):
        clock = {"now": 0.0}
        monkeypatch.setattr(
            "dataprovider.time.monotonic", lambda: clock["now"]
        )
        c = dp.TTLCache(ttl=1.0)
        c.set("k", "v")
        clock["now"] = 5.0
        assert c.get("k") is None
        assert "k" not in c._store

    def test_clear(self):
        c = dp.TTLCache(ttl=1.0)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert c.get("a") is None
        assert c.get("b") is None

    def test_ttl_none_disables_cache(self):
        c = dp.TTLCache(ttl=None)
        c.set("k", "v")
        assert c.get("k") is None
        assert c._store == {}

    def test_max_items_evicts_lru(self):
        c = dp.TTLCache(ttl=10.0, max_items=3)
        for i in range(3):
            c.set(f"k{i}", i)
        # Touch k0 so it becomes most-recent, making k1 the LRU.
        assert c.get("k0") == 0
        c.set("k3", 3)  # pushes over cap -> evict k1 (LRU)
        assert c.get("k1") is None
        assert c.get("k0") == 0
        assert c.get("k2") == 2
        assert c.get("k3") == 3

    def test_max_items_with_expiry_keeps_bounded(self):
        c = dp.TTLCache(ttl=1.0, max_items=3)
        for i in range(5):
            c.set(f"k{i}", i)
        assert len(c._store) == 3