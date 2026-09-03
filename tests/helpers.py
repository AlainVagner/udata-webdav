"""Shared test helpers: fake API responses and a minimal WSGI environ."""


def make_environ(provider=None):
    """A minimal WSGI environ usable by WsgiDAV resource constructors."""
    return {"wsgidav.provider": provider}


def make_resource(**overrides):
    """Build a resource dict like one returned by the data.public.lu API."""
    base = {
        "id": "res-1",
        "title": "A dataset file",
        "url": "https://data.public.lu/fr/datasets/r/example.pdf",
        "mime": "application/pdf",
        "filesize": 1000,
        "filetype": "file",
        "checksum": {"type": "sha1", "value": "abc123"},
        "last_modified": "2024-01-02T03:04:05",
    }
    base.update(overrides)
    return base


def make_dataset(**overrides):
    """Build a dataset dict like one returned by the data.public.lu API."""
    base = {
        "id": "dataset-1",
        "slug": "dataset-1",
        "title": "Example dataset",
        "description": "Some description.",
        "page": "https://data.public.lu/fr/datasets/dataset-1/",
        "license": "cc-by",
        "created_at": "2024-01-01T00:00:00",
        "last_modified": "2024-02-03T04:05:06",
        "resources": [],
    }
    base.update(overrides)
    return base