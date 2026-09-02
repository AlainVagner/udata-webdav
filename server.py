"""Launch the read-only data.public.lu WebDAV server."""

from __future__ import annotations

import argparse
import logging
import threading
import traceback

from wsgidav.wsgidav_app import WsgiDAVApp

from dataprovider import DataPublicLuProvider

logger = logging.getLogger("udata-dav")

# Thread-local slot holding the request currently being served by the current
# worker thread.  It lets us enrich Cheroot's otherwise-context-free socket
# errors (e.g. "OSError: [Errno 9] Bad file descriptor") with the details of
# the request that triggered them.
_REQ = threading.local()


def _request_info():
    """Return a small dict describing the request in flight, or None."""
    return getattr(_REQ, "current", None)


def _patch_cheroot_wfile_teardown():
    """Close Cheroot's response writer before the socket fd dies.

    Cheroot's ``HTTPConnection.close`` closes the read buffer and the socket
    but never closes ``self.wfile`` (the response ``StreamWriter``).  Any
    response bytes still sitting in its buffer are therefore not flushed until
    Python garbage-collects the writer, at which point the socket is already
    closed and the flush raises ``OSError: [Errno 9] Bad file descriptor``.
    CPython reports this as ``Exception ignored while calling deallocator``
    with a full traceback, once for every terminated connection (the macOS
    WebDAV client opens many short-lived connections for metadata).

    Closing ``self.wfile`` *before* the socket is closed flushes that buffer
    while the socket is still open, leaving nothing for the deallocator to
    write into a dead file descriptor.  If the peer has already gone away the
    flush cleanly fails and is swallowed.
    """
    from cheroot import server as cheroot_server

    original_close = cheroot_server.HTTPConnection.close

    def close_with_wfile_flush(self):
        wfile = getattr(self, "wfile", None)
        if wfile is not None and not getattr(wfile, "closed", True):
            try:
                wfile.close()
            except OSError:
                # Peer went away mid-response; the buffered bytes can no
                # longer be delivered.  Drop them silently instead of letting
                # them surface later as an EBADF from IOBase.__del__.
                pass
        original_close(self)

    cheroot_server.HTTPConnection.close = close_with_wfile_flush


def _describe(environ):
    """Extract the relevant details of an incoming request for diagnostics."""
    return {
        "method": environ.get("REQUEST_METHOD", ""),
        "path": environ.get("PATH_INFO", ""),
        "remote": environ.get("REMOTE_ADDR", ""),
        "user_agent": environ.get("HTTP_USER_AGENT", ""),
        "range": environ.get("HTTP_RANGE", ""),
    }


def with_request_context(app):
    """WSGI middleware recording the current request for better error logs.

    It wraps ``app`` so that, while a request is being served, the current
    thread remembers which request it is.  Cheroot's generic error logging
    (``error_log``) then resolves that context and reports the affected
    request instead of a bare socket error.

    It also intercepts exceptions that surface while the response body is
    being produced and logs them with the request context before re-raising.
    """

    def __call__(environ, start_response):
        info = _describe(environ)

        # A middleware that wants to log the *outcome* must observe the
        # response status, so wrap start_response to capture it.
        status_holder = []

        def _start_response(status, response_headers, exc_info=None):
            status_holder.append(status)
            return start_response(status, response_headers, exc_info)

        prev = getattr(_REQ, "current", None)
        _REQ.current = info
        try:
            app_iter = app(environ, _start_response)
        except BaseException as exc:
            # The app raised before producing a body.
            if _is_benign_socket_error(exc):
                logger.info(
                    "Client disconnected during request | method=%s path=%s "
                    "remote=%s | %s: %s",
                    info.get("method"), info.get("path"), info.get("remote"),
                    type(exc).__name__, exc,
                )
                return
            _log_request_error(info, "startup", exc)
            raise
        try:
            for part in app_iter:
                yield part
        except GeneratorExit:
            # The client disconnected and Cheroot closed our generator mid
            # stream.  This is a graceful shutdown, not an error; the real
            # socket problem is reported by the error handling below (if any).
            raise
        except BaseException as exc:
            if _is_benign_socket_error(exc):
                # Client went away mid-transfer; nothing more to send.  Report
                # concisely and stop without re-raising (which would otherwise
                # surface as a noisy 500 in Cheroot's log).
                logger.info(
                    "Client disconnected mid-transfer | method=%s path=%s "
                    "remote=%s range=%s | %s: %s",
                    info.get("method"), info.get("path"), info.get("remote"),
                    info.get("range") or "-", type(exc).__name__, exc,
                )
                return
            _log_request_error(info, status_holder, exc)
            raise
        finally:
            _REQ.current = prev

    return __call__


def _is_benign_socket_error(exc):
    """Return True for expected 'client went away' socket errors.

    These are OSErrors with errno codes such as EBADF, EPIPE or ECONNRESET
    raised while writing a response to a client that has disconnected or sent
    a TCP RST (typical of the macOS WebDAV client aborting a download).
    """
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, (OSError, ConnectionError)):
        try:
            from cheroot import errors as cheroot_errors
            benign = cheroot_errors.socket_errors_to_ignore
        except Exception:
            from errno import EPIPE, EBADF, ECONNRESET, ENOTCONN, EINVAL
            benign = [EPIPE, EBADF, ECONNRESET, ENOTCONN, EINVAL]
        errno_num = getattr(exc, "errno", None) or (
            exc.args[0] if exc.args else None
        )
        return errno_num in benign
    return False


def _root_cause(exc):
    """Walk to the innermost exception, skipping Python plumbing wrappers."""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        cause = cur.__cause__ or cur.__context__
        if cause is None:
            break
        cur = cause
    return cur


def _log_request_error(info, status, exc):
    """Log an error with the context of the request that caused it."""
    if not isinstance(exc, BaseException):
        return
    root = _root_cause(exc)
    status_txt = "?"
    if isinstance(status, str):
        status_txt = status
    elif isinstance(status, list) and status:
        status_txt = status[0]

    if type(root) is type(exc):
        # Single, unwrapped failure: report directly.
        reason = f"{type(exc).__name__}: {exc}"
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        reason = f"{type(exc).__name__}: {exc} (caused by {type(root).__name__}: {root})"
        tb = "".join(traceback.format_exception(type(root), root, root.__traceback__))

    logger.error(
        "Request error | method=%s path=%s remote=%s agent=%s range=%s "
        "status=%s | %s",
        info.get("method"),
        info.get("path"),
        info.get("remote"),
        info.get("user_agent") or "-",
        info.get("range") or "-",
        status_txt,
        reason,
    )
    if tb:
        logger.error("Request error traceback:\n%s", tb)


class DiagnosticServer:
    """Handle Cheroot's connection errors with context and without noise.

    Cheroot's default ``error_log`` writes a bare message to stderr (e.g.
    "OSError: [Errno 9] Bad file descriptor") plus a full traceback, with no
    indication of which request was affected.  Most of these are benign: the
    client (typically the macOS WebDAV client) disconnected or reset the
    connection mid-transfer.  This swaps ``error_log`` so that:

    * known benign socket errors are logged once, concisely and with the
      request in flight (method, path, client, user agent, range) instead of
      a scary yet context-free traceback;
    * genuinely unexpected errors keep their traceback for debugging.
    """

    def wrap(self, server):
        try:
            from cheroot import errors as cheroot_errors
            benign_errnos = list(cheroot_errors.socket_errors_to_ignore)
        except Exception:
            from errno import EPIPE, EBADF, ECONNRESET, ENOTCONN, EINVAL
            benign_errnos = [EPIPE, EBADF, ECONNRESET, ENOTCONN, EINVAL]

        original_error_log = server.error_log

        def _is_benign(exc):
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return True
            if isinstance(exc, OSError) or isinstance(exc, Exception):
                errno_num = getattr(exc, "errno", None) or (exc.args and exc.args[0])
                if errno_num in benign_errnos:
                    return True
            return False

        def error_log(msg="", level=20, traceback=False):
            benign = _is_benign(msg)
            info = _request_info()
            if isinstance(msg, BaseException):
                reason = f"{type(msg).__name__}: {msg}"
            else:
                reason = str(msg)

            if benign:
                # Expected client-disconnect.  Reply with a single concise,
                # contextual line; skip the noisy stderr traceback.
                if info:
                    logger.info(
                        "Client disconnected / connection reset | method=%s path=%s "
                        "remote=%s agent=%s range=%s | %s",
                        info.get("method"), info.get("path"), info.get("remote"),
                        info.get("user_agent") or "-", info.get("range") or "-",
                        reason,
                    )
                else:
                    logger.info(
                        "Client disconnected / connection reset | no request "
                        "context | %s", reason,
                    )
                return

            # Real error: keep Cheroot's original stderr output AND add context.
            try:
                original_error_log(msg, level, traceback)
            except Exception:
                pass
            if info:
                logger.error(
                    "Socket/server error | method=%s path=%s remote=%s agent=%s "
                    "range=%s | %s",
                    info.get("method"), info.get("path"), info.get("remote"),
                    info.get("user_agent") or "-", info.get("range") or "-",
                    reason,
                )
            else:
                logger.error("Socket/server error | no request context | %s", reason)
            if traceback:
                try:
                    logger.error(
                        "Socket/server error traceback:\n%s",
                        traceback.format_exc(),
                    )
                except Exception:
                    pass

        server.error_log = error_log
        return server


def build_config(host, port, verbose, cache_ttl=300.0):
    if cache_ttl <= 0:
        cache_ttl = None  # no caching
    provider = DataPublicLuProvider(cache_ttl)

    config = {
        "host": host,
        "port": port,
        "provider_mapping": {"/": provider},
        "http_authenticator": {
            "domain_controller": None,  # anonymous read-only access
        },
        # Allow anonymous access to the "/" share (read-only provider).
        "simple_dc": {"user_mapping": {"*": True}},
        "verbose": verbose,
        "logging": {
            "enable_loggers": ["udata-dav"],
            "level": logging.DEBUG if verbose >= 2 else logging.INFO,
        },
        "lock_storage": False,  # no write/LOCK support (read-only)
        "dir_browser": {"enable": True},
        "hotfixes": {"re_encode_path_info": True},
    }
    return config


def run(config):
    """Serve the WsgiDAV app with the Cheroot WSGI server."""
    from cheroot import wsgi

    app = WsgiDAVApp(config)
    app.application = with_request_context(app.application)

    _patch_cheroot_wfile_teardown()

    server = wsgi.Server(
        bind_addr=(config["host"], config["port"]),
        wsgi_app=app,
        server_name="udata-webdav",
        numthreads=50,
    )
    DiagnosticServer().wrap(server)
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only WebDAV server for data.public.lu"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=300.0,
        help="Seconds to cache listings in memory (0 disables caching)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=1, help="Increase verbosity"
    )
    args = parser.parse_args(argv)

    config = build_config(args.host, args.port, args.verbose, args.cache_ttl)

    # Configure our module logger.
    logging.getLogger("udata-dav").setLevel(
        logging.DEBUG if args.verbose >= 2 else logging.INFO
    )

    print(f"data.public.lu WebDAV server on http://{args.host}:{args.port}/")
    print("Virtual tree: /<organisation>/<dataset>/<file>")
    run(config)


if __name__ == "__main__":
    main()