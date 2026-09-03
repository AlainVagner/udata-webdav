"""Tests for the UpstreamRangeStream streaming class."""

import pytest

import dataprovider as dp

FULL_BODY = b"0123456789abcdefg"  # 17 bytes


class _FakeResp:
    """A minimal stand-in for a requests.Response supporting streaming.

    When a ``Range: bytes=<start>-`` header is present, ``iter_content`` serves
    the body *starting at* ``<start>`` (as a real server would), so seeking the
    stream re-requests from the correct offset.
    """

    def __init__(self, body, range_header=None, chunk_size=65536):
        self._body = body
        self._range_header = range_header
        self._chunk_size = chunk_size
        self._closed = False
        # 206 if a Range was requested
        self.status_code = 206 if range_header else 200
        start = 0
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):]
            left = spec.split("-", 1)[0]
            if left.isdigit():
                start = int(left)
        self._start = start

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        body = self._body[self._start:]
        for i in range(0, len(body), self._chunk_size):
            yield body[i : i + self._chunk_size]

    def close(self):
        self._closed = True


class _FakeSession:
    def __init__(self, fake_get):
        self.get = fake_get


def _install_fake_requests(monkeypatch, body, captured, chunk_size=65536):
    """Monkeypatch the shared session's get to return a fake response."""

    def fake_get(url, headers=None, stream=None, timeout=None):
        captured["headers"] = headers
        captured.setdefault("calls", []).append((url, headers.get("Range") if headers else None))
        return _FakeResp(body, headers.get("Range") if headers else None, chunk_size)

    monkeypatch.setattr(dp, "_session", lambda: _FakeSession(fake_get))


class TestUpstreamRangeStream:
    def test_read_all_uses_single_range_request(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s.read() == FULL_BODY
        assert captured["headers"]["Range"] == "bytes=0-"
        assert captured["calls"] == [("https://example.com/big.bin", "bytes=0-")]

    def test_seek_then_read_requests_offset(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s.seek(5) == 5
        assert s.read() == FULL_BODY[5:]
        assert captured["headers"]["Range"] == "bytes=5-"

    def test_partial_read_buffers_rest(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        # Read tiny pieces to force internal buffering.
        pieces = b""
        while True:
            chunk = s.read(3)
            if not chunk:
                break
            pieces += chunk
        assert pieces == FULL_BODY

    def test_stream_is_read_only_seekable(self, monkeypatch):
        _install_fake_requests(monkeypatch, FULL_BODY, {})
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s.readable() is True
        assert s.seekable() is True
        assert s.writable() is False

    def test_close_closes_upstream_response(self, monkeypatch):
        captured_objs = []

        def fake_get(url, headers=None, stream=None, timeout=None):
            resp = _FakeResp(FULL_BODY, headers.get("Range") if headers else None)
            captured_objs.append(resp)
            return resp

        monkeypatch.setattr(
            dp, "_session", lambda: _FakeSession(fake_get)
        )
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        s.read(4)
        assert captured_objs and not captured_objs[0]._closed
        s.close()
        assert captured_objs[0]._closed

    def test_seek_reopens_stream_and_closes_previous(self, monkeypatch):
        captured_objs = []

        def fake_get(url, headers=None, stream=None, timeout=None):
            resp = _FakeResp(FULL_BODY, headers.get("Range") if headers else None)
            captured_objs.append(resp)
            return resp

        monkeypatch.setattr(
            dp, "_session", lambda: _FakeSession(fake_get)
        )
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        s.read(4)
        first = captured_objs[0]
        assert not first._closed
        s.seek(10)  # should close the first upstream response
        assert first._closed
        s.read(4)  # opens a new one
        assert len(captured_objs) == 2

    def test_read_until_eof_stops(self, monkeypatch):
        _install_fake_requests(monkeypatch, FULL_BODY, {})
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s.read() == FULL_BODY
        # EOF already reached; further reads return empty without re-requesting.
        assert s.read() == b""
        assert s.read(4) == b""


class TestRangeStreamSeekTell:
    """Deeper coverage of seek/tell and read chunking behaviour."""

    def test_tell_reflects_position(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s.tell() == 0
        s.read(5)
        assert s.tell() == 5

    def test_seek_forward_reopens_with_new_range(self, monkeypatch):
        # Seeking far enough clears the buffer and closes the current response,
        # opening a fresh request at the new offset.
        captured_objs = []

        def fake_get(url, headers=None, stream=None, timeout=None):
            resp = _FakeResp(FULL_BODY, (headers or {}).get("Range"))
            captured_objs.append(resp)
            return resp

        monkeypatch.setattr(dp, "_session", lambda: _FakeSession(fake_get))
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        s.read(2)
        first = captured_objs[0]
        assert not first._closed
        s.seek(10)
        assert first._closed  # old response closed
        assert s.read() == FULL_BODY[10:]
        assert len(captured_objs) == 2

    def test_seek_backward_reopens(self, monkeypatch):
        # Backward seek must also re-open (no cached earlier prefix to back over).
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        s.read(12)
        s.seek(3)
        assert s.read() == FULL_BODY[3:]
        assert captured["calls"] == [
            ("https://example.com/big.bin", "bytes=0-"),
            ("https://example.com/big.bin", "bytes=3-"),
        ]

    def test_seek_zero_resets_to_start(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        s.read(7)
        assert s.tell() == 7
        assert s.seek(0) == 0
        assert s.read() == FULL_BODY

    def test_seek_invalid_args_raise(self, monkeypatch):
        _install_fake_requests(monkeypatch, FULL_BODY, {})
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        with pytest.raises(OSError):
            s.seek(5, whence=1)
        with pytest.raises(OSError):
            s.seek(-1)

    def test_read_size_crosses_internal_chunks(self, monkeypatch):
        # The upstream yields many small chunks; a single read() larger than one
        # chunk must still assemble the whole requested window correctly.
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured, chunk_size=4)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        # Ask for 10 bytes, which spans several 4-byte upstream chunks.
        assert s.read(10) == FULL_BODY[:10]
        assert s.read(10) == FULL_BODY[10:]

    def test_chunked_reads_across_boundaries_match_whole(self, monkeypatch):
        body = b"A" * 3 + b"BBBB" + b"C" * 100  # irregular, boundary-straddling
        captured = {}
        _install_fake_requests(monkeypatch, body, captured, chunk_size=7)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        pieces = b""
        # Read in awkward sizes (1, 5, 3, 11, ...) that straddle chunk edges.
        for size in [1, 5, 3, 11, 7, 2]:
            pieces += s.read(size)
        pieces += s.read()
        assert pieces == body

    def test_repeated_tiny_reads_accumulate(self, monkeypatch):
        captured = {}
        _install_fake_requests(monkeypatch, FULL_BODY, captured, chunk_size=2)
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        got = b"".join(s.read(1) for _ in range(len(FULL_BODY) + 3))
        assert got == FULL_BODY

    def test_initially_not_opened_until_first_read(self, monkeypatch):
        s = dp.UpstreamRangeStream("https://example.com/big.bin")
        assert s._resp is None  # lazy: nothing fetched yet
        monkeypatch.setattr(
            dp, "_session", lambda: _FakeSession(
                lambda url, headers=None, stream=None, timeout=None: _FakeResp(
                    FULL_BODY, headers.get("Range") if headers else None
                )
            )
        )
        s.read(1)
        assert s._resp is not None