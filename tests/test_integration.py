"""End-to-end integration tests.

Spin up a fake ``data.public.lu`` upstream API on a loopback port, build the
real server stack (WsgiDAV app + request-context middleware + Cheroot) exactly
as ``server.run`` does, point the provider at the fake API, and drive the
resulting WebDAV server with a real WebDAV client library (``webdavclient3``).

This exercises the full production HTTP pipeline — PROPFIND directory listings,
OPTIONS capabilities, GET file downloads, Range reads, and read-only protection
— against a hermetic upstream so nothing touches the real portal or the network.
"""

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from webdav3.client import Client

import dataprovider as dp
import server as server_mod


# ---------------------------------------------------------------------------
# Fake upstream data.public.lu API
# ---------------------------------------------------------------------------


def _resource(slug, ext, content, filetype="file", mime=None, url=None):
    return {
        "id": f"res-{slug}-{ext}",
        "title": f"{slug}.{ext}",
        "url": url,  # resolved per-port when the portal is started
        "mime": mime or "text/plain",
        "filesize": len(content),
        "filetype": filetype,
        "checksum": {"type": "sha1", "value": f"chk-{slug}-{ext}"},
        "last_modified": "2024-01-02T03:04:05",
    }


def _dataset(slug, resources):
    return {
        "id": f"dataset-{slug}",
        "slug": slug,
        "title": slug.title(),
        "description": "A dataset.",
        "license": "cc-by",
        "resources": resources,
    }


class _FakePortal:
    """Model of the upstream portal's JSON responses."""

    def __init__(self):
        self.small = b"a,b,c\n1,2,3\n"
        self.large = b"0123456789" * 400  # 4000 bytes
        self.bodies = {
            "/files/data.csv": self.small,
            "/files/big.bin": self.large,
        }
        # slug -> (content, mime)
        self.resource_payloads = {
            "data.csv": (self.small, "text/plain"),
            "big.bin": (self.large, "application/octet-stream"),
        }
        # org: {"stats": [dataset-1..]} where d1 has the small file and d2 the
        # large file.
        self.orgs = {
            "stats": [
                _dataset("d1", [_resource("data", "csv", self.small)]),
                _dataset("d2", [_resource("big", "bin", self.large, mime="application/octet-stream")]),
            ]
        }

    def datasets_for_org(self, slug):
        return self.orgs.get(slug, [])

    def dataset(self, slug):
        for org in self.orgs.values():
            for ds in org:
                if ds["slug"] == slug:
                    return ds
        return None


# --- fake HTTP server ---------------------------------------------------------


class _PortalHandler(BaseHTTPRequestHandler):
    portal = None  # set on the class by build_fake_server

    def log_message(self, *_):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        portal = self.__class__.portal

        if path.startswith("/api/1/organizations/"):
            rest = path.split("/organizations/")[1].rstrip("/").split("/")
            if len(rest) == 2 and rest[1] == "datasets":
                org_slug = rest[0]
                self._send_json({"data": portal.datasets_for_org(org_slug), "next_page": None})
                return
            # Plain organizations listing.
            items = [
                {"slug": s, "title": s.title(), "metrics": {"datasets": len(v)}}
                for s, v in portal.orgs.items()
            ]
            self._send_json({"data": items, "next_page": None})
            return

        if path.startswith("/api/1/datasets/"):
            slug = path.split("/datasets/")[1].rstrip("/")
            ds = portal.dataset(slug)
            if ds is None:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(ds)
            return

        # File content (serve from the same fake server, Range-capable).
        if path in portal.bodies:
            body = portal.bodies[path]
            range_header = self.headers.get("Range")
            if range_header:
                start = int(range_header.split("=")[1].split("-")[0])
                body = body[start:]
                self._send_bytes(
                    body,
                    206,
                    {"Content-Range": f"bytes {start}-{start + len(body) - 1}/{len(body) + start}"},
                )
            else:
                self._send_bytes(body)
            return

        self._send_json({"error": "no such file"}, 404)


def build_fake_server(portal):
    """Start the fake portal HTTP server; return (server, thread, port)."""
    _PortalHandler.portal = portal
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PortalHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, port


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def running_server(monkeypatch):
    """Yield a live WebDAV server (real Cheroot + WsgiDAV) backed by a fake API."""
    portal = _FakePortal()
    srv, srv_thread, port = build_fake_server(portal)
    api_port = port
    # Resolve each resource's url to the fake server's file endpoints.
    for org_res in portal.orgs.values():
        for ds in org_res:
            for r in ds["resources"]:
                name = r["title"]
                stem, ext = name.rsplit(".", 1)
                r["url"] = f"http://127.0.0.1:{api_port}/files/{stem}.{ext}"

    monkeypatch.setattr(dp, "API_BASE", f"http://127.0.0.1:{api_port}/api/1")
    # Force the large file through the streaming/Range path.
    monkeypatch.setattr(dp, "_STREAM_THRESHOLD", 1024)

    host, wdav_port = "127.0.0.1", _free_port()
    config = server_mod.build_config(host, wdav_port, 0)
    from cheroot import wsgi
    from server import DiagnosticServer
    import wsgidav.wsgidav_app as wsgidav_mod

    app = wsgidav_mod.WsgiDAVApp(config)
    app.application = server_mod.with_request_context(app.application)
    server_mod._patch_cheroot_wfile_teardown()
    cheroot_server = wsgi.Server(
        bind_addr=(host, wdav_port),
        wsgi_app=app,
        server_name="udata-webdav",
        numthreads=8,
    )
    cheroot_server.shutdown_timeout = 2.0
    DiagnosticServer().wrap(cheroot_server)

    started = threading.Thread(target=cheroot_server.start, daemon=True)
    started.start()
    actual_port = cheroot_server.bind_addr[1]
    base = f"http://127.0.0.1:{actual_port}/"
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base, timeout=1)
        except Exception:
            time.sleep(0.1)
        else:
            ready = True
            break
    if not ready:
        cheroot_server.stop()
        srv.shutdown()
        srv.server_close()
        pytest.fail("WebDAV server did not become ready")

    try:
        yield {"base": base, "port": actual_port, "portal": portal}
    finally:
        cheroot_server.stop()
        started.join(timeout=3.0)
        srv.shutdown()
        srv.server_close()


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_client(base):
    return Client(
        {
            "webdav_hostname": base,
            "webdav_login": None,
            "webdav_password": None,
            "webdav_timeout": 30,
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_root_lists_orgs(running_server):
    info = make_client(running_server["base"]).list("")
    assert "stats" in [n.rstrip("/") for n in info]


def test_org_lists_datasets(running_server):
    info = make_client(running_server["base"]).list("stats")
    names = [n.rstrip("/") for n in info]
    assert "d1" in names and "d2" in names


def test_dataset_lists_files(running_server):
    info = make_client(running_server["base"]).list("stats/d1")
    assert "data.csv" in [n.rstrip("/") for n in info]


def test_readonly_no_put(running_server):
    base = running_server["base"]
    req = urllib.request.Request(base + "stats/d1/data.csv", data=b"overwrite", method="PUT")
    with pytest.raises(Exception) as exc_info:  # noqa: BLE001
        urllib.request.urlopen(req, timeout=10)
    assert getattr(exc_info.value, "code", None) in (403, 405, 501)


def test_download_small_file(running_server, tmp_path):
    target = tmp_path / "data.csv"
    make_client(running_server["base"]).download("stats/d1/data.csv", str(target))
    assert target.read_bytes() == running_server["portal"].small


def test_download_large_file(running_server, tmp_path):
    target = tmp_path / "big.bin"
    make_client(running_server["base"]).download("stats/d2/big.bin", str(target))
    assert target.read_bytes() == running_server["portal"].large


def test_range_read_large_file(running_server):
    base = running_server["base"]
    req = urllib.request.Request(
        base + "stats/d2/big.bin", headers={"Range": "bytes=10-19"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 206
        assert resp.read() == running_server["portal"].large[10:20]