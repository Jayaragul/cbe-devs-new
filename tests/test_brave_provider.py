"""Tests for BraveSearchProvider against a mocked HTTP layer.

No network access, no real API key -- `urllib.request.urlopen` is patched
to return a canned response matching Brave's documented schema (confirmed
live against api-dashboard.search.brave.com/app/documentation/web-search
during development: top-level `{"web": {"results": [...]}}`, each result
carrying `title`, `url`, `description`). This cannot prove the real API
still matches that shape at any given moment -- see brave.py's own
"UNVERIFIED AGAINST THE REAL API" warning -- but it does lock in that the
parsing code is at least *internally consistent* with what was actually
observed, which a units-of-code review alone cannot confirm.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live.brave import BraveApiKeyMissing, BraveSearchProvider, _freshness_for
from ai_signal.model import Bucket, Query

BRAVE_SHAPED_RESPONSE = {
    "web": {
        "results": [
            {
                "title": "K.P.R. Mill commissions new garment unit in Tiruppur",
                "url": "https://www.fibre2fashion.com/news/textile-news/example-1",
                "description": "The textile exporter said commercial production begins next quarter.",
                "age": "2 hours ago",
            },
            {
                "title": "K.P.R. Mill Limited - Company Overview",
                "url": "https://www.zoominfo.com/c/kpr-mill/12345",
                "description": "Company profile and contact information.",
            },
        ]
    }
}


class _FakeResponse:
    """Stands in for the object `urllib.request.urlopen`'s context manager
    yields: needs only `.read()` and to work as a context manager."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestBraveSearchProviderParsing(unittest.TestCase):
    def setUp(self):
        self.provider = BraveSearchProvider(api_key="fake-test-key", min_interval_s=0.0)

    def test_parses_brave_shaped_results_into_candidates(self):
        with patch("urllib.request.urlopen", return_value=_FakeResponse(BRAVE_SHAPED_RESPONSE)):
            hits = self.provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS), limit=20)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://www.fibre2fashion.com/news/textile-news/example-1")
        self.assertEqual(hits[0].title, "K.P.R. Mill commissions new garment unit in Tiruppur")
        self.assertIn("commercial production", hits[0].snippet)
        self.assertEqual(hits[0].bucket, Bucket.CLIENTS)
        self.assertEqual(hits[0].found_by, [Query('"K.P.R. Mill"', Bucket.CLIENTS)])

    def test_a_result_missing_url_is_skipped_not_crashed_on(self):
        broken = {"web": {"results": [{"title": "no url here", "description": "x"}]}}
        with patch("urllib.request.urlopen", return_value=_FakeResponse(broken)):
            hits = self.provider.search(Query("x", Bucket.TOPICS))
        self.assertEqual(hits, [])

    def test_missing_web_key_returns_no_hits_not_an_error(self):
        with patch("urllib.request.urlopen", return_value=_FakeResponse({})):
            hits = self.provider.search(Query("x", Bucket.TOPICS))
        self.assertEqual(hits, [])

    def test_the_request_carries_the_key_as_a_header_not_a_query_param(self):
        """The key must never end up somewhere it would be logged in a URL
        (server access logs, browser history, proxy logs) -- Brave's own
        auth scheme puts it in a header, and this must not silently regress
        to a query parameter."""
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            captured["url"] = request.full_url
            return _FakeResponse(BRAVE_SHAPED_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.provider.search(Query("x", Bucket.TOPICS))
        self.assertIn("X-subscription-token", captured["headers"])  # urllib title-cases headers
        self.assertEqual(captured["headers"]["X-subscription-token"], "fake-test-key")
        self.assertNotIn("fake-test-key", captured["url"])

    def test_a_bad_key_surfaces_brave_s_own_structured_error(self):
        """Observed live against the real endpoint with a deliberately
        invalid key: Brave returns HTTP 422 (not 401/403) with a
        structured JSON body naming the exact problem. Guessing a generic
        "401/403 means a bad key" hint, as an earlier version did, would
        have produced a *wrong* explanation for the single most common
        real failure -- reading the actual body is what gets this right."""
        body = json.dumps({
            "error": {
                "code": "SUBSCRIPTION_TOKEN_INVALID",
                "detail": "The provided subscription token is invalid.",
                "meta": {"component": "authentication"},
                "status": 422,
            },
            "type": "ErrorResponse",
        }).encode("utf-8")
        err = urllib.error.HTTPError("url", 422, "", {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.provider.search(Query("x", Bucket.TOPICS))
        self.assertIn("SUBSCRIPTION_TOKEN_INVALID", str(ctx.exception))
        self.assertIn("subscription token is invalid", str(ctx.exception))

    def test_an_error_body_that_is_not_json_falls_back_to_the_status(self):
        err = urllib.error.HTTPError("url", 500, "Internal Server Error", {}, io.BytesIO(b"not json"))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.provider.search(Query("x", Bucket.TOPICS))
        self.assertIn("500", str(ctx.exception))

    def test_http_429_with_no_body_reports_the_rate_limit_specifically(self):
        err = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.provider.search(Query("x", Bucket.TOPICS))
        self.assertIn("rate limit", str(ctx.exception))

    def test_network_failure_is_a_runtime_error_not_left_uncaught(self):
        err = urllib.error.URLError("no route to host")
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                self.provider.search(Query("x", Bucket.TOPICS))
        self.assertIn("could not reach", str(ctx.exception))


class TestBraveThrottle(unittest.TestCase):
    def test_consecutive_calls_are_paced_at_least_min_interval_apart(self):
        interval = 0.2  # generous relative to typical OS timer granularity
        provider = BraveSearchProvider(api_key="fake", min_interval_s=interval)
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"web": {"results": []}})):
            import time
            t0 = time.monotonic()
            provider.search(Query("a", Bucket.TOPICS))
            provider.search(Query("b", Bucket.TOPICS))
            elapsed = time.monotonic() - t0
        # A small tolerance for OS scheduling jitter (Windows' sleep timer
        # can wake a few ms early); the point being tested is "meaningfully
        # paced," not sub-millisecond precision.
        self.assertGreaterEqual(elapsed, interval * 0.9)

    def test_first_call_is_not_delayed(self):
        provider = BraveSearchProvider(api_key="fake", min_interval_s=5.0)
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"web": {"results": []}})):
            import time
            t0 = time.monotonic()
            provider.search(Query("a", Bucket.TOPICS))
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)  # no wait before the very first request


class TestFreshnessMapping(unittest.TestCase):
    def test_maps_lookback_hours_to_the_narrowest_covering_preset(self):
        self.assertEqual(_freshness_for(1), "pd")
        self.assertEqual(_freshness_for(24), "pd")
        self.assertEqual(_freshness_for(25), "pw")
        self.assertEqual(_freshness_for(24 * 7), "pw")
        self.assertEqual(_freshness_for(24 * 7 + 1), "pm")
        self.assertEqual(_freshness_for(24 * 31 + 1), "py")


class TestApiKeyHandling(unittest.TestCase):
    def test_from_env_raises_a_clear_error_when_unset(self):
        import os
        env_backup = os.environ.pop("BRAVE_API_KEY", None)
        try:
            with self.assertRaises(BraveApiKeyMissing):
                BraveSearchProvider.from_env()
        finally:
            if env_backup is not None:
                os.environ["BRAVE_API_KEY"] = env_backup

    def test_from_env_reads_the_named_variable(self):
        import os
        env_backup = os.environ.get("BRAVE_API_KEY")
        os.environ["BRAVE_API_KEY"] = "test-value-not-a-real-key"
        try:
            provider = BraveSearchProvider.from_env()
            self.assertEqual(provider.api_key, "test-value-not-a-real-key")
        finally:
            if env_backup is None:
                os.environ.pop("BRAVE_API_KEY", None)
            else:
                os.environ["BRAVE_API_KEY"] = env_backup


if __name__ == "__main__":
    unittest.main(verbosity=2)
