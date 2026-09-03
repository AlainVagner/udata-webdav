"""Tests for the provider's virtual-tree path resolution and collections."""

import dataprovider as dp
from helpers import make_dataset, make_environ, make_resource


def _provider_with(monkeypatch, org_data, datasets_by_org, dataset_by_slug):
    """Build a DataPublicLuProvider whose API calls are stubbed out.

    org_data            : list of org dicts (each has slug + optional metrics)
    datasets_by_org     : dict slug -> list of dataset dicts
    dataset_by_slug     : dict slug -> full dataset dict (with 'resources')
    """
    monkeypatch.setattr(dp, "_paginate", _make_paginate(org_data, datasets_by_org))
    monkeypatch.setattr(dp, "_http_get_json", _make_get_json(dataset_by_slug))
    return dp.DataPublicLuProvider(cache_ttl=300.0)


def _make_paginate(org_data, datasets_by_org):
    def fake_paginate(url):
        if "/datasets/" in url:
            org_slug = url.split("/organizations/")[1].split("/datasets/")[0]
            yield from datasets_by_org.get(org_slug, [])
        else:
            yield from org_data

    return fake_paginate


def _make_get_json(dataset_by_slug):
    def fake_get_json(url):
        if "/datasets/" in url and url.endswith("/"):
            slug = url.split("/datasets/")[1].strip("/").split("/")[0]
            return dataset_by_slug.get(slug, {})
        raise AssertionError(f"Unexpected API URL: {url}")

    return fake_get_json


class TestProviderPathResolution:
    def test_root_returns_root_collection(self, monkeypatch):
        p = _provider_with(monkeypatch, [], {}, {})
        res = p.get_resource_inst("", make_environ(p))
        assert isinstance(res, dp.RootCollection)

    def test_org_level(self, monkeypatch):
        orgs = [{"slug": "org-a", "metrics": {"datasets": 1}}]
        p = _provider_with(monkeypatch, orgs, {"org-a": []}, {})
        res = p.get_resource_inst("/org-a", make_environ(p))
        assert isinstance(res, dp.OrgCollection)

    def test_unknown_org_returns_none(self, monkeypatch):
        p = _provider_with(monkeypatch, [], {}, {})
        assert p.get_resource_inst("/nope", make_environ(p)) is None

    def test_dataset_level(self, monkeypatch):
        orgs = [{"slug": "org-a", "metrics": {"datasets": 1}}]
        ds = make_dataset()
        p = _provider_with(
            monkeypatch, orgs, {"org-a": [ds]}, {ds["slug"]: ds}
        )
        res = p.get_resource_inst("/org-a/dataset-1", make_environ(p))
        assert isinstance(res, dp.DatasetCollection)

    def test_readme_at_deepest_level(self, monkeypatch):
        ds = make_dataset()
        p = _provider_with(
            monkeypatch, [], {}, {ds["slug"]: ds}
        )
        res = p.get_resource_inst("/org-a/dataset-1/README.txt", make_environ(p))
        assert isinstance(res, dp.DatasetReadme)

    def test_regular_file(self, monkeypatch):
        ds = make_dataset(
            resources=[make_resource(url="https://e.com/data.csv", id="r1")]
        )
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        res = p.get_resource_inst("/org-a/dataset-1/data.csv", make_environ(p))
        assert isinstance(res, dp.FileResource)
        assert res.filename == "data.csv"

    def test_remote_file_exposed_as_shortcut(self, monkeypatch):
        remote = make_resource(
            url="https://external.com/x.pdf", filetype="remote", id="r1"
        )
        ds = make_dataset(resources=[remote])
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        res = p.get_resource_inst("/org-a/dataset-1/x.pdf.url", make_environ(p))
        assert isinstance(res, dp.UrlShortcut)

    def test_remote_raw_file_not_directly_reachable(self, monkeypatch):
        remote = make_resource(
            url="https://external.com/x.pdf", filetype="remote", id="r1"
        )
        ds = make_dataset(resources=[remote])
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        assert p.get_resource_inst("/org-a/dataset-1/x.pdf", make_environ(p)) is None

    def test_unknown_file_returns_none(self, monkeypatch):
        ds = make_dataset(resources=[make_resource(url="https://e.com/a.csv")])
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        assert p.get_resource_inst("/org-a/dataset-1/nope.csv", make_environ(p)) is None

    def test_file_access_reuses_cached_org_listing(self, monkeypatch):
        # When the org-datasets listing is cached and carries the dataset's
        # resources, a direct file access reuses it instead of issuing a
        # redundant full GET /datasets/<slug>.
        res = make_resource(url="https://e.com/a.csv", id="r1")
        ds = make_dataset(slug="dataset-1", resources=[res])
        p = _provider_with(monkeypatch, [], {"org-a": [ds]}, {ds["slug"]: ds})
        # Prime the org listing cache, then make the full dataset fetch fail.
        p._cache.set("org:org-a:datasets", [ds])

        def boom(url):
            raise AssertionError("dataset() should not be called")

        monkeypatch.setattr(dp, "_http_get_json", boom)
        res_file = p.get_resource_inst(
            "/org-a/dataset-1/a.csv", make_environ(p)
        )
        assert isinstance(res_file, dp.FileResource)

    def test_deeper_paths_return_none(self, monkeypatch):
        ds = make_dataset()
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        assert p.get_resource_inst("/a/b/c/d", make_environ(p)) is None

    def test_is_readonly(self, monkeypatch):
        p = _provider_with(monkeypatch, [], {}, {})
        assert p.is_readonly() is True


class TestRootCollection:
    def test_lists_orgs_with_datasets(self, monkeypatch):
        orgs = [
            {"slug": "with-data", "metrics": {"datasets": 3}},
            {"slug": "no-count", "metrics": {"datasets": 0}},
        ]
        datasets_by_org = {"with-data": [make_dataset()]}
        p = _provider_with(monkeypatch, orgs, datasets_by_org, {})
        root = dp.RootCollection("", make_environ(p), p)
        names = root.get_member_names()
        assert names == ["with-data"]

    def test_empty_orgs_checked_live(self, monkeypatch):
        # metrics count is 0/unknown -> must be confirmed via live API call.
        orgs = [
            {"slug": "empty", "metrics": {"datasets": 0}},
            {"slug": "has", "metrics": {"datasets": 0}},
        ]
        datasets_by_org = {"has": [make_dataset()], "empty": []}
        p = _provider_with(monkeypatch, orgs, datasets_by_org, {})
        root = dp.RootCollection("", make_environ(p), p)
        assert root.get_member_names() == ["has"]

    def test_cached_org_datasets_reused_not_refetched(self, monkeypatch):
        # A zero-count org whose dataset listing is already in the provider
        # cache should be resolved without a fresh API call.
        orgs = [{"slug": "cached-org", "metrics": {"datasets": 0}}]
        ds = make_dataset()
        p = _provider_with(monkeypatch, orgs, {"cached-org": [ds]}, {})
        # Prime the provider cache as if the data were already fetched.
        p._cache.set("orgs", orgs)
        p._cache.set("org:cached-org:datasets", [ds])

        # Invalidate the underlying fake paginate so any API call would fail.
        def boom(url):
            raise AssertionError("API should not be called")

        monkeypatch.setattr(dp, "_paginate", boom)

        root = dp.RootCollection("", make_environ(p), p)
        assert root.get_member_names() == ["cached-org"]

    def test_parallel_checks_bounded_and_reuse_cache(self, monkeypatch):
        # Many zero/unknown-count orgs must be resolved concurrently but without
        # exceeding the worker cap, and pre-cached orgs must NOT be re-fetched.
        import threading

        orgs = [
            {"slug": f"org-{i}", "metrics": {"datasets": 0}} for i in range(8)
        ]
        # Only org-0 and org-1 have datasets (tested live); org-2 is pre-cached.
        datasets_by_org = {"org-0": [make_dataset()], "org-1": [make_dataset()]}
        cached_org = make_dataset()
        state = {
            "active": 0,
            "peak": 0,
            "lock": threading.Lock(),
            "org2_calls": 0,
        }

        def fake_paginate(url):
            if "/organizations/" in url and "/datasets/" not in url:
                yield from orgs
                return
            # A per-org dataset listing (live API call for zero-count orgs).
            org_slug = url.split("/organizations/")[1].split("/datasets/")[0]
            if org_slug == "org-2":
                state["org2_calls"] += 1
            with state["lock"]:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                # Simulate slow upstream so concurrency is actually exercised.
                threading.Event().wait(0.02)
                yield from datasets_by_org.get(org_slug, [])
            finally:
                with state["lock"]:
                    state["active"] -= 1

        monkeypatch.setattr(dp, "_paginate", fake_paginate)
        p = dp.DataPublicLuProvider(cache_ttl=300.0, root_workers=3)
        root = dp.RootCollection("", make_environ(p), p)
        # Pre-cache one org's listing and the orgs list itself.
        p._cache.set("orgs", orgs)
        p._cache.set("org:org-2:datasets", [cached_org])

        names = root.get_member_names()
        assert "org-0" in names and "org-1" in names and "org-2" in names
        assert "org-3" not in names
        # The live checks never exceed the configured worker cap (3).
        assert state["peak"] <= 3
        # The pre-cached org-2 listing is reused and never re-fetched live.
        assert state["org2_calls"] == 0


class TestOrgCollection:
    def test_filters_datasets_without_resources(self, monkeypatch):
        ds_with = make_dataset(
            slug="has-file",
            resources=[make_resource(url="https://e.com/a.csv", id="r1")],
        )
        ds_empty = make_dataset(slug="no-file", resources=[])
        p = _provider_with(
            monkeypatch, [], {"org-a": [ds_empty, ds_with]}, {}
        )
        org = dp.OrgCollection("/org-a", make_environ(p), p, {"slug": "org-a"})
        assert org.get_member_names() == ["has-file"]

    def test_display_name_from_org(self, monkeypatch):
        p = _provider_with(monkeypatch, [], {}, {})
        org = dp.OrgCollection(
            "/org-a", make_environ(p), p, {"slug": "org-a", "name": "Org A"}
        )
        assert org.get_display_name() == "Org A"


class TestSingleFlight:
    def test_concurrent_org_datasets_share_one_fetch(self, monkeypatch):
        # Two threads asking for the same org in parallel must trigger only a
        # single upstream pagination, not one per caller.
        ds = make_dataset(resources=[make_resource()])
        org_slug = "org-a"

        calls = {"n": 0}
        import threading

        def slow_paginate(url):
            calls["n"] += 1
            threading.Event().wait(0.05)  # widen the race window
            yield from [ds]

        monkeypatch.setattr(dp, "_paginate", slow_paginate)
        p = dp.DataPublicLuProvider(cache_ttl=300.0)

        results = {}

        def worker():
            results["v"] = p.org_datasets(org_slug)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert calls["n"] == 1, f"expected 1 upstream fetch, got {calls['n']}"
        assert results["v"] == [ds]

    def test_second_call_served_from_cache(self, monkeypatch):
        # After the first fetch, a later access must not hit the API again.
        ds = make_dataset()
        org_slug = "org-a"
        calls = {"n": 0}

        def counting_paginate(url):
            calls["n"] += 1
            yield from [ds]

        monkeypatch.setattr(dp, "_paginate", counting_paginate)
        p = dp.DataPublicLuProvider(cache_ttl=300.0)

        assert p.org_datasets(org_slug) == [ds]
        assert p.org_datasets(org_slug) == [ds]
        assert calls["n"] == 1

    def test_single_flight_lock_expires_after_ttl(self, monkeypatch):
        # Locks unused for longer than the TTL must be dropped, so the map does
        # not grow unboundedly with distinct keys over a long-running server.
        monkeypatch.setattr(dp, "_SINGLE_FLIGHT_TTL", 10.0)
        clock = {"t": 0.0}
        monkeypatch.setattr(dp, "_now", lambda: clock["t"])
        ds = make_dataset()

        def echo(url):
            yield from [ds]

        monkeypatch.setattr(dp, "_paginate", echo)
        p = dp.DataPublicLuProvider(cache_ttl=300.0)

        # Exercise a key -> its single-flight lock entry is created.
        key1 = "org:first:datasets"
        p.org_datasets("first")
        assert key1 in p._single_flight

        # Use a *different* key after advancing the clock past the TTL.  The
        # expired (unused) entry for key1 must be trimmed, so no entry in the
        # map is older than the TTL.
        clock["t"] = 20.0
        p.org_datasets("second")
        assert all(
            (clock["t"] - v[2]) <= dp._SINGLE_FLIGHT_TTL
            for v in p._single_flight.values()
        )
        assert key1 not in p._single_flight

    def test_single_flight_lock_lru_capped(self, monkeypatch):
        # When many keys are visited within the TTL window, the store is capped.
        monkeypatch.setattr(dp, "_SINGLE_FLIGHT_MAX_ITEMS", 2)
        clock = {"t": 0.0}
        monkeypatch.setattr(dp, "_now", lambda: clock["t"])
        ds = make_dataset()

        def echo(url):
            yield from [ds]

        monkeypatch.setattr(dp, "_paginate", echo)
        p = dp.DataPublicLuProvider(cache_ttl=300.0)

        k1, k2, k3 = "org:k1:datasets", "org:k2:datasets", "org:k3:datasets"
        p.org_datasets("k1")
        p.org_datasets("k2")
        p.org_datasets("k3")
        # Only the capped number of lock entries may remain; the oldest-touched
        # (k1, created first and not re-touched) should have been dropped in
        # favour of k2/k3.
        assert len(p._single_flight) <= 2
        assert k1 not in p._single_flight
        assert k2 in p._single_flight
        assert k3 in p._single_flight


class TestDatasetCollection:
    def test_resources_reuse_embedded_list(self, monkeypatch):
        # A dataset object that already carries 'resources' (as the org-datasets
        # listing does) must NOT trigger a separate full dataset() API call.
        res = make_resource(url="https://e.com/local.csv", id="r1")
        ds = make_dataset(resources=[res])

        def boom(url):
            raise AssertionError("dataset() should not be called")

        monkeypatch.setattr(dp, "_http_get_json", boom)
        p = dp.DataPublicLuProvider(cache_ttl=300.0)
        coll = dp.DatasetCollection("/a/b", make_environ(p), p, ds)
        assert coll._resources() == [res]

    def test_member_names_readme_first_with_mixed_resources(self, monkeypatch):
        ds = make_dataset(
            resources=[
                make_resource(url="https://e.com/local.csv", id="r1"),
                make_resource(
                    url="https://external.com/ext.pdf", filetype="remote", id="r2"
                ),
            ]
        )
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        coll = dp.DatasetCollection("/a/b", make_environ(p), p, ds)
        assert coll.get_member_names() == [
            "README.txt",
            "local.csv",
            "ext.pdf.url",
        ]

    def test_get_member_readme(self, monkeypatch):
        ds = make_dataset()
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        coll = dp.DatasetCollection("/a/b", make_environ(p), p, ds)
        member = coll.get_member("README.txt")
        assert isinstance(member, dp.DatasetReadme)

    def test_get_member_remote_shortcut(self, monkeypatch):
        remote = make_resource(
            url="https://external.com/ext.pdf", filetype="remote", id="r2"
        )
        ds = make_dataset(resources=[remote])
        p = _provider_with(monkeypatch, [], {}, {ds["slug"]: ds})
        coll = dp.DatasetCollection("/a/b", make_environ(p), p, ds)
        member = coll.get_member("ext.pdf.url")
        assert isinstance(member, dp.UrlShortcut)