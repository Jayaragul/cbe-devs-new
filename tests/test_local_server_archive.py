"""Real HTTP-level tests: a live ThreadingHTTPServer, against fixture
providers/fetchers so nothing here touches the network, proving that a
Force refresh actually persists to shared storage and /api/archive
actually reads it back -- not just that the underlying storage helpers
work in isolation (see test_storage.py for those).

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import temporal
from ai_signal.live import local_server, storage as store
from ai_signal.live.demo_provider import DemoFetcher, DemoSearchProvider
from ai_signal.model import Bucket, Candidate
from ai_signal.providers import PageContent, StaticFetcher, StaticProvider

IST = temporal.IST


def _fixture_backend():
    """One watched company, one real-shaped article -- enough for a
    verified item without ever touching the network."""
    url = "https://trade-press.example/story"
    provider = StaticProvider(
        name="fixture",
        results={'"Anthropic"': [Candidate(url=url, title="Anthropic expands AI training capacity", bucket=Bucket.CLIENTS)]},
    )
    fetcher = StaticFetcher(pages={
        url: PageContent(
            body="Anthropic announced an expansion of its AI training capacity today.",
            published=temporal.DateFinding(datetime.now(IST), False, "byline"),
        )
    })
    return provider, fetcher


class _TestServer:
    """Starts the real Handler on an OS-assigned free port, backed by a
    temp-directory LocalStorage and offline fixtures. Torn down after
    each test; nothing here is left running or written outside tempdir.
    """

    def __init__(self, is_live: bool):
        self.tmp = tempfile.TemporaryDirectory()
        provider, fetcher = _fixture_backend()

        class Handler(local_server.Handler):
            pass

        Handler.providers = [provider]
        Handler.fetcher_factory = staticmethod(lambda now: fetcher)
        Handler.is_live = is_live
        Handler.storage = store.LocalStorage(self.tmp.name)
        self._handler_cls = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    def __enter__(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.url(path), timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_expect_error(self, path: str, payload: dict):
        """Like post(), but for requests expected to fail -- urlopen raises
        on a non-2xx status rather than returning it, so the error body
        (which carries the actual message) has to be read from the
        exception instead."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected this request to fail, it did not")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


TERMS_PAYLOAD = {
    "windowHours": 24,
    "terms": [{"name": "Anthropic", "category": "clients", "enabled": True}],
}


class TestForceRefreshSavesToTheArchive(unittest.TestCase):
    def test_a_live_refresh_is_readable_back_from_api_archive(self):
        with _TestServer(is_live=True) as srv:
            report = srv.post("/api/refresh", TERMS_PAYLOAD)
            self.assertEqual(report["totalVerified"], 1)

            archived = srv.get("/api/archive")
            self.assertEqual(len(archived["reports"]), 1)
            self.assertEqual(archived["reports"][0]["totalVerified"], 1)

    def test_two_refreshes_both_land_as_separate_archive_entries(self):
        with _TestServer(is_live=True) as srv:
            srv.post("/api/refresh", TERMS_PAYLOAD)
            srv.post("/api/refresh", TERMS_PAYLOAD)
            archived = srv.get("/api/archive")
            self.assertEqual(len(archived["reports"]), 2)

    def test_archive_starts_empty(self):
        with _TestServer(is_live=True) as srv:
            self.assertEqual(srv.get("/api/archive")["reports"], [])

    def test_a_demo_mode_refresh_is_not_saved(self):
        """Fixture data has no business in the shared archive."""
        with _TestServer(is_live=False) as srv:
            report = srv.post("/api/refresh", TERMS_PAYLOAD)
            self.assertTrue(report["demoData"])
            self.assertEqual(srv.get("/api/archive")["reports"], [])

    def test_archive_respects_a_limit_query_param(self):
        with _TestServer(is_live=True) as srv:
            for _ in range(3):
                srv.post("/api/refresh", TERMS_PAYLOAD)
            self.assertEqual(len(srv.get("/api/archive?limit=2")["reports"]), 2)

    def test_a_save_failure_is_reported_as_a_note_not_a_500(self):
        """The user's live report must not be withheld just because the
        write-back to shared storage happened to fail."""
        with _TestServer(is_live=True) as srv:
            def _boom(key, text):
                raise RuntimeError("simulated bucket outage")
            srv._handler_cls.storage.write_text = _boom
            report = srv.post("/api/refresh", TERMS_PAYLOAD)
            self.assertEqual(report["totalVerified"], 1)
            self.assertTrue(
                any("could not be saved" in n["text"] for n in report["notes"])
            )


class TestRefreshWindowHours(unittest.TestCase):
    """The reporting window is now user-settable from the UI, not fixed at
    24h -- these pin down both the validation at the edge and the
    correctness fix (lookback_for) that makes a wider window actually find
    older items, rather than silently still capping recall at 72h."""

    def _payload(self, window_hours):
        return {**TERMS_PAYLOAD, "windowHours": window_hours}

    def test_a_week_wide_window_is_accepted(self):
        with _TestServer(is_live=True) as srv:
            report = srv.post("/api/refresh", self._payload(24 * 7))
            self.assertEqual(report["totalVerified"], 1)

    def test_zero_is_rejected_with_a_clear_message(self):
        with _TestServer(is_live=True) as srv:
            status, body = srv.post_expect_error("/api/refresh", self._payload(0))
            self.assertEqual(status, 400)
            self.assertIn("windowHours", body["error"])

    def test_negative_is_rejected(self):
        with _TestServer(is_live=True) as srv:
            status, body = srv.post_expect_error("/api/refresh", self._payload(-24))
            self.assertEqual(status, 400)

    def test_beyond_the_30_day_archive_retention_is_rejected(self):
        with _TestServer(is_live=True) as srv:
            status, body = srv.post_expect_error("/api/refresh", self._payload(24 * 365))
            self.assertEqual(status, 400)

    def test_non_numeric_is_rejected_not_a_500(self):
        with _TestServer(is_live=True) as srv:
            status, body = srv.post_expect_error("/api/refresh", self._payload("banana"))
            self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
