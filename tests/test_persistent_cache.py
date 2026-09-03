"""Tests for PersistentCache (on-disk listing cache) and its integration."""

import json
import os
import time

import dataprovider as dp

import pytest


@pytest.fixture
def cache_file(tmp_path):
    return str(tmp_path / "cache.json")


class TestPersistentCache:
    def test_set_persists_to_disk(self, cache_file):
        c = dp.PersistentCache(ttl=300.0, path=cache_file)
        c.set("orgs", [{"slug": "a"}])
        with open(cache_file, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["orgs"][0] == [{"slug": "a"}]

    def test_new_instance_loads_fresh_entries(self, cache_file):
        c1 = dp.PersistentCache(ttl=300.0, path=cache_file)
        c1.set("org:acme:datasets", [{"slug": "ds1"}])
        # Simulate a restart: a brand-new cache reading the same file.
        c2 = dp.PersistentCache(ttl=300.0, path=cache_file)
        assert c2.get("org:acme:datasets") == [{"slug": "ds1"}]

    def test_expired_entries_not_loaded(self, cache_file):
        # Short TTL, write, then age it out by travelling forward in time.
        c = dp.PersistentCache(ttl=10.0, path=cache_file)
        c.set("k", "v")
        # Rewrite the file as if it were written long ago (TTL already lapsed).
        old_set_at = time.time() - 9999.0
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump({"k": ["v", old_set_at]}, fh)
        c2 = dp.PersistentCache(ttl=10.0, path=cache_file)
        assert c2.get("k") is None

    def test_corrupt_file_does_not_crash(self, cache_file):
        with open(cache_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        c = dp.PersistentCache(ttl=300.0, path=cache_file)
        assert c.get("k") is None  # no crash; starts empty

    def test_missing_file_starts_empty(self, cache_file):
        c = dp.PersistentCache(ttl=300.0, path=cache_file)
        assert c.get("k") is None

    def test_clear_removes_file(self, cache_file):
        c = dp.PersistentCache(ttl=300.0, path=cache_file)
        c.set("k", "v")
        assert os.path.exists(cache_file)
        c.clear()
        assert not os.path.exists(cache_file)

    def test_persist_disabled_writes_nothing(self, cache_file):
        c = dp.PersistentCache(ttl=300.0, path=cache_file, persist=False)
        c.set("k", "v")
        assert not os.path.exists(cache_file)

    def test_no_path_does_not_write(self, tmp_path):
        c = dp.PersistentCache(ttl=300.0, path=None)
        c.set("k", "v")
        assert not os.path.exists(str(tmp_path / "cache.json"))


class TestProviderPersistence:
    def test_provider_uses_persistent_cache_when_path_given(
        self, tmp_path
    ):
        cache_file = str(tmp_path / "c.json")
        p = dp.DataPublicLuProvider(
            cache_ttl=300.0, cache_path=cache_file
        )
        assert isinstance(p._cache, dp.PersistentCache)

    def test_provider_uses_tty_cache_without_path(self):
        p = dp.DataPublicLuProvider(cache_ttl=300.0)
        assert not isinstance(p._cache, dp.PersistentCache)