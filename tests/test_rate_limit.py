"""Tests for the outbound rate limiter and retry-with-backoff behaviour."""

import time

import pytest
import requests

import dataprovider as dp


class _FakeResp:
    def __init__(self, status_code, payload=None, exc=None):
        self.status_code = status_code
        self._payload = payload
        self._exc = exc

    def json(self):
        if self._exc is not None:
            raise self._exc
        return self._payload


class _FakeSession:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if not self.sequence:
            return _FakeResp(200, {"data": [], "next_page": None})
        item = self.sequence.pop(0)
        # Fault injection: an exception placed in the sequence is raised (it
        # models a transport-level failure such as a connection error).
        if isinstance(item, Exception):
            raise item
        return item


def _patch_session(monkeypatch, sequence):
    sess = _FakeSession(sequence)
    monkeypatch.setattr(dp, "_session", lambda: sess)
    # Keep backoff sleeps negligible so the tests stay fast.
    monkeypatch.setattr(dp, "_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(dp, "_RETRY_MAX_DELAY", 0.0)
    return sess


class TestRateLimiter:
    def test_disabled_acquire_is_immediate(self):
        lim = dp.RateLimiter(rate_per_sec=0.0)
        start = time.monotonic()
        for _ in range(10):
            lim.acquire()
        assert time.monotonic() - start < 1.0

    def test_burst_capacity_then_throttle(self):
        # A bucket with burst=1, fast refill must not allow two immediate
        # acquisitions; the second one blocks.
        lim = dp.RateLimiter(rate_per_sec=100.0, burst=1)
        lim.acquire()  # drains the single token
        start = time.monotonic()
        lim.acquire()  # must wait for the refill (>= ~0.01 s)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.005


class TestRetryBackoff:
    def test_transient_failure_then_success(self, monkeypatch):
        # 500 first, then 200: _http_get_json must retry and succeed.
        sess = _patch_session(
            monkeypatch, [_FakeResp(500), _FakeResp(200, {"ok": True})]
        )
        result = dp._http_get_json("https://data.public.lu/api/1/x/")
        assert result == {"ok": True}
        assert len(sess.calls) == 2

    def test_429_then_success(self, monkeypatch):
        sess = _patch_session(
            monkeypatch, [_FakeResp(429), _FakeResp(200, {"ok": True})]
        )
        result = dp._http_get_json("https://data.public.lu/api/1/x/")
        assert result == {"ok": True}
        assert len(sess.calls) == 2

    def test_persistent_failure_raises(self, monkeypatch):
        sess = _patch_session(monkeypatch, [_FakeResp(500)] * 10)
        with pytest.raises(dp.DataPublicLuError):
            dp._http_get_json("https://data.public.lu/api/1/x/")
        # Retry attempt count governs how many times it actually tried.
        assert len(sess.calls) <= dp._RETRY_ATTEMPTS + 1
        assert len(sess.calls) >= dp._RETRY_ATTEMPTS

    def test_transport_error_then_success(self, monkeypatch):
        # A connection error (transient) must be retried, then succeed.
        sess = _patch_session(
            monkeypatch,
            [requests.ConnectionError("boom"), _FakeResp(200, {"ok": True})],
        )
        result = dp._http_get_json("https://data.public.lu/api/1/x/")
        assert result == {"ok": True}
        assert len(sess.calls) == 2

    def test_persistent_transport_error_raises(self, monkeypatch):
        sess = _patch_session(
            monkeypatch, [requests.Timeout("slow")] * 10
        )
        with pytest.raises(dp.DataPublicLuError):
            dp._http_get_json("https://data.public.lu/api/1/x/")
        assert len(sess.calls) == dp._RETRY_ATTEMPTS

    def test_429_does_not_retry_past_limit_but_recovers(self, monkeypatch):
        # Several 429s in a row still exhaust retries then give up.
        sess = _patch_session(monkeypatch, [_FakeResp(429)] * 10)
        with pytest.raises(dp.DataPublicLuError):
            dp._http_get_json("https://data.public.lu/api/1/x/")
        assert len(sess.calls) == dp._RETRY_ATTEMPTS