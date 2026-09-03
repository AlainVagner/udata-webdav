"""Tests for the with_request_context diagnostic WSGI middleware."""

import logging

import server as srv

ENV = {
    "REQUEST_METHOD": "GET",
    "PATH_INFO": "/org/dataset/file.pdf",
    "REMOTE_ADDR": "10.0.0.1",
    "HTTP_USER_AGENT": "mount_webdav",
    "HTTP_RANGE": "bytes=0-99",
}


def _start_response(status, headers, exc_info=None):
    return None


def _iter_all(middleware):
    """Fully exhaust the middleware for a request and return the collected body."""
    return b"".join(middleware(ENV, _start_response))


class _App:
    """A configurable fake downstream WSGI app."""

    def __init__(self, body=b"", raise_after=None, raise_at=None, exc=None):
        self.body = body
        self.raw = raise_after  # raise after N bytes yielded
        self.exc = exc or RuntimeError("boom")

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Length", str(len(self.body)))])
        if self.raw is None:
            yield self.body
            return
        emitted = 0
        for chunk in [self.body[: self.raw], self.body[self.raw :]]:
            if not chunk:
                continue
            yield chunk
            emitted += len(chunk)
            if emitted >= self.raw and self.raw > 0:
                raise self.exc


class TestMiddleware:
    def test_passes_through_normally(self):
        mw = srv.with_request_context(_App(body=b"helloworld"))
        assert _iter_all(mw) == b"helloworld"

    def test_benign_ebadf_mid_body_is_swallowed(self, caplog):
        mw = srv.with_request_context(
            _App(body=b"abcdefghij", raise_after=5, exc=OSError(9, "Bad file descriptor"))
        )
        with caplog.at_level(logging.INFO, logger="udata-dav"):
            # Must NOT raise; body is cut short at the swallow point.
            out = _iter_all(mw)
        assert out == b"abcde"
        assert any("Client disconnected mid-transfer" in r.message for r in caplog.records)
        assert any("Bad file descriptor" in r.message for r in caplog.records)

    def test_benign_broken_pipe_is_swallowed(self, caplog):
        mw = srv.with_request_context(
            _App(body=b"abcdefghij", raise_after=5, exc=BrokenPipeError(32, "broken"))
        )
        with caplog.at_level(logging.INFO, logger="udata-dav"):
            out = _iter_all(mw)
        assert out == b"abcde"

    def test_real_error_is_rerisen(self, caplog):
        mw = srv.with_request_context(
            _App(body=b"abcdefghij", raise_after=5, exc=RuntimeError("real error"))
        )
        import pytest

        with caplog.at_level(logging.ERROR, logger="udata-dav"):
            with pytest.raises(RuntimeError):
                _iter_all(mw)
        assert any("Request error" in r.message for r in caplog.records)
        assert any("RuntimeError" in r.message for r in caplog.records)

    def test_startup_error_reported(self, caplog):
        def bad_app(environ, start_response):
            raise ValueError("before body")  # noqa: TRY301

        mw = srv.with_request_context(bad_app)
        import pytest

        with caplog.at_level(logging.ERROR, logger="udata-dav"):
            with pytest.raises(ValueError):
                _iter_all(mw)
        assert any("Request error" in r.message for r in caplog.records)
        assert any("before body" in r.message for r in caplog.records)

    def test_generatorexit_is_rerisen_not_logged(self, caplog):
        def gen_app(environ, start_response):
            start_response("200 OK", [("Content-Length", "10")])
            yield b"abcde"
            raise GeneratorExit

        mw = srv.with_request_context(gen_app)
        import pytest

        with caplog.at_level(logging.INFO, logger="udata-dav"):
            it = iter(mw(ENV, _start_response))
            next(it)
            with pytest.raises(GeneratorExit):
                next(it)
        assert not caplog.records  # GeneratorExit is graceful, not logged

    def test_thread_local_restored_after_request(self):
        mw = srv.with_request_context(_App(body=b"x"))
        srv._REQ.current = "previous"
        _iter_all(mw)
        assert srv._REQ.current == "previous"

    def test_thread_local_set_during_request(self):
        seen = {}

        def spy_app(environ, start_response):
            start_response("200 OK", [("Content-Length", "1")])
            seen["ctx"] = srv._REQ.current
            yield b"y"

        mw = srv.with_request_context(spy_app)
        _iter_all(mw)
        assert seen["ctx"]["path"] == ENV["PATH_INFO"]
        assert seen["ctx"]["method"] == ENV["REQUEST_METHOD"]