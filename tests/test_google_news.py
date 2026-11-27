"""Offline tests for the optional Google News RSS provider."""

from __future__ import annotations

import json
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import patch

from ai_signal.live.google_news import GoogleNewsPageFetcher, GoogleNewsSearchProvider
from ai_signal.model import Bucket, Query
from ai_signal.providers import PageContent


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Fresh &amp; relevant - Publisher A</title>
<link>https://news.google.com/rss/articles/fresh</link>
<pubDate>Tue, 15 Sep 2026 09:30:00 GMT</pubDate>
<description><![CDATA[<a href="x">Fresh textile investment</a>]]></description>
<source>Publisher A</source></item>
<item><title>At the exclusive lower bound</title>
<link>https://news.google.com/rss/articles/boundary</link>
<pubDate>Sat, 12 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>After the upper bound</title>
<link>https://news.google.com/rss/articles/future</link>
<pubDate>Tue, 15 Sep 2026 10:00:01 GMT</pubDate></item>
<item><title>No date</title>
<link>https://news.google.com/rss/articles/undated</link></item>
<item><title>Fresh duplicate</title>
<link>https://news.google.com/rss/articles/fresh</link>
<pubDate>Tue, 15 Sep 2026 09:31:00 GMT</pubDate></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, body: bytes = FEED):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestGoogleNewsProvider(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
        self.end = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
        self.query = Query(
            '"textile investment"',
            Bucket.TOPICS,
            lookback_hours=72,
            retrieval_start=self.start,
            retrieval_end=self.end,
        )

    def test_keyword_and_when_window_are_encoded(self):
        url = GoogleNewsSearchProvider()._url(self.query)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["q"], ['"textile investment" when:3d'])
        self.assertEqual(params["ceid"], ["IN:en"])

    def test_exact_bounds_undated_items_and_duplicates_are_filtered(self):
        provider = GoogleNewsSearchProvider()
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            hits = provider.search(self.query)
        self.assertEqual([hit.url for hit in hits], ["https://news.google.com/rss/articles/fresh"])
        self.assertIsNone(hits[0].published_at, "feed time must not become verified evidence")
        status = provider.health()["Google News"]
        self.assertIn("2 outside exact bounds", status)
        self.assertIn("1 undated", status)

    def test_upper_bound_is_inclusive(self):
        feed = FEED.replace(b"Tue, 15 Sep 2026 09:30:00 GMT", b"Tue, 15 Sep 2026 10:00:00 GMT")
        with patch("urllib.request.urlopen", return_value=FakeResponse(feed)):
            hits = GoogleNewsSearchProvider().search(self.query, limit=1)
        self.assertEqual(len(hits), 1)

    def test_zero_limit_returns_no_candidates(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            hits = GoogleNewsSearchProvider().search(self.query, limit=0)
        self.assertEqual(hits, [])


class RecordingFetcher:
    def __init__(self):
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        return PageContent(body="publisher article", ok=True, source_name="Publisher A")


class TestGoogleNewsPageFetcher(unittest.TestCase):
    google_url = "https://news.google.com/rss/articles/token123?oc=5"
    wrapper = (
        '<div data-n-a-id="token123" data-n-a-ts="1789474470" '
        'data-n-a-sg="signature123"></div>'
    ).encode()
    batch = (
        ")]}'\n\n" + json.dumps([
            ["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://publisher.example/story"])]
        ])
    ).encode()

    def test_resolves_wrapper_before_fetching_publisher_page(self):
        primary = RecordingFetcher()
        responses = [FakeResponse(self.wrapper), FakeResponse(self.batch)]
        with patch("urllib.request.urlopen", side_effect=responses):
            page = GoogleNewsPageFetcher(primary).fetch(self.google_url)
        self.assertTrue(page.ok)
        self.assertEqual(primary.urls, ["https://publisher.example/story"])
        self.assertEqual(page.resolved_url, "https://publisher.example/story")

    def test_resolution_failure_is_explicit_and_does_not_fetch_wrapper_as_article(self):
        primary = RecordingFetcher()
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"<html></html>")):
            page = GoogleNewsPageFetcher(primary).fetch(self.google_url)
        self.assertFalse(page.ok)
        self.assertIn("resolution failed", page.error)
        self.assertEqual(primary.urls, [])

    def test_non_google_url_passes_through(self):
        primary = RecordingFetcher()
        page = GoogleNewsPageFetcher(primary).fetch("https://publisher.example/direct")
        self.assertTrue(page.ok)
        self.assertEqual(primary.urls, ["https://publisher.example/direct"])


if __name__ == "__main__":
    unittest.main()
