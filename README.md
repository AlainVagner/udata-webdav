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
and listing any remote resources.

## Why

data.public.lu is a great source of open data, but browsing it is normally done
through a web UI. This project turns the whole catalog into a plain folder
tree (Finder on macOS, File Explorer on Windows, `mount.davfs` on Linux), so
you can explore and open datasets just like local files.

## Features

- **Read-only** — writes are rejected (HTTP 405). The upstream catalog is a
  one-way read; nothing on the portal can be modified.
- **Virtual tree** built from the catalog: `organizations → datasets → files`.
- **No authentication** — only exposes public, openly licensed data.
- **On-demand fetching** — files are downloaded from `data.public.lu` lazily
  when you open them, not in advance.
- **TTL in-memory cache** for directory listings, to avoid hammering the API
  on repeated browsing (configurable).
- **Remote (`filetype: remote`) resources** are exposed as internet-shortcut
  `.url` files instead of the raw resource, each pointing at the external
  destination URL. The raw remote file is hidden.
- **Range support** — large files are served with HTTP `Range`, so text
  editors, previews and `mount_webdav` can read sub-sections instead of the
  whole file.
- **Hybrid streaming** — files under 64 MB are buffered in memory; larger ones
  are streamed range-by-range from the upstream URL to avoid running out of
  memory on very large datasets (e.g. the multi-hundred-MB annual deposit XML).
- **Diagnostic logging** — errors are logged with request context (method,
  path, client, user-agent, range), and the macOS `mount_webdav` client's
  tendency to open and abort many short-lived connections no longer floods the
  log with `OSError: Bad file descriptor` deallocator tracebacks.

## Requirements

- Python 3.9+ (tested on macOS with Python 3.14)
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
.venv/bin/python server.py --host 127.0.0.1 --port 8080
```

Then open the share in your file manager:

- **macOS (Finder):** `⌘K` → `http://127.0.0.1:8080/` → Connect
- **Windows (File Explorer):** Map network drive → `http://127.0.0.1:8080/`
- **Linux:** `mount -t davfs http://127.0.0.1:8080/ /mnt/udata`

Anonymous (guest) access is all that is required — the API is public.

### Command-line options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--host` | `0.0.0.0` | Interface to bind the server to. |
| `--port` | `8080` | TCP port to listen on. |
| `--cache-ttl` | `300.0` | Seconds to cache directory listings in memory (`0` disables caching). |
| `-v` / `--verbose` | `1` | Increase verbosity (repeat for debug logging level). |

## Architecture

Two modules:

- **`server.py`** — the entry point. Builds the WsgiDAV/Cheroot server, applies
  the diagnostic wrappers, and hosts the `udata-webdav` console command.
- **`dataprovider.py`** — a custom `DAVProvider` that turns the
  `data.public.lu` API into a DAV tree:
  - `RootCollection`, `OrgCollection`, `DatasetCollection` — the folder
    levels.
  - `DatasetReadme` — the per-dataset `README.txt`.
  - `FileResource` — an individual downloadable file (with 64 MB hybrid
    streaming + range support).
  - `UrlShortcut` — a `.url` internet shortcut for remote (`filetype: remote`)
    resources.
  - `TTLCache` — small thread-safe in-memory listing cache.

### Data source

All data is fetched from the public JSON API, `https://data.public.lu/api/1`:

- `organizations/` — list of organizations (skipping ones with no datasets).
- `organizations/{org}/datasets/` — datasets and their inline `resources[]`
  (used to skip empty datasets without extra calls).
- Resources are downloaded from their raw URLs when opened.

There is no authentication and no rate limiting beyond the portal's own terms;
the TTL cache keeps API traffic modest.

### Diagnostics / known client behaviour

The macOS WebDAV client (`mount_webdav`) opens many short-lived connections to
gather metadata and build previews, and often aborts them mid-transfer. Two
things in `server.py` keep this from hurting you:

1. `_patch_cheroot_wfile_teardown()` fixes an upstream Cheroot cleanup bug where
   the response writer was never flushed before the socket was closed, which
   otherwise produced `OSError: [Errno 9] Bad file descriptor` under
   `Exception ignored while calling deallocator` for every abandoned
   connection.
2. The WSGI middleware and `DiagnosticServer` enrich error logs with request
   context and report benign client disconnects as a single concise line rather
   than a scary traceback.

