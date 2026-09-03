"""Custom read-only DAVProvider exposing the data.public.lu catalog as a virtual file tree.

Virtual layout:
    /{org-slug}/{dataset-slug}/{resource-file}

Data is fetched on demand from the data.public.lu API.  A small in-memory
TTL cache avoids hammering the API on repeated directory listings, but there
is otherwise no caching and no authentication.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import unquote

import requests
from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.util import join_uri

logger = logging.getLogger("udata-dav")

API_BASE = "https://data.public.lu/api/1"
PAGE_SIZE = 100

# Identify ourselves to data.public.lu in every outbound request.
_UA = "udata-webdav/0.1 (+data.public.lu webdav gateway)"
_DEFAULT_HEADERS = {"User-Agent": _UA}
_READ_TIMEOUT = 60

# Upper bound on concurrent API calls used to confirm whether zero/unknown-count
# organisations actually have datasets when listing the root.  The default can
# be overridden per-provider via ``root_workers``.
_DEFAULT_ROOT_WORKERS = 8

# Bounding parameters for the per-key single-flight lock map.  Locks are tiny
# but a long-running server visits many distinct keys, so unused ones are
# dropped after a short TTL and the store is capped (LRU) as a backstop.  The
# TTL must comfortably exceed any single upstream fetch so an in-use lock is
# never expired out from under a settled thread.
_SINGLE_FLIGHT_TTL = 60.0
_SINGLE_FLIGHT_MAX_ITEMS = 512


def _now():
    """Monotonic clock (aliased so tests can reason about the eviction niceties)."""
    return time.monotonic()


# Module-level connection pool reused by every outbound request.  Reusing a
# single Session (with keep-alive) avoids re-doing the TCP+TLS handshake for
# every API listing, small-file download and range read, which the macOS client
# generates in high volume.
_SESSION = None
_session_lock = threading.Lock()

# Global outbound throttle: a token-bucket caps the request rate to
# data.public.lu so a Finder/PROPFIND burst cannot overwhelm the portal.  The
# defaults allow a modest short burst then settle to 20 req/s.  Rate-limit is
# applied to every upstream request via _get(); retries with backoff handle
# transient failures (see _http_get_json).
_RATE_REQUESTS_PER_SEC = 20.0
_RATE_BURST = 40
_RATE_LIMITER = None
_rate_limiter_lock = threading.Lock()


def _rate_limiter() -> "RateLimiter":
    """Return the shared outbound RateLimiter, creating it once (lazily)."""
    global _RATE_LIMITER
    if _RATE_LIMITER is None:
        with _rate_limiter_lock:
            if _RATE_LIMITER is None:
                _RATE_LIMITER = RateLimiter(
                    _RATE_REQUESTS_PER_SEC, _RATE_BURST
                )
    return _RATE_LIMITER

# Retry policy for idempotent GETs (metadata and small-file bodies).  Retried
# on transient transport errors, HTTP 5xx, and 429 (with extra backoff).
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.25  # seconds; delay = base * 2**attempt
_RETRY_MAX_DELAY = 4.0


def _session() -> "requests.Session":
    """Return the shared requests.Session, creating it once."""
    global _SESSION
    if _SESSION is None:
        with _session_lock:
            if _SESSION is None:
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=50,
                    pool_maxsize=200,
                    max_retries=0,
                )
                sess = requests.Session()
                sess.headers.update(_DEFAULT_HEADERS)
                sess.mount("https://", adapter)
                sess.mount("http://", adapter)
                _SESSION = sess
    return _SESSION

# Filesystem "name" for the dataset-level README pseudo-file.
DATASET_README = "README.txt"

# Files at or above this size are streamed from the upstream URL rather than
# buffered fully in memory (see FileResource.get_content).  The threshold is
# kept low so that nearly all files stream (bounded memory under concurrency);
# only small files below it are pulled fully into memory, which lets them be
# served from the content cache on repeated previews.
_STREAM_THRESHOLD = 4 * 1024 * 1024  # 4 MB


class TTLCache:
    """Small thread-safe in-memory cache with time-to-live expiration.

    When ``max_items`` is set, the cache is bounded: inserting a new key beyond
    that limit evicts the least-recently-used entry, so the memory footprint of
    hot data (e.g. file bodies) stays finite.
    """

    def __init__(self, ttl: float = 300.0, max_items: int | None = None):
        self.ttl = ttl
        self.max_items = max_items
        self._lock = threading.Lock()
        self._store = {}

    def get(self, key):
        if self.ttl is None:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at, last_access = entry
            if time.monotonic() > expires_at:
                self._store.pop(key, None)
                return None
            entry[2] = time.monotonic()  # touch for LRU (entry is a list)
            return value

    def set(self, key, value):
        if self.ttl is None:
            return
        with self._lock:
            now = time.monotonic()
            self._store[key] = [value, now + self.ttl, now]
            if self.max_items is not None and len(self._store) > self.max_items:
                # Evict the least-recently-used key to stay under the cap.
                oldest = min(self._store.items(), key=lambda kv: kv[1][2])
                self._store.pop(oldest[0])

    def clear(self):
        with self._lock:
            self._store.clear()


class PersistentCache(TTLCache):
    """A ``TTLCache`` that additionally persists valid entries to a JSON file.

    Persistence lets a restart re-warm the listing cache from disk instead of
    re-hitting ``data.public.lu`` for every org/dataset, dramatically reducing
    the post-restart API burst.  Freshness across processes is tracked with
    wall-clock set times (``time.time``), because ``time.monotonic`` resets on
    restart.

    Writes are atomic (temp file + ``os.replace``) and performed under the same
    lock as the in-memory store, so readers never see a half-written file.  Only
    JSON-serialisable values are supported (the API listing payloads).
    """

    def __init__(
        self,
        ttl: float = 300.0,
        max_items: int | None = None,
        path: str | None = None,
        persist: bool = True,
    ):
        super().__init__(ttl, max_items)
        self._path = path
        self._persist_enabled = bool(persist and path and ttl is not None)
        self._load()

    def _load(self):
        """Load valid (still-fresh) entries from disk into the in-memory store."""
        if not self._persist_enabled:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return  # no cache file yet
        except (OSError, ValueError):
            # Corrupt or unreadable cache: ignore it, the in-memory cache and
            # fresh fetches will rebuild it.
            return
        now = time.time()
        with self._lock:
            for key, (value, set_at) in raw.items():
                if now - set_at < self.ttl:
                    self._store[key] = [value, time.monotonic() + self.ttl, now]

    def set(self, key, value):
        super().set(key, value)
        if self._persist_enabled:
            self._persist()

    def clear(self):
        super().clear()
        if self._persist_enabled:
            self._remove_file()

    def _persist(self):
        """Atomically write a snapshot of the current store to disk."""
        if not self._persist_enabled:
            return
        now = time.time()
        snapshot = {}
        # Under the same lock to avoid a torn read of an in-progress update.
        with self._lock:
            for k, (value, _expires_at, _last_access) in self._store.items():
                snapshot[k] = [value, now]
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, self._path)
        except (OSError, TypeError):
            # Persistence must never break serving; on a failure just skip the
            # write (the in-memory store still works).
            pass

    def _remove_file(self):
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass


class DataPublicLuError(Exception):
    """Raised when the upstream API cannot be reached or returns an error."""


class RateLimiter:
    """A thread-safe token-bucket limiter to cap upstream request rate.

    ``rate_per_sec`` refills the bucket; ``burst`` is the maximum number of
    tokens the bucket can hold (i.e. how much short-term burst is tolerated).
    ``acquire()`` blocks until a token is available.  When ``rate_per_sec <= 0``
    the limiter is disabled and ``acquire()`` returns immediately, so tests and
    callers can opt out easily.
    """

    def __init__(self, rate_per_sec: float = 0.0, burst: int | None = None):
        self.rate = max(0.0, float(rate_per_sec))
        self.burst = int(burst) if burst is not None else max(1, int(self.rate))
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._cond = threading.Condition()

    def acquire(self):
        """Block until a request token is available (no-op when disabled)."""
        if self.rate <= 0:
            return
        with self._cond:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.burst,
                    self._tokens + (now - self._last) * self.rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                self._cond.wait(timeout=wait)

    def wait_until_ready(self):
        """Non-consuming readiness check; used by tests to assert pacing."""
        if self.rate <= 0:
            return 0.0
        with self._cond:
            now = time.monotonic()
            deficit = max(0.0, 1.0 - self._tokens)
            return deficit / self.rate if self.rate else 0.0


def _get(url: str, **kwargs):
    """GET a URL through the shared session with our default headers.

    Defaults are merged explicitly here (not only via the session) so callers
    are guaranteed the User-Agent regardless of session state; a per-call
    ``headers`` argument overrides the defaults.

    Every request passes through the global rate limiter first, so the whole
    process (metadata, small files, range reads) is throttled together.
    """
    _rate_limiter().acquire()
    headers = dict(_DEFAULT_HEADERS)
    headers.update(kwargs.pop("headers", None) or {})
    return _session().get(url, headers=headers, **kwargs)


def _is_retryable(resp_or_exc):
    """Whether a failed response/exception warrants a retry with backoff."""
    status = getattr(resp_or_exc, "status_code", None)
    if status is not None:
        # 429 = rate-limited by the portal; 5xx = upstream trouble.  Retryable.
        return status in (429,) or 500 <= status <= 599
    # Transport-level error (timeout, connection reset, DNS, ...): retryable.
    if isinstance(resp_or_exc, requests.RequestException):
        return True
    return False


def _http_get_json(url: str):
    """GET a URL expecting a JSON response; raise on failure.

    Retries transient failures (transport errors, 5xx, 429) with exponential
    backoff, since GETs are idempotent and an occasional blip shouldn't fail a
    directory listing outright.
    """
    delay = _RETRY_BASE_DELAY
    last = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = _get(url, timeout=30)
        except requests.RequestException as exc:
            if attempt < _RETRY_ATTEMPTS - 1:
                last = exc
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
                continue
            raise DataPublicLuError(f"Request failed for {url}: {exc}") from exc
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:  # invalid JSON
                raise DataPublicLuError(f"Invalid JSON from {url}") from exc
        if _is_retryable(resp) and attempt < _RETRY_ATTEMPTS - 1:
            # On 429 back off harder (respect the portal's pacing request).
            if resp.status_code == 429:
                delay = min(
                    delay * 2 + _RETRY_BASE_DELAY,
                    _RETRY_MAX_DELAY + _RETRY_BASE_DELAY,
                )
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY)
            continue
        raise DataPublicLuError(
            f"API error {resp.status_code} for {url}"
            + (f" (last: {last})" if last else "")
        )


def _paginate(url: str):
    """Yield every item across all pages of a paginated list endpoint."""
    current = url
    while current:
        data = _http_get_json(current)
        for item in data.get("data", []):
            yield item
        current = data.get("next_page")


# Cap on how many small-file bodies are kept in memory (LRU).  Must be small
# enough that concurrent copies stay bounded but large enough to serve the
# repeated preview/thumbnail reads the OS clients make.
_CONTENT_CACHE_ITEMS = 64
_CONTENT_CACHE_MAX_BYTES = 256 * 1024 * 1024  # upper bound per cached body
_CONTENT_CACHE_TTL_SECONDS = 60.0


class DataPublicLuProvider(DAVProvider):
    def __init__(
        self,
        cache_ttl: float = 300.0,
        root_workers: int = 8,
        cache_path: str | None = None,
        persist: bool = True,
    ):
        super().__init__()
        # The listing cache persists to disk when a path is given, so a restart
        # re-warms org/dataset listings from disk instead of re-fetching them
        # (greatly reducing the post-restart API burst).  The short-lived,
        # binary file-body cache stays in-memory only.
        if cache_path and cache_ttl is not None:
            self._cache = PersistentCache(
                ttl=cache_ttl,
                path=cache_path,
                persist=persist,
            )
        else:
            self._cache = TTLCache(cache_ttl)
        # Bounded LRU cache of fully-read (small) file bodies keyed by the
        # resource download URL, so Finder's repeated previews/thumbnails of the
        # same file do not trigger a fresh upstream download every time.
        self._content_cache = TTLCache(
            ttl=_CONTENT_CACHE_TTL_SECONDS, max_items=_CONTENT_CACHE_ITEMS
        )
        self._root_workers = max(2, root_workers if root_workers is not None else _DEFAULT_ROOT_WORKERS)
        # Per-key lock map (single-flight): while one thread is fetching a given
        # listing, any other thread asking for the same key waits on this lock
        # and reuses the result, so a fan-out (DIRECTORY / PROPFIND across many
        # orgs at once) issues at most one API call per distinct key.
        #
        # The map is bounded: unused locks expire after a short TTL and the
        # store is capped (LRU), so a long-running server does not accumulate an
        # unbounded number of lock objects as distinct keys are visited.
        self._single_flight: dict = {}
        self._single_flight_lock = threading.Lock()

    def _single_flight_context(self, key: str):
        """Return a context manager that holds the per-key lock for ``key``.

        Acquires (creating on first use) the lock for ``key`` and, on exit,
        releases it and updates the eviction bookkeeping so held locks are
        never evicted (which would let a second request slip in and duplicate
        the in-flight fetch).
        """
        @contextmanager
        def _cm():
            with self._single_flight_lock:
                entry = self._single_flight.get(key)
                if entry is None:
                    entry = [threading.Lock(), 0, 0.0]
                    self._single_flight[key] = entry
                lock, held, _last_access = entry
                held += 1
                entry[1] = held
                entry[2] = _now()
                self._trim_locked()
            try:
                with lock:
                    yield
            finally:
                with self._single_flight_lock:
                    entry = self._single_flight.get(key)
                    if entry is not None:
                        entry[1] = max(0, entry[1] - 1)
                        entry[2] = _now()

        return _cm()

    def _trim_locked(self):
        """Evict expired/unused single-flight locks (caller holds the guard lock).

        Skips any lock that is still being held (its request may still be
        in-flight or another thread is waiting on it).
        """
        now = _now()
        expired = [
            k
            for k, v in self._single_flight.items()
            if v[1] == 0 and (now - v[2]) > _SINGLE_FLIGHT_TTL
        ]
        for k in expired:
            self._single_flight.pop(k, None)
        if len(self._single_flight) > _SINGLE_FLIGHT_MAX_ITEMS:
            # LRU backstop: evict the oldest-touched unused entry to stay under
            # the cap if many keys were seen within the TTL window.
            idle = [
                (k, v)
                for k, v in self._single_flight.items()
                if v[1] == 0
            ]
            idle.sort(key=lambda kv: kv[1][2])
            drop = len(self._single_flight) - _SINGLE_FLIGHT_MAX_ITEMS
            for k, _v in idle[:drop]:
                self._single_flight.pop(k, None)

    def _cache_keyed(self, key: str, fetch):
        """Return the cached value for ``key``, fetching it once under a lock.

        Guards the whole cache-check -> fetch -> store cycle with a per-key lock
        so concurrent callers for the same key share a single upstream request
        (single-flight) instead of each firing its own.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        with self._single_flight_context(key):
            # Re-check inside the lock: another thread may have populated it
            # while we were waiting.
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            if self._cache.ttl is None:
                # Caching disabled: still serialize concurrent calls for the
                # same key, but don't persist the result.
                return fetch()
            value = fetch()
            if value is not None:
                self._cache.set(key, value)
            return value

    # -- file-content cache ---------------------------------------------------
    def get_cached_content(self, url):
        return self._content_cache.get(url)

    def cache_content(self, url, body):
        if self._cache.ttl is None:
            return  # caching disabled entirely
        if len(body) > _CONTENT_CACHE_MAX_BYTES:
            return  # too big to hold in memory
        self._content_cache.set(url, body)

    def cached_org_datasets(self, org_slug):
        """Return the cached org-dataset listing, or None if not cached/fresh enough.

        Lets callers resolve an org's dataset availability without an API round
        trip when a recent listing is already in memory.
        """
        return self._cache.get(f"org:{org_slug}:datasets")
    # -- caching helpers -----------------------------------------------------
    def orgs(self):
        return self._cache_keyed(
            "orgs",
            lambda: list(
                _paginate(
                    f"{API_BASE}/organizations/?page_size={PAGE_SIZE}&sort=name"
                )
            ),
        )

    def org_datasets(self, org_slug: str):
        return self._cache_keyed(
            f"org:{org_slug}:datasets",
            lambda: list(
                _paginate(
                    f"{API_BASE}/organizations/{org_slug}/datasets/"
                    f"?page_size={PAGE_SIZE}&sort=title"
                )
            ),
        )

    def dataset(self, dataset_slug: str):
        return self._cache_keyed(
            f"dataset:{dataset_slug}",
            lambda: _http_get_json(f"{API_BASE}/datasets/{dataset_slug}/"),
        )

    # -- DAVProvider interface ----------------------------------------------
    def is_readonly(self):
        return True

    def _dataset_from_org_listing(self, org_slug, dataset_slug):
        """Return the dataset dict from a fresh cached org listing if available.

        The org-datasets API response embeds each dataset with its full
        ``resources[]``, so a dataset referenced there needs no separate API
        call.  Only reuses the cache when it is already fresh; if the listing
        is absent or expired it returns None so the caller falls back to a
        single full ``GET /datasets/<slug>`` rather than re-warming the whole
        (possibly multi-page) org listing just for one file.
        """
        listing = self._cache.get(f"org:{org_slug}:datasets")
        if listing is None:
            return None
        return find_dict(listing, "slug", dataset_slug)

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
            # Prefer the dataset object already embedded in the org-datasets
            # listing (which carries its ``resources[]``), so a direct file
            # access does not require a separate full ``GET /datasets/<slug>``
            # call.  Fall back to the full fetch only when not available.
            dataset = self._dataset_from_org_listing(org_slug, dataset_slug)
            if dataset is not None:
                resources = dataset.get("resources", [])
            else:
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
    """Return the display filename for a resource, based on its URL.

    Uses the last URL path segment where available; otherwise falls back to the
    resource title and finally its id.  The fallback is defensive: a URL-less
    resource whose ``title``/``id`` are ``None`` or whitespace must not produce
    a ``None`` filename (``unquote`` would crash on it), so it degrades to
    ``"file"``.
    """
    url = (resource.get("url") or "").strip()
    name = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    if not name:
        title = resource.get("title")
        rid = resource.get("id")
        name = (title if isinstance(title, str) and title.strip() else "") or (
            rid if isinstance(rid, str) and rid.strip() else ""
        )
    # Name is empty-or-whitespace only when there is nothing usable at all.
    name = name.strip()
    return unquote(name) if name else "file"


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
        # ones need a live API check.  We resolve those in parallel (bounded),
        # reusing any already-cached org-dataset listings so a listing that was
        # fetched moments ago within the TTL does not trigger a fresh request.
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
            cached = self.provider.cached_org_datasets(org["slug"])
            if cached is not None:
                return bool(cached)
            try:
                return bool(self.provider.org_datasets(org["slug"]))
            except DataPublicLuError:
                return False

        if to_check:
            workers = max(2, min(self.provider._root_workers, len(to_check)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
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
        # The dataset object passed in already comes from the org-datasets
        # listing, which embeds each dataset's ``resources[]``.  Reuse that so
        # listing a folder doesn't issue a second, redundant
        # ``GET /datasets/<slug>`` call.  Only fall back to a full dataset fetch
        # when the embedded list is missing (e.g. a shallow object).
        embedded = self.dataset.get("resources")
        if embedded is not None:
            return embedded
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
            # Serve repeat reads from the in-memory content cache to avoid a
            # fresh upstream download for every preview/thumbnail.
            cached = (
                self.provider.get_cached_content(url)
                if self.provider is not None else None
            )
            if cached is not None:
                return io.BytesIO(cached)
            try:
                upstream = _get(url, timeout=_READ_TIMEOUT)
            except requests.RequestException as exc:
                raise DAVError(HTTP_FORBIDDEN, context_info=str(exc)) from exc
            upstream.raise_for_status()
            body = upstream.content
            if self.provider is not None:
                self.provider.cache_content(url, body)
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
        self._pos = 0  # absolute position in the file (see tell())
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

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence != 0 or offset < 0:
            raise OSError(22, "Invalid argument")
        self._close_stream()
        self._start = offset
        self._pos = offset
        return self._pos

    def _open(self):
        if self._resp is None:
            self._resp = _get(
                self._url,
                headers={"Range": f"bytes={self._start}-"},
                stream=True,
                timeout=_READ_TIMEOUT,
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
            self._pos += len(out)
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
        self._pos += len(out)
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
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return time.mktime(parsed)
    except (ValueError, TypeError, OverflowError):
        return None


def license_name(license_id):
    if not isinstance(license_id, str) or not license_id.strip():
        return "Unknown"
    # data.public.lu license identifiers look like "cc-by" or "notspecified".
    return license_id.strip()
