"""Custom read-only DAVProvider exposing the data.public.lu catalog as a virtual file tree.

Virtual layout:
    /{org-slug}/{dataset-slug}/{resource-file}

Data is fetched on demand from the data.public.lu API.  A small in-memory
TTL cache avoids hammering the API on repeated directory listings, but there
is otherwise no caching and no authentication.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import requests
from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.util import join_uri

logger = logging.getLogger("udata-dav")

API_BASE = "https://data.public.lu/api/1"
PAGE_SIZE = 100

# Filesystem "name" for the dataset-level README pseudo-file.
DATASET_README = "README.txt"

# Files at or above this size are streamed from the upstream URL rather than
# buffered fully in memory (see FileResource.get_content).
_STREAM_THRESHOLD = 64 * 1024 * 1024


class TTLCache:
    """Very small thread-safe in-memory cache with time-to-live expiration."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._store = {}

    def get(self, key):
        if self.ttl is None:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key, value):
        if self.ttl is None:
            return
        with self._lock:
            self._store[key] = (value, time.monotonic() + self.ttl)

    def clear(self):
        with self._lock:
            self._store.clear()


class DataPublicLuError(Exception):
    """Raised when the upstream API cannot be reached or returns an error."""


def _http_get_json(url: str):
    """GET a URL expecting a JSON response; raise on any failure."""
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        raise DataPublicLuError(f"Request failed for {url}: {exc}") from exc
    if resp.status_code != 200:
        raise DataPublicLuError(f"API error {resp.status_code} for {url}")
    try:
        return resp.json()
    except ValueError as exc:  # invalid JSON
        raise DataPublicLuError(f"Invalid JSON from {url}") from exc


def _paginate(url: str):
    """Yield every item across all pages of a paginated list endpoint."""
    current = url
    while current:
        data = _http_get_json(current)
        for item in data.get("data", []):
            yield item
        current = data.get("next_page")


class DataPublicLuProvider(DAVProvider):
    def __init__(self, cache_ttl: float = 300.0):
        super().__init__()
        self._cache = TTLCache(cache_ttl)
    # -- caching helpers -----------------------------------------------------
    def orgs(self):
        key = "orgs"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        orgs = list(
            _paginate(
                f"{API_BASE}/organizations/?page_size={PAGE_SIZE}&sort=name"
            )
        )
        self._cache.set(key, orgs)
        return orgs

    def org_datasets(self, org_slug: str):
        key = f"org:{org_slug}:datasets"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        datasets = list(
            _paginate(
                f"{API_BASE}/organizations/{org_slug}/datasets/"
                f"?page_size={PAGE_SIZE}&sort=title"
            )
        )
        self._cache.set(key, datasets)
        return datasets

    def dataset(self, dataset_slug: str):
        key = f"dataset:{dataset_slug}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        dataset = _http_get_json(f"{API_BASE}/datasets/{dataset_slug}/")
        self._cache.set(key, dataset)
        return dataset

    # -- DAVProvider interface ----------------------------------------------
    def is_readonly(self):
        return True

    def get_resource_inst(self, path: str, environ: dict):
        norm = path.strip("/")
        if not norm:
            return RootCollection("", environ, self)

        segments = [unquote(part) for part in norm.split("/")]

        if len(segments) == 1:
            org = find_dict(self.orgs(), "slug", segments[0])
            if org is None:
                return None
            return OrgCollection("/" + segments[0], environ, self, org)

        if len(segments) == 2:
            dt = find_dict(self.org_datasets(segments[0]), "slug", segments[1])
            if dt is None:
                return None
            return DatasetCollection(
                f"/{segments[0]}/{segments[1]}", environ, self, dt
            )

        if len(segments) == 3:
            org_slug, dataset_slug, filename = segments
            try:
                dataset = self.dataset(dataset_slug)
                resources = dataset.get("resources", [])
            except DataPublicLuError:
                dataset = None
                resources = []
            if filename == DATASET_README:
                if dataset is not None:
                    return DatasetReadme(path, environ, dataset)
                return None
            remote = find_remote_by_shortcut(resources, filename)
            if remote is not None:
                return UrlShortcut(path, environ, remote)
            # Remote resources are exposed only via their '.url' shortcut.
            resource = find_resource_by_filename(
                [r for r in resources if not is_remote(r)], filename
            )
            if resource is None:
                return None
            return FileResource(
                path, environ, self, dataset_slug, filename, resource
            )

        # Nested paths are not part of the virtual layout.
        return None


def find_dict(seq, key, value):
    """Return first dict in seq whose key equals value, else None."""
    for item in seq:
        if item.get(key) == value:
            return item
    return None


def find_resource_by_filename(resources, filename):
    """Find a resource whose filename (last URL segment) matches filename."""
    for res in resources:
        if resource_filename(res) == filename:
            return res
    return None


def resource_filename(resource):
    """Return the display filename for a resource, based on its URL."""
    url = resource.get("url", "") or ""
    name = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    if not name:
        name = resource.get("title", "") or resource.get("id", "file")
    return unquote(name)


def is_remote(resource):
    """Return True for resources that point to an external URL (filetype 'remote')."""
    return resource.get("filetype") == "remote"


def url_shortcut_name(resource):
    """Return the '.url' shortcut filename for a remote resource."""
    return resource_filename(resource) + ".url"


def find_remote_by_shortcut(resources, name):
    """Find the remote resource whose '.url' shortcut filename equals name."""
    for res in resources:
        if is_remote(res) and url_shortcut_name(res) == name:
            return res
    return None


class RootCollection(DAVCollection):
    """Virtual '/' showing every organization as a collection."""

    def __init__(self, path, environ, provider):
        super().__init__(path, environ)
        self.provider = provider

    def get_member_names(self):
        # An organisation without any dataset is hidden.  Most orgs expose a
        # dataset count in their "metrics"; only the remaining (zero/unknown)
        # ones need a live API check, which we do in parallel to stay fast.
        orgs = self.provider.orgs()
        definite = []
        to_check = []
        for org in orgs:
            count = (org.get("metrics") or {}).get("datasets")
            if isinstance(count, int) and count > 0:
                definite.append(org)
            else:
                to_check.append(org)

        def has_datasets(org):
            try:
                return bool(self.provider.org_datasets(org["slug"]))
            except DataPublicLuError:
                return False

        if to_check:
            with ThreadPoolExecutor(max_workers=16) as pool:
                keep = list(pool.map(has_datasets, to_check))
            definite += [o for o, ok in zip(to_check, keep) if ok]

        return [org["slug"] for org in definite]


class OrgCollection(DAVCollection):
    """An organization folder listing its datasets as sub-collections."""

    def __init__(self, path, environ, provider, org):
        super().__init__(path, environ)
        self.provider = provider
        self.org = org

    def get_member_names(self):
        # The org-datasets listing already embeds each dataset's resources, so
        # we can filter out datasets without any file without extra API calls.
        names = []
        for dt in self.provider.org_datasets(self.org["slug"]):
            resources = dt.get("resources") or []
            if any(resource_filename(r) for r in resources):
                names.append(dt["slug"])
        return names

    def get_display_name(self):
        return self.org.get("name") or self.name

    def get_creation_date(self):
        return iso_to_epoch(self.org.get("created_at"))

    def get_last_modified(self):
        return iso_to_epoch(self.org.get("last_modified"))

    def support_modified(self):
        return True


class DatasetCollection(DAVCollection):
    """A dataset folder listing its resource files plus a README.txt."""

    def __init__(self, path, environ, provider, dataset):
        super().__init__(path, environ)
        self.provider = provider
        self.dataset = dataset

    def _resources(self):
        try:
            return self.provider.dataset(self.dataset["slug"]).get(
                "resources", []
            )
        except DataPublicLuError as exc:
            logger.warning("Could not fetch dataset resources: %s", exc)
            return []

    def get_member_names(self):
        resources = self._resources()
        # Regular (uploaded) resources are exposed as their own file.  Remote
        # resources are exposed only as a '.url' internet-shortcut file.
        names = [resource_filename(r) for r in resources if not is_remote(r)]
        names += [url_shortcut_name(r) for r in resources if is_remote(r)]
        names.insert(0, DATASET_README)
        return names

    def get_member(self, name):
        if name == DATASET_README:
            return DatasetReadme(
                join_uri(self.path, name), self.environ, self.dataset
            )
        remote = find_remote_by_shortcut(self._resources(), name)
        if remote is not None:
            return UrlShortcut(join_uri(self.path, name), self.environ, remote)
        return super().get_member(name)

    def get_display_name(self):
        return self.dataset.get("title") or self.name

    def get_creation_date(self):
        return iso_to_epoch(self.dataset.get("created_at"))

    def get_last_modified(self):
        return iso_to_epoch(self.dataset.get("last_modified"))

    def support_modified(self):
        return True


class DatasetReadme(DAVNonCollection):
    """A small README.txt describing a dataset."""

    def __init__(self, path, environ, dataset):
        super().__init__(path, environ)
        self.dataset = dataset

    def get_content_length(self):
        return len(self._text().encode("utf-8"))

    def get_content_type(self):
        return "text/plain; charset=utf-8"

    def get_etag(self):
        return None

    def support_etag(self):
        return False

    def get_content(self):
        return io.BytesIO(self._text().encode("utf-8"))

    def _text(self):
        d = self.dataset
        lines = [
            d.get("title", ""),
            "=" * len(d.get("title", "")),
            "",
            d.get("description") or "",
            "",
            f"URL: {d.get('page') or d.get('uri') or ''}",
            f"License: {license_name(d.get('license'))}",
            f"Updated: {d.get('last_modified') or ''}",
            "",
            "Files:",
        ]
        for r in d.get("resources", []):
            name = url_shortcut_name(r) if is_remote(r) else resource_filename(r)
            lines.append(f"  - {name}")
        return "\n".join(lines)


class FileResource(DAVNonCollection):
    """A read-only resource file streamed from the upstream URL."""

    def __init__(self, path, environ, provider, dataset_slug, filename, resource):
        super().__init__(path, environ)
        self.provider = provider
        self.dataset_slug = dataset_slug
        self.filename = filename
        self.resource = resource

    def get_content_length(self):
        size = self.resource.get("filesize")
        return size if isinstance(size, int) and size >= 0 else None

    def get_content_type(self):
        return self.resource.get("mime") or None

    def get_etag(self):
        checksum = self.resource.get("checksum") or {}
        value = checksum.get("value")
        # WsgiDAV adds the surrounding quotes itself; never include them here.
        return value if value else None

    def support_etag(self):
        return bool(self.get_etag())

    def get_last_modified(self):
        return iso_to_epoch(self.resource.get("last_modified"))

    def support_modified(self):
        return self.get_last_modified() is not None

    def support_content_length(self):
        return self.get_content_length() is not None

    def support_ranges(self):
        # macOS's mount_webdav client reads files (e.g. PDFs opened by
        # Preview/QuickLook) via HTTP Range requests.  Enabling ranges makes
        # WsgiDAV answer such requests with '206 Partial Content' instead of
        # sending the whole body, which previously confused the client into
        # misaligned reads ("OSError: [Errno 9] Bad file descriptor").
        return True

    def get_content(self):
        url = self.resource.get("url")
        if not url:
            raise DAVError(HTTP_FORBIDDEN)
        size = self.resource.get("filesize")
        large = isinstance(size, int) and size >= _STREAM_THRESHOLD
        if not large:
            try:
                upstream = requests.get(url, timeout=60)
            except requests.RequestException as exc:
                raise DAVError(HTTP_FORBIDDEN, context_info=str(exc)) from exc
            upstream.raise_for_status()
            body = upstream.content
            if isinstance(size, int) and size >= 0 and len(body) != size:
                logger.warning(
                    "Resource %s: upstream body has %d bytes but metadata reports %d",
                    self.filename,
                    len(body),
                    size,
                )
            # Return the whole body as an in-memory stream.  Reading it fully
            # and letting WsgiDAV own a plain BytesIO avoids reusing the
            # underlying HTTP socket after it has been closed (which caused
            # "OSError: [Errno 9] Bad file descriptor" with the raw stream).
            return io.BytesIO(body)
        # Very large files are streamed from the upstream URL instead of being
        # downloaded fully into memory.  WsgiDAV seeks to the range start and
        # then reads sequentially, so ranges are mirrored on demand rather
        # than stalling for seconds while the whole body is fetched.
        return UpstreamRangeStream(url)


class UpstreamRangeStream(io.RawIOBase):
    """A seekable read-only stream backed by Range requests to an upstream URL.

    WsgiDAV seeks to the start of the requested range, then reads the body in
    sequential chunks.  Lazy access avoids downloading large files fully into
    memory: the first seek arms the stream and the first read() opens a single
    upstream 'Range: bytes=<start>-' request and streams the remainder.
    """

    def __init__(self, url):
        super().__init__()
        self._url = url
        self._start = 0
        self._resp = None
        self._iter = None
        self._buf = b""
        self._eof = False

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return False

    def seek(self, offset, whence=0):
        if whence != 0 or offset < 0:
            raise OSError(22, "Invalid argument")
        self._close_stream()
        self._start = offset
        return self._start

    def _open(self):
        if self._resp is None:
            self._resp = requests.get(
                self._url,
                headers={"Range": f"bytes={self._start}-"},
                stream=True,
                timeout=60,
            )
            self._resp.raise_for_status()
            self._iter = self._resp.iter_content(chunk_size=65536)
            self._buf = b""
            self._eof = False

    def read(self, size=-1):
        if size is None or size < 0:
            size = -1
        self._open()
        if size < 0:
            tail = b"".join(self._iter) if not self._eof else b""
            self._eof = True
            out = self._buf + tail
            self._buf = b""
            return out
        while len(self._buf) < size and not self._eof:
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._eof = True
                break
            self._buf += chunk
        out = self._buf[:size]
        self._buf = self._buf[size:]
        return out

    def _close_stream(self):
        if self._resp is not None:
            self._resp.close()
            self._resp = None
            self._iter = None
            self._buf = b""
            self._eof = False

    def close(self):
        self._close_stream()
        super().close()


class UrlShortcut(DAVNonCollection):
    """A '.url' internet-shortcut file pointing to a remote resource.

    The content follows the Windows internet-shortcut syntax::

        [InternetShortcut]
        URL=<resource-url>
    """

    def __init__(self, path, environ, resource):
        super().__init__(path, environ)
        self.resource = resource

    def _text(self):
        url = self.resource.get("url") or ""
        return f"[InternetShortcut]\nURL={url}"

    def get_content_length(self):
        return len(self._text().encode("utf-8"))

    def get_content_type(self):
        return "application/x-internet-shortcut"

    def get_etag(self):
        return None

    def support_etag(self):
        return False

    def get_content(self):
        return io.BytesIO(self._text().encode("utf-8"))


def iso_to_epoch(value):
    """Convert an ISO-8601 string to an epoch float, or None."""
    if not value:
        return None
    try:
        parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return time.mktime(parsed)
    except (ValueError, TypeError):
        return None


def license_name(license_id):
    if not license_id:
        return "Unknown"
    # data.public.lu license identifiers look like "cc-by" or "notspecified".
    return license_id
