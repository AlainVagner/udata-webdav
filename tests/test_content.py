"""Tests for generated file content: DatasetReadme and UrlShortcut."""

import dataprovider as dp
from helpers import make_dataset, make_environ, make_resource


class TestDatasetReadme:
    def _readme(self, **ds_overrides):
        ds = make_dataset(**ds_overrides)
        return dp.DatasetReadme("/a/b/README.txt", make_environ(), ds)

    def test_content_type_and_length(self):
        rm = self._readme()
        assert rm.get_content_type() == "text/plain; charset=utf-8"
        assert rm.get_content_length() == len(rm._text().encode("utf-8"))

    def test_no_etag(self):
        rm = self._readme()
        assert rm.support_etag() is False
        assert rm.get_etag() is None

    def test_content_matches_text(self):
        rm = self._readme()
        assert rm.get_content().read() == rm._text().encode("utf-8")

    def test_text_contains_title_and_metadata(self):
        rm = self._readme(
            title="My dataset",
            description="It has data.",
            license="cc-by",
            page="https://data.public.lu/fr/datasets/dataset-1/",
        )
        text = rm._text()
        assert "My dataset" in text
        assert "It has data." in text
        assert "URL: https://data.public.lu/fr/datasets/dataset-1/" in text
        assert "License: cc-by" in text
        assert "Files:" in text

    def test_lists_remote_as_shortcut(self):
        remote = make_resource(
            url="https://external.com/ext.pdf", filetype="remote", id="r1"
        )
        local = make_resource(url="https://e.com/local.csv", id="r2")
        rm = self._readme(resources=[local, remote])
        text = rm._text()
        assert "  - local.csv" in text
        assert "  - ext.pdf.url" in text


class TestUrlShortcut:
    def _shortcut(self, url):
        return dp.UrlShortcut(
            "/a/b/ext.pdf.url", make_environ(), make_resource(url=url, filetype="remote")
        )

    def test_content_type(self):
        sc = self._shortcut("https://example.com/x")
        assert sc.get_content_type() == "application/x-internet-shortcut"

    def test_internet_shortcut_syntax(self):
        sc = self._shortcut("https://example.com/data.pdf")
        assert sc._text() == "[InternetShortcut]\nURL=https://example.com/data.pdf"

    def test_content_length(self):
        sc = self._shortcut("https://example.com/x")
        assert sc.get_content_length() == len(sc._text().encode("utf-8"))

    def test_content_matches(self):
        sc = self._shortcut("https://example.com/x")
        assert sc.get_content().read() == sc._text().encode("utf-8")

    def test_no_etag(self):
        sc = self._shortcut("https://example.com/x")
        assert sc.support_etag() is False