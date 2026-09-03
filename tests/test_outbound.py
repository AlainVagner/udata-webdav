"""Tests for outbound requests: default headers / User-Agent."""

import dataprovider as dp


class _FakeSession:
    def __init__(self, fake_get):
        self.get = fake_get


def _patch_session(monkeypatch, fake_get):
    monkeypatch.setattr(dp, "_session", lambda: _FakeSession(fake_get))


def test_default_user_agent_on_json_requests(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _JsonResp({"data": [], "next_page": None})

    _patch_session(monkeypatch, fake_get)
    dp._http_get_json("https://data.public.lu/api/1/organizations/?page_size=100")
    assert "User-Agent" in captured["headers"]
    assert captured["headers"]["User-Agent"].startswith("udata-webdav/")


def test_stream_request_includes_user_agent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, stream=None, timeout=None):
        captured["headers"] = headers
        return _RangeResp(b"abc")

    _patch_session(monkeypatch, fake_get)
    s = dp.UpstreamRangeStream("https://example.com/big.bin")
    s.read()
    assert captured["headers"]["User-Agent"].startswith("udata-webdav/")
    assert captured["headers"].get("Range") == "bytes=0-"


def test_resource_get_includes_user_agent(monkeypatch):
    from unittest.mock import MagicMock

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"data"
        resp.raise_for_status.return_value = None
        return resp

    _patch_session(monkeypatch, fake_get)
    # Exercise FileResource.get_content small-file path.
    res = dp.FileResource(
        "/o/d/f.csv", {"wsgidav.provider": None}, None, "d", "f.csv",
        {"url": "https://e.com/f.csv", "filesize": 4, "mime": "text/csv"},
    )
    r = res.get_content()
    assert r.read() == b"data"
    assert captured["headers"]["User-Agent"].startswith("udata-webdav/")
    assert "Range" not in captured["headers"]


# ---- content cache reuse ----------------------------------------------------
class TestContentCache:
    def _make(self, monkeypatch):
        captured = {"count": 0}

        def fake_get(url, headers=None, timeout=None):
            from unittest.mock import MagicMock

            captured["count"] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"payload"
            resp.raise_for_status.return_value = None
            return resp

        _patch_session(monkeypatch, fake_get)
        provider = dp.DataPublicLuProvider(cache_ttl=300.0)
        res = dp.FileResource(
            "/o/d/f.csv", {"wsgidav.provider": provider}, provider, "d",
            "f.csv",
            {"url": "https://e.com/f.csv", "filesize": 7, "mime": "text/csv"},
        )
        return provider, res, captured

    def test_second_open_hits_cache(self, monkeypatch):
        provider, res, captured = self._make(monkeypatch)
        first = res.get_content().read()
        second = res.get_content().read()
        assert first == b"payload" == second
        assert captured["count"] == 1  # only one upstream download

    def test_caching_disabled_when_ttl_none(self, monkeypatch):
        captured = {"count": 0, "storage": {}}

        def fake_get(url, headers=None, timeout=None):
            from unittest.mock import MagicMock

            captured["count"] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"payload"
            resp.raise_for_status.return_value = None
            return resp

        _patch_session(monkeypatch, fake_get)
        provider = dp.DataPublicLuProvider(cache_ttl=None)
        res = dp.FileResource(
            "/o/d/f.csv", {"wsgidav.provider": provider}, provider, "d",
            "f.csv",
            {"url": "https://e.com/x.csv", "filesize": 7, "mime": "text/csv"},
        )
        res.get_content().read()
        res.get_content().read()
        assert captured["count"] == 2  # no cache -> every open fetches


# ---- streaming selection by size --------------------------------------------
class TestStreamingSelection:
    def test_large_file_returns_range_stream(self):
        provider = dp.DataPublicLuProvider(cache_ttl=300.0)
        res = dp.FileResource(
            "/o/d/big.bin",
            {"wsgidav.provider": provider},
            provider,
            "d",
            "big.bin",
            {"url": "https://e.com/big.bin", "filesize": 5 * 1024 * 1024},
        )
        assert isinstance(res.get_content(), dp.UpstreamRangeStream)

    def test_small_file_returns_buffered(self, monkeypatch):
        from unittest.mock import MagicMock

        def fake_get(url, headers=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"small"
            resp.raise_for_status.return_value = None
            return resp

        _patch_session(monkeypatch, fake_get)
        provider = dp.DataPublicLuProvider(cache_ttl=300.0)
        res = dp.FileResource(
            "/o/d/small.txt",
            {"wsgidav.provider": provider},
            provider,
            "d",
            "small.txt",
            {"url": "https://e.com/small.txt", "filesize": 5},
        )
        content = res.get_content()
        import io

        assert isinstance(content, io.BytesIO)
        assert content.read() == b"small"


class _JsonResp:
    def __init__(self, payload):
        self._payload = payload

    @property
    def status_code(self):
        return 200

    def json(self):
        return self._payload


class _RangeResp:
    def __init__(self, body):
        self._body = body
        self._closed = False

    @property
    def status_code(self):
        return 206

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        self._closed = True