"""Tests for DiagnosticServer and the Cheroot wfile-teardown patch."""

import logging

import server as srv


# ---- DiagnosticServer --------------------------------------------------------
class _FakeCherootServer:
    """Stand-in for a Cheroot HTTPServer exposing the error_log hook."""

    def __init__(self):
        self.stderr_calls = []

    def error_log(self, msg="", level=20, traceback=False):
        self.stderr_calls.append((msg, level, traceback))


def _wrap(server_obj, caplog, level=logging.INFO):
    srv.DiagnosticServer().wrap(server_obj)
    caplog.set_level(level, logger="udata-dav")
    return server_obj


class TestDiagnosticServer:
    def test_benign_error_logged_once_no_stderr(self, caplog):
        fake = _FakeCherootServer()
        srv.DiagnosticServer().wrap(fake)
        srv._REQ.current = {
            "method": "GET",
            "path": "/org/dataset/file.pdf",
            "remote": "10.0.0.1",
            "user_agent": "mount_webdav",
            "range": "bytes=0-99",
        }
        with caplog.at_level(logging.INFO, logger="udata-dav"):
            fake.error_log(OSError(9, "Bad file descriptor"), 40, traceback=True)
        assert fake.stderr_calls == []  # benign -> no noisy stderr traceback
        assert any(
            "Client disconnected / connection reset" in r.message
            for r in caplog.records
        )
        assert any("Bad file descriptor" in r.message for r in caplog.records)
        assert any("path=/org/dataset/file.pdf" in r.message for r in caplog.records)

    def test_benign_uses_info_level_and_skips_traceback(self, caplog):
        fake = _FakeCherootServer()
        srv.DiagnosticServer().wrap(fake)
        with caplog.at_level(logging.INFO, logger="udata-dav"):
            fake.error_log(BrokenPipeError(32, "x"), 40, traceback=True)
        # Only an INFO record, no traceback blob.
        assert all(r.levelno == logging.INFO for r in caplog.records)
        assert not any("Traceback" in r.message for r in caplog.records)
        assert fake.stderr_calls == []

    def test_real_error_keeps_stderr_and_logs_context(self, caplog):
        fake = _FakeCherootServer()
        srv.DiagnosticServer().wrap(fake)
        srv._REQ.current = {"method": "GET", "path": "/a", "remote": "r",
                            "user_agent": "u", "range": "-"}
        with caplog.at_level(logging.ERROR, logger="udata-dav"):
            fake.error_log(RuntimeError("boom"), 40, traceback=True)
        assert len(fake.stderr_calls) == 1  # original stderr behavior preserved
        assert any("Socket/server error" in r.message for r in caplog.records)
        assert any("RuntimeError" in r.message for r in caplog.records)

    def test_no_request_context_still_logged(self, caplog):
        fake = _FakeCherootServer()
        srv.DiagnosticServer().wrap(fake)
        srv._REQ.current = None
        with caplog.at_level(logging.INFO, logger="udata-dav"):
            fake.error_log(OSError(9, "Bad file descriptor"), 40, traceback=True)
        assert any("no request context" in r.message for r in caplog.records)


# ---- _patch_cheroot_wfile_teardown -------------------------------------------
class _FakeWfile:
    def __init__(self):
        self.closed = False
        self._closed_count = 0

    def close(self):
        self._closed_count += 1
        self.closed = True


class _FakeConnection:
    def __init__(self):
        self.wfile = _FakeWfile()
        self.close_called = False

    def close(self):
        self.close_called = True


class TestWfileTeardownPatch:
    def test_closes_wfile_before_original_close(self, monkeypatch):
        import cheroot.server as cheroot_server

        original_close = cheroot_server.HTTPConnection.close
        conn = _FakeConnection()

        # Prevent the real class close from doing anything; capture the call.
        monkeypatch.setattr(cheroot_server.HTTPConnection, "close", _FakeConnection.close)
        srv._patch_cheroot_wfile_teardown()

        try:
            patched = cheroot_server.HTTPConnection.close
            patched(conn)
        finally:
            # Restore for other tests
            monkeypatch.setattr(cheroot_server.HTTPConnection, "close", original_close)

        # wfile was closed (flushed) first, then the original close ran.
        assert conn.wfile._closed_count == 1
        assert conn.close_called is True

    def test_missing_wfile_is_tolerated(self, monkeypatch):
        import cheroot.server as cheroot_server

        original_close = cheroot_server.HTTPConnection.close

        class NoWfileConn(_FakeConnection):
            def __init__(self):
                super().__init__()
                self.wfile = None

        conn = NoWfileConn()
        monkeypatch.setattr(cheroot_server.HTTPConnection, "close", _FakeConnection.close)
        srv._patch_cheroot_wfile_teardown()
        try:
            cheroot_server.HTTPConnection.close(conn)
        finally:
            monkeypatch.setattr(cheroot_server.HTTPConnection, "close", original_close)
        assert conn.close_called is True  # did not blow up on a missing wfile

    def test_already_closed_wfile_is_tolerated(self, monkeypatch):
        import cheroot.server as cheroot_server

        original_close = cheroot_server.HTTPConnection.close
        conn = _FakeConnection()
        conn.wfile.closed = True  # simulate upstream already having closed it

        monkeypatch.setattr(cheroot_server.HTTPConnection, "close", _FakeConnection.close)
        srv._patch_cheroot_wfile_teardown()
        try:
            cheroot_server.HTTPConnection.close(conn)
        finally:
            monkeypatch.setattr(cheroot_server.HTTPConnection, "close", original_close)
        # closed writer was skipped; original close still ran.
        assert conn.wfile._closed_count == 0
        assert conn.close_called is True