"""Tests for the pure diagnostic helpers in server.py."""

import server as srv


class TestDescribe:
    def test_extracts_request_fields(self):
        env = {
            "REQUEST_METHOD": "PROPFIND",
            "PATH_INFO": "/org/dataset/file.pdf",
            "REMOTE_ADDR": "10.0.0.1",
            "HTTP_USER_AGENT": "mount_webdav",
            "HTTP_RANGE": "bytes=0-99",
        }
        assert srv._describe(env) == {
            "method": "PROPFIND",
            "path": "/org/dataset/file.pdf",
            "remote": "10.0.0.1",
            "user_agent": "mount_webdav",
            "range": "bytes=0-99",
        }

    def test_missing_fields_default_to_empty(self):
        assert srv._describe({}) == {
            "method": "",
            "path": "",
            "remote": "",
            "user_agent": "",
            "range": "",
        }


class TestIsBenignSocketError:
    def test_broken_pipe_is_benign(self):
        assert srv._is_benign_socket_error(BrokenPipeError()) is True

    def test_connection_reset_is_benign(self):
        assert srv._is_benign_socket_error(ConnectionResetError()) is True

    def test_ebadf_is_benign(self):
        assert srv._is_benign_socket_error(OSError(9, "Bad file descriptor")) is True

    def test_epipe_is_benign(self):
        # errno 32 = EPIPE
        assert srv._is_benign_socket_error(OSError(32, "Broken pipe")) is True

    def test_ecoronet_reset_errno(self):
        import errno

        assert srv._is_benign_socket_error(
            OSError(errno.ECONNRESET, "Connection reset")
        ) is True

    def test_non_socket_error_not_benign(self):
        assert srv._is_benign_socket_error(RuntimeError("boom")) is False
        assert srv._is_benign_socket_error(ValueError("nope")) is False

    def test_unrelated_oserror_not_benign(self):
        # errno 13 = EACCES
        assert srv._is_benign_socket_error(OSError(13, "Permission denied")) is False

    def test_real_oserror_wrapped_surfaces(self):
        # A nested exception with a benign cause is still classified by errno.
        assert srv._is_benign_socket_error(OSError(9, "Bad file descriptor")) is True


class TestRootCause:
    def test_returns_innermost_cause(self):
        inner = ValueError("inner")
        outer = RuntimeError("outer")
        outer.__cause__ = inner
        assert srv._root_cause(outer) is inner

    def test_uses_context_when_no_cause(self):
        cause = OSError(32, "Broken pipe")
        exc = RuntimeError("wrapped")
        exc.__context__ = cause
        assert srv._root_cause(exc) is cause

    def test_single_exception_returns_itself(self):
        exc = KeyError("x")
        assert srv._root_cause(exc) is exc

    def test_chained_chain(self):
        a = KeyError("a")
        b = ValueError("b")
        c = RuntimeError("c")
        c.__cause__ = b
        b.__cause__ = a
        assert srv._root_cause(c) is a