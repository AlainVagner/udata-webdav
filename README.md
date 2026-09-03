# udata-webdav

A read-only WebDAV server that exposes every file published on
[data.public.lu](https://data.public.lu) — the Luxembourg national open-data
portal — as a virtual filesystem you can mount in your operating system's file
manager.

The virtual tree is built on the fly from the public API:

```
/{organization-slug}/{dataset-slug}/{resource-file}
```

Each dataset folder also contains a tiny `README.txt` describing the dataset
and listing its files (including any remote resources).

## Why

data.public.lu is a great source of open data, but browsing it is normally done
through a web UI. This project turns the whole catalog into a plain folder
tree (Finder on macOS, File Explorer on Windows, `mount.davfs` on Linux), so
you can explore and open datasets just like local files.

## Features

- **Read-only** — writes are rejected (HTTP 405). The upstream catalog is a
  one-way read; nothing on the portal can be modified.
- **Virtual tree** — `organizations → datasets → files`, built by walking the
  catalog API.
- **No authentication** — only exposes public, openly licensed data.
- **On-demand fetching** — files are downloaded from `data.public.lu` lazily
  when you open them, not in advance.
- **TTL in-memory cache** for directory listings (configurable) to avoid
  hammering the API on repeated browsing.
- **Connection pooling** — a single shared `requests.Session` reuses keep-alive
  connections to `data.public.lu` instead of re-doing the TCP/TLS handshake for
  every API listing, file download and range read (which the OS clients issue
  in high volume).
- **Bounded content cache** — fully-read small-file bodies are held in a small
  LRU cache keyed by resource URL, so the macOS client's repeated
  preview/thumbnail opens of the same file do not trigger a fresh download each
  time. Cached bodies are capped in size and count, and the cache is disabled
  whenever the listing TTL is `0`.
- **Empty branches hidden** — organizations with no datasets and datasets with
  no files are skipped, so the tree reflects what is actually downloadable.
- **Remote (`filetype: remote`) resources** are exposed as internet-shortcut
  `.url` files pointing at the external destination; the raw remote file is
  hidden.
- **Range support** — large files answer HTTP `Range` requests
  (`206 Partial Content`), so editors, previews and `mount_webdav` read only
  the sub-section they need.
- **Hybrid streaming** — a low threshold (4 MB) keeps memory bounded under
  concurrency: small files are buffered and served from the content cache,
  while everything larger streams range-by-range from the upstream URL, so even
  multi-hundred-MB files (e.g. the annual deposit XML) never load into memory.
- **Diagnostic logging** — errors are logged with full request context
  (method, path, client, user-agent, range), and benign client aborts no longer
  flood the log with `OSError: Bad file descriptor` deallocator tracebacks.

## Requirements

- Python 3.9+
- [WsgiDAV](https://wsgidav.readthedocs.io/) ≥ 4.0
- [Cheroot](https://cherrypy.dev/cheroot/) ≥ 8.0
- [requests](https://requests.readthedocs.io/) ≥ 2.28

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs the `udata-webdav` console entry point.

## Usage

Start the server:

```bash
.venv/bin/udata-webdav --host 127.0.0.1 --port 8080
```

or run the module directly:

```bash
.venv/bin/python server.py --host 127.0.0.1 --port 8080
```

Then open the share in your file manager:

- **macOS (Finder):** `⌘K` → `http://127.0.0.1:8080/` → Connect
- **Windows (File Explorer):** Map network drive → `http://127.0.0.1:8080/`
- **Linux:** `mount -t davfs http://127.0.0.1:8080/ /mnt/udata`

Anonymous (guest) access is all that is required — the API is public.

### Testing

The unit test suite lives in `tests/` and requires `pytest`:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

Tests mock the `data.public.lu` API and upstream HTTP calls, so they run
offline and need no network. They cover the virtual-tree path resolution, the
pure helper functions, the streaming reader, the TTL cache, the generated
file content, and the diagnostics (middleware, `DiagnosticServer`, and the
Cheroot teardown patch).

`test_shutdown.py` and `test_integration.py` are end-to-end: the former runs the
real `server.py` in a subprocess and sends it SIGTERM/SIGINT; the latter spins
up a fake `data.public.lu` API on a loopback port, builds the real WsgiDAV +
Cheroot server in-process, and drives it with the `webdavclient3` WebDAV client
(PROPFIND listings, GET downloads through both the buffered and the
Range-streamed paths, and read-only protection). These require
`pip install webdavclient3` but still need no real network.

### Command-line options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--host` | `0.0.0.0` | Interface to bind the server to. |
| `--port` | `8080` | TCP port to listen on. |
| `--cache-ttl` | `300.0` | Seconds to cache directory listings in memory (`0` disables caching). |
| `--cache-file` | *(none)* | Path to a persistent JSON cache. When set, org/dataset listings are saved here and re-warmed on restart, so a reboot re-fetches far fewer listings. |
| `--root-workers` | `8` | Max concurrent API checks used to confirm zero/unknown-count orgs have datasets when listing `/`. Lower it to cap load on `data.public.lu` (min `2`). |
| `--rate-limit` | `20.0` | Max outbound requests/second to `data.public.lu` (token bucket; `0` disables). Applies to all upstream traffic; transient failures (`5xx`/`429`) are retried with exponential backoff. |
| `--shutdown-timeout` | `5.0` | Seconds to wait for in-flight requests to finish before closing on `SIGINT`/`SIGTERM` (graceful shutdown drain window). |
| `-v` / `--verbose` | `1` | Increase verbosity (repeat for debug logging level). |

---

## Architecture

The server is a small WSGI application made of two modules, `server.py` and
`dataprovider.py`, running under WsgiDAV (the WebDAV framework) on top of
Cheroot (the HTTP server). Requests flow through the layers in this order:

```
File manager (Finder / Explorer / davfs)
        │
        ▼
Cheroot (HTTP/1.1 server, connection handling)         [patched teardown]
        │
        ▼
WsgiDAV (WebDAV protocol: PROPFIND/GET/HEAD/Range)     [WSGI app]
        │
        ▼
with_request_context  (diagnostic WSGI middleware)     [request tracking]
        │
        ▼
DataPublicLuProvider  (maps paths → virtual tree)      [dataprovider.py]
        │
        ▼
data.public.lu JSON API  →  raw files
```

### Module roles

| Module | Responsibility |
| ------ | -------------- |
| `server.py` | Entry point. Builds the WsgiDAV app and Cheroot server, applies the three diagnostic/integrity fixes, hosts the `udata-webdav` console command. |
| `dataprovider.py` | A custom `DAVProvider` that turns the portal JSON API into a virtual DAV tree. All catalog and file logic lives here. |

---

## `dataprovider.py` — the virtual file tree

This module defines a `DAVProvider` subclass. WsgiDAV asks the provider for a
resource for every path URL a client requests; the provider answers with a
collection or a file, or `None` when the path does not exist.

### Data source layer

- `API_BASE` — `https://data.public.lu/api/1`, the public JSON API (no auth).
- `PAGE_SIZE` — 100 items per API page.
- `_http_get_json(url)` — **GET a URL, expect JSON, raise `DataPublicLuError`
  on any transport/HTTP/parse failure.** Centralises all API error handling.
- `_paginate(url)` — **yields every item across all pages** by following the
  `next_page` field returned by the portal.
- `TTLCache` — **a small thread-safe key/value store with time-to-live
  expiration** (optionally LRU-bounded via `max_items`). Listing cache entries
  and cached file bodies both expire, so repeated directory listings and
  preview reads do not re-hit the API or re-download files. `ttl=None`
  disables it.
- `PersistentCache(TTLCache)` — **adds disk persistence** to the listing cache.
  Valid (still-fresh) entries are reloaded on construction, so a restart
  re-warms listings without re-fetching them. Expiry is tracked in wall-clock
  time (stable across restarts, unlike `time.monotonic`); writes are atomic and
  skipped on any failure so serving never breaks.

### Module-level discovery helpers

These are pure functions used both by the provider and the collections:

- `resource_filename(resource)` — derive a display filename from a resource's
  URL (last path segment); falls back to `title`, then `id`.
- `is_remote(resource)` — true when the resource's `filetype` is `"remote"`,
  i.e. it points to an external URL rather than an uploaded file.
- `url_shortcut_name(resource)` — the `.url` filename that represents a remote
  resource (`<filename>.url`).
- `find_resource_by_filename(resources, filename)` / `find_remote_by_shortcut(...)`
  — look up a resource by its displayed name.
- `find_dict(seq, key, value)` — scan a list of dicts for the first dict whose
  `key == value`.
- `iso_to_epoch(value)` — convert an ISO-8601 timestamp to an epoch float (or
  `None`), used for DAV `get_last_modified`/`get_creation_date`.
- `license_name(license_id)` — map a portal license id to a display string.

### The provider: `DataPublicLuProvider`

Caches API responses in a `TTLCache` and resolves paths in `get_resource_inst`
by splitting the URL into 0…3 segments:

| Path depth | Returned resource |
| ---------- | ----------------- |
| `0`    | `RootCollection` — the `/` listing. |
| `1`    | `OrgCollection` — `/organization-slug`. |
| `2`    | `DatasetCollection` — `/org-slug/dataset-slug`. |
| `3`    | A file or shortcut — `/org-slug/dataset-slug/<file>`. |
| `>3`   | `None` (nested paths do not exist). |

At depth 3 it decides what the leaf is:

1. `README.txt` → a `DatasetReadme`.
2. a remote `.url` shortcut name → a `UrlShortcut`.
3. a non-remote filename → a `FileResource`.
4. otherwise → `None` (remote files are deliberately not directly reachable).

The provider also reports `is_readonly() == True` so WsgiDAV refuses writes.

#### Keeping the load on data.public.lu low

The gateway is intentionally conservative with upstream API traffic:

- **Persistent on-disk cache** — beyond the TTL cache, listings can be written
  to a JSON file (`--cache-file`) and re-warmed on restart, so a supervisor
  restart or reboot re-fetches very few listings instead of bursting the API
  again. Freshness across processes uses wall-clock timestamps; writes are
  atomic (temp file + rename) under the file-body lock, and a corrupt file is
  ignored.
- **TTL caching** — API listings (`orgs`, per-org datasets, single `dataset`)
  are cached for `--cache-ttl` (default 300 s), so consecutive browser/OS
  operations reuse the same response instead of re-fetching.
- **Single-flight request coalescing** — each cache key is guarded by a per-key
  lock. When a fan-out (e.g. a `DIRECTORY`/`PROPFIND` covering many orgs, or
  Finder opening many folders at once) issues concurrent lookups for the *same*
  org or dataset, they all wait on one lock and share a single API call, reusing
  the result, rather than each firing its own. The lock map is itself bounded —
  unused locks expire after a short TTL and the store is capped (LRU) so a
  long-running server never accumulates lock objects for every distinct key; a
  held lock is never evicted.
- **Rate limiting + backoff** — a process-wide token bucket (`--rate-limit`,
  default 20 req/s, `0` disables) throttles *all* outbound traffic to the portal
  (metadata, small files, range reads), so a Finder/PROPFIND burst cannot
  overwhelm it. Idempotent metadata GETs are retried up to `_RETRY_ATTEMPTS`
  times with exponential backoff on transient failures (`5xx`, transport
  errors), and harder on `429` (the portal signalling back off).
- **No redundant `dataset` fetches** — the org-datasets listing already embeds
  each dataset's full `resources[]`, so listing a dataset folder (or opening a
  file directly) reuses that embedded data. A separate `GET /datasets/<slug>`
  is only issued when the cached listing isn't available (e.g. expired).
- **Bounded concurrency** — the root listing's zero/unknown-count org checks run
  in a worker pool capped by `--root-workers` (default 8) and skip any org whose
  dataset listing is already cached.

> **Why no HTTP conditional requests?** The `data.public.lu` API returns no
> `ETag`/`Last-Modified`/`Cache-Control` headers and does **not** honour
> `If-None-Match`/`If-Modified-Since` (a conditional revalidation returns
> `200`, not `304`). HTTP-style revalidation would therefore be dead weight, so
> freshness is instead governed by the TTL cache + single-flight above.

### Collections (folders)

- **`RootCollection`** — lists organizations at `/`. It exposes only orgs that
  actually have datasets. Most orgs publish a dataset count in `metrics`, so it
  can skip them immediately; the remaining candidates are confirmed with a
  **bounded** parallel check (`ThreadPoolExecutor`, capped by `--root-workers`,
  default 8) that first reuses any already-cached org-dataset listings before
  hitting the API, keeping the root listing fast without bursting the portal.
- **`OrgCollection`** — lists a single organization's datasets. Because the
  org-datasets API response already embeds each dataset's `resources[]`, it
  filters out empty datasets with no extra API calls.
- **`DatasetCollection`** — lists a dataset's files. Regular (uploaded)
  resources become plain files; remote resources become `.url` shortcuts;
  `README.txt` is forced first. `get_member` special-cases the README and
  remote shortcuts, delegating everything else to the base class.

### File / leaf resources

All resources are subclasses of WsgiDAV's `DAVNonCollection` (a file).

- **`DatasetReadme`** — a generated `README.txt`. Its content (`_text()`) is a
  small human-readable summary: title, description, page URL, license, last
  modified, and a bullet list of the dataset's files. Served as UTF-8 text.
- **`FileResource`** — a downloadable resource file. Key `get_*` methods:
  - `get_content_length()` — the metadata `filesize` (or `None`).
  - `get_content_type()` — the metadata `mime` type.
  - `get_etag()` / `support_etag()` — from the resource `checksum` **without**
    surrounding quotes (WsgiDAV adds them itself; including them would break
    `If-*` matching).
  - `support_ranges()` — always `True`, enabling `206 Partial Content`. See
    *Range support* below.
  - `get_content()` — the streaming strategy, see *Hybrid streaming* below.
- **`UrlShortcut`** — a `.url` internet-shortcut file whose content is the
  Windows shortcut syntax:
  ```
  [InternetShortcut]
  URL=<resource-url>
  ```

### Streaming strategy: `FileResource.get_content`

`get_content()` returns an object WsgiDAV can seek and read sequentially. The
split is driven by the low `_STREAM_THRESHOLD` (4 MB) to keep memory bounded:

- **Small files** (below `_STREAM_THRESHOLD`): the body is fetched over HTTP,
  served from the bounded in-memory content cache on repeat reads, and returned
  as an `io.BytesIO`.
- **Everything else** (≥ `_STREAM_THRESHOLD`): an `UpstreamRangeStream` is
  returned, streaming ranges on demand so nothing is ever loaded into memory.

### `UpstreamRangeStream`

A seekable, read-only `io.RawIOBase` backed by upstream `Range` requests
(rather than a full download):

- `seek()` arms the stream to a byte offset; it lazily closes any previous
  upstream connection.
- `read()` opens a single `Range: bytes=<start>-` request on first read and
  streams the remainder in chunks, buffering as needed.
- `tell()` reports the current absolute position in the file.
- `close()` closes the upstream response.

This keeps memory usage flat on very large files (e.g. the 368 MB
`deposit-2026q2.xml`) — the previous all-in-memory approach took several
seconds to download and blew past the client's read timeout.

---

## `server.py` — wiring, config and diagnostics

This module does three jobs: build the runtime, tweak upstream behaviour, and
make logs useful.

### Config: `build_config(host, port, verbose, cache_ttl, root_workers)`

Creates the WsgiDAV config dict:

- `provider_mapping`: `{"/": provider}` mounts the virtual tree at the root.
- `http_authenticator` / `simple_dc`: allow **anonymous, read-only** access.
- `lock_storage: False` — no LOCK/PUT support (consistency with the read-only
  provider; uses the modern `lock_storage`, not the deprecated `lock_manager`).
- `walk_dynamic`-independent `dir_browser` and `hotfixes`.
- `logging.enable_loggers: ["udata-dav"]` so the project's logger reaches the
  console; level follows `--verbose`.

### `run(config, shutdown_timeout=5.0)` — graceful shutdown

Wires everything together in order:

1. Wrap the WsgiDAV WSGI app in `with_request_context` (diagnostics).
2. Call `_patch_cheroot_wfile_teardown()` (integrity fix).
3. Build the Cheroot `wsgi.Server` (50 worker threads).
4. Apply `DiagnosticServer().wrap(server)` (diagnostics).
5. Run the server on a background thread and wait for a stop event.
6. On `SIGINT`/`SIGTERM`, log the notice and begin a **graceful shutdown**:
   register the signal handlers so a process manager or `Ctrl+C` (rather than
   the default SIGTERM kill) triggers `server.stop()`, which stops accepting
   new connections and — within `shutdown_timeout` seconds — drains in-flight
   requests before closing. A second signal forces an immediate exit.

### The three engineering touches

These address the real-world quirks of serving the portal to the OS WebDAV
clients, especially macOS's:

**1. `_patch_cheroot_wfile_teardown()` — stop deallocator EBADF tracebacks.**

Cheroot's `HTTPConnection.close()` closes the read buffer and the socket but
never closes `self.wfile` (the response `StreamWriter`). Any response bytes
still buffered are not flushed until the writer is garbage-collected, at which
point the socket is already closed — so the flush raises `OSError: [Errno 9]
Bad file descriptor`, and CPython prints `Exception ignored while calling
deallocator` with a full traceback **once per terminated connection**. The
macOS client opens many short-lived connections, so this flooded the log.

The patch overrides `HTTPConnection.close` to flush and close `wfile` *while
the socket is still open* (emptying the buffer), then does the original close.
If the peer really did vanish, the failing flush is swallowed. Net effect:
zero deallocator tracebacks. (Verified: 14 such errors from a 60-connection
harness before the patch, 0 after.)

**2. `with_request_context` — a diagnostic WSGI middleware.**

Wraps the app so that while a request is served, the thread remembers it in a
thread-local (`_REQ`). It:

- captures the response status by wrapping `start_response`;
- sets/restores the thread-local request context;
- intercepts exceptions that surface either at body-production start or while
  streaming the body;
- for **benign** socket errors (client went away, errno EBADF/EPIPE/ECONNRESET)
  logs one concise line and **swallows** them (instead of re-raising into a
  noisy 500);
- for **real** errors logs `Request error | method=… path=… … | reason` with a
  traceback, then re-raises so behaviour is unchanged;
- treats `GeneratorExit` (the client disconnected and Cheroot closed the
  generator) as graceful.

**3. `DiagnosticServer` — enrich Cheroot's context-free error log.**

`wrap(server)` swaps `server.error_log` so that:

- benign socket errors become a single `INFO` line with request context
  (`Client disconnected / connection reset | method=… path=… …`), skipping the
  scary stderr traceback;
- genuine errors keep the original stderr output *and* add a contextual
  `Socket/server error | …` `ERROR` line with traceback.

Helpers used across the above: `_describe` (pulls method/path/remote/agent/range
from the WSGI `environ`), `_is_benign_socket_error` (classifies errno codes),
`_root_cause` (walks `__cause__`/`__context__` to the innermost exception), and
`_log_request_error` (formats a request-aware error line).

---

## Notes on the macOS WebDAV client

`mount_webdav` (Finder) behaves in ways that originally exposed several bugs:

- It opens many short-lived connections to gather metadata and build previews,
  and frequently aborts them mid-transfer. This surfaces as connection resets —
  now logged as a single concise line, not a stacktrace.
- It reads files such as PDFs via `Range` requests. `support_ranges()` must
  return `True` so these are answered with `206 Partial Content`; otherwise the
  client misreads framing and reports `OSError: [Errno 9] Bad file descriptor`.
- When it abandons a response, the server-side response writer was left with
  buffered bytes that were flushed into a closed socket at GC time — the
  deallocator tracebacks fixed by `_patch_cheroot_wfile_teardown()`.

## License

See the data.public.lu terms for the upstream content. This project's code is
provided under a standard open-source license; add your own `LICENSE` file if
you intend to distribute it.