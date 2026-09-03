"""Tests for the pure helper functions in dataprovider.py."""

import threading

import pytest

import dataprovider as dp
from helpers import make_dataset, make_resource


# ---- resource_filename ------------------------------------------------------
class TestResourceFilename:
    def test_uses_last_url_segment(self):
        r = make_resource(url="https://data.public.lu/fr/datasets/r/data.csv")
        assert dp.resource_filename(r) == "data.csv"

    def test_strips_trailing_slash(self):
        r = make_resource(url="https://example.com/data.zip/")
        assert dp.resource_filename(r) == "data.zip"

    def test_url_decoded(self):
        r = make_resource(url="https://example.com/a%20b.txt")
        assert dp.resource_filename(r) == "a b.txt"

    def test_falls_back_to_title(self):
        r = make_resource(url="", title="My file")
        assert dp.resource_filename(r) == "My file"

    def test_falls_back_to_id(self):
        r = make_resource(url="", title="", id="the-id")
        assert dp.resource_filename(r) == "the-id"

    def test_no_url_title_or_id(self):
        r = {"url": "", "title": ""}  # no 'id' key at all -> falls back to 'file'
        assert dp.resource_filename(r) == "file"

    def test_none_id_does_not_crash(self):
        # Regression: a present-but-None id must not produce None that crashes
        # unquote(); it falls back to 'file'.
        r = {"url": "", "title": "", "id": None}
        assert dp.resource_filename(r) == "file"

    def test_none_title_with_id_falls_back_to_id(self):
        r = {"url": "", "title": None, "id": "abc123"}
        assert dp.resource_filename(r) == "abc123"

    def test_whitespace_title_ignored(self):
        r = {"url": "", "title": "   ", "id": "id-x"}
        assert dp.resource_filename(r) == "id-x"

    def test_non_string_title_and_id_safe(self):
        r = {"url": "", "title": 123, "id": None}
        assert dp.resource_filename(r) == "file"

    def test_whitespace_url_falls_back(self):
        r = {"url": "   ", "title": "T"}
        assert dp.resource_filename(r) == "T"


# ---- is_remote --------------------------------------------------------------
class TestIsRemote:
    def test_uploaded_is_not_remote(self):
        assert dp.is_remote(make_resource(filetype="file")) is False

    def test_remote_is_remote(self):
        assert dp.is_remote(make_resource(filetype="remote")) is True

    def test_missing_filetype_is_not_remote(self):
        r = make_resource()
        r.pop("filetype")
        assert dp.is_remote(r) is False


# ---- url_shortcut_name ------------------------------------------------------
class TestUrlShortcutName:
    def test_appends_url_suffix(self):
        r = make_resource(url="https://example.com/report.pdf")
        assert dp.url_shortcut_name(r) == "report.pdf.url"

    def test_handles_decoded_name(self):
        r = make_resource(url="https://example.com/a%20b.xlsx")
        assert dp.url_shortcut_name(r) == "a b.xlsx.url"


# ---- find_dict --------------------------------------------------------------
class TestFindDict:
    def test_finds_first_match(self):
        seq = [{"slug": "a"}, {"slug": "b"}, {"slug": "b"}]
        assert dp.find_dict(seq, "slug", "b")["slug"] == "b"

    def test_returns_none_when_missing(self):
        assert dp.find_dict([{"slug": "a"}], "slug", "zzz") is None

    def test_returns_none_for_empty(self):
        assert dp.find_dict([], "slug", "a") is None


# ---- find_resource_by_filename / find_remote_by_shortcut --------------------
class TestFindResource:
    def test_finds_by_filename(self):
        csv = make_resource(url="https://e.com/data.csv")
        pdf = make_resource(url="https://e.com/report.pdf", id="r2")
        found = dp.find_resource_by_filename([csv, pdf], "report.pdf")
        assert found is pdf

    def test_returns_none_when_missing(self):
        csv = make_resource(url="https://e.com/data.csv")
        assert dp.find_resource_by_filename([csv], "nope.csv") is None

    def test_remote_shortcut_lookup(self):
        remote = make_resource(
            url="https://external.com/x.pdf", filetype="remote"
        )
        assert dp.find_remote_by_shortcut([remote], "x.pdf.url") is remote

    def test_remote_shortcut_ignores_non_remote(self):
        local = make_resource(url="https://e.com/x.pdf", id="r1")
        assert dp.find_remote_by_shortcut([local], "x.pdf.url") is None


# ---- iso_to_epoch -----------------------------------------------------------
class TestIsoToEpoch:
    def test_converts(self):
        # 2024-01-02T03:04:05 in the local timezone.
        assert dp.iso_to_epoch("2024-01-02T03:04:05") is not None

    def test_none_for_empty(self):
        assert dp.iso_to_epoch("") is None
        assert dp.iso_to_epoch(None) is None

    def test_none_for_garbage(self):
        assert dp.iso_to_epoch("not-a-date") is None

    def test_non_string_returns_none(self):
        assert dp.iso_to_epoch(123) is None
        assert dp.iso_to_epoch(None) is None
        assert dp.iso_to_epoch(["x"]) is None

    def test_overlarge_returns_none(self):
        # mktime may overflow on some platforms; must be caught, never throw.
        import pytest

        try:
            result = dp.iso_to_epoch("9999-01-01T00:00:00")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"iso_to_epoch raised: {exc!r}")
        assert result is None or isinstance(result, float)


# ---- license_name -----------------------------------------------------------
class TestLicenseName:
    def test_passthrough(self):
        assert dp.license_name("cc-by") == "cc-by"

    def test_strips_whitespace(self):
        assert dp.license_name("  cc-by  ") == "cc-by"

    def test_unknown_when_missing(self):
        assert dp.license_name("") == "Unknown"
        assert dp.license_name(None) == "Unknown"

    def test_unknown_when_whitespace_or_non_string(self):
        assert dp.license_name("   ") == "Unknown"
        assert dp.license_name(123) == "Unknown"


# ---- single-flight ----------------------------------------------------------
class TestSingleFlight:
    def test_concurrent_same_key_single_fetch(self):
        p = dp.DataPublicLuProvider(cache_ttl=300.0)
        state = {"fetches": 0}
        import threading

        def fetch():
            state["fetches"] += 1
            # Simulate a slow upstream request to widen the race window.
            threading.Event().wait(0.05)
            return {"value": 42}

        results, errors = [], []

        def worker():
            try:
                results.append(p._cache_keyed("shared-key", fetch))
            except Exception as exc:  # noqa: BLE001 - surface thread exceptions
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Exactly one upstream fetch for 12 concurrent identical requests.
        assert state["fetches"] == 1
        assert all(r == {"value": 42} for r in results)

    def test_distinct_keys_fetch_independently(self):
        p = dp.DataPublicLuProvider(cache_ttl=300.0)
        state = {"fetches": {"a": 0, "b": 0}}
        import threading

        def make_fetch(label):
            def fetch():
                state["fetches"][label] += 1
                threading.Event().wait(0.02)
                return label
            return fetch

        results, errors = [], []

        def worker(k):
            try:
                results.append(p._cache_keyed(k, make_fetch(k)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        targets = [("a",) * 5 + ("b",) * 5]
        threads = [threading.Thread(target=worker, args=(k,)) for k in targets[0]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # Each key fetched exactly once, even though they share the pool.
        assert state["fetches"]["a"] == 1
        assert state["fetches"]["b"] == 1
        assert sorted(results) == ["a"] * 5 + ["b"] * 5

    def test_cache_disabled_still_serializes(self):
        # With caching disabled (ttl=None) single-flight must still collapse
        # concurrent calls for the same key into a single upstream request.
        p = dp.DataPublicLuProvider(cache_ttl=None)
        state = {"calls": 0, "active": 0, "peak": 0, "lock": threading.Lock()}

        def fetch():
            with state["lock"]:
                state["calls"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            threading.Event().wait(0.02)
            with state["lock"]:
                state["active"] -= 1
            return "done"

        results = []

        def worker():
            results.append(p._cache_keyed("k", fetch))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Serialized: no two fetches run at once (single-flight serializes
        # per-key even when caching is disabled), but because no value is
        # stored each thread still runs the upstream call in turn.
        assert state["calls"] == 8
        assert state["peak"] == 1
        assert results == ["done"] * 8