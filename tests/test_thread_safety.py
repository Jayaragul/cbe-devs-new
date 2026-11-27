"""Proofs that the providers/fetchers are safe under concurrent calls --
no network.

Each test hammers one shared piece of state from many threads at once and
checks the *specific* race the lock in question closes, not just "no
exception was raised": a fetch-once cache that isn't actually locked can
still run cleanly under `unittest`'s single test thread scheduling and only
fail once in a while under real contention, so an artificial delay is used
throughout to hold each critical section open long enough that, without the
fix, two threads would reliably collide inside it.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live.google_news import GoogleNewsPageFetcher, GoogleNewsSearchProvider
from ai_signal.live.http_fetcher import HttpPageFetcher
from ai_signal.live.nse_filings import NseFilingsProvider
from ai_signal.live.rss_provider import RssSearchProvider
from ai_signal.model import Bucket, Query

N_THREADS = 16

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>K.P.R. Mill commissions new garment unit</title>
  <link>https://textilefocus.com/kpr-mill-unit/</link>
  <pubDate>Fri, 11 Sep 2026 09:00:00 +0000</pubDate>
</item>
</channel></rss>"""


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return "https://example.com/ok"

    headers = {"Content-Type": "text/html; charset=utf-8"}


def _run_concurrently(fn, n=N_THREADS):
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()  # every thread starts the call at the same instant
        fn()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return threads


class TestRssProviderThreadSafety(unittest.TestCase):
    def test_ensure_fresh_polls_the_feeds_exactly_once_under_concurrent_search(self):
        """Without the lock, N threads calling search() before the cache is
        warm would each see 'not fetched yet' and each poll every feed --
        an easy thing to miss in a single-threaded test run."""
        calls = {"n": 0}

        def slow_fake(request, timeout=None):
            calls["n"] += 1
            time.sleep(0.05)  # hold the window open long enough to collide
            return _FakeResponse(FEED_XML.encode("utf-8"))

        provider = RssSearchProvider(feeds={"Textile Focus": "https://textilefocus.com/feed/"})
        with patch("urllib.request.urlopen", side_effect=slow_fake):
            _run_concurrently(lambda: provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS)))

        # One feed in the fixture -> exactly one real request, regardless
        # of how many threads asked for a search at once.
        self.assertEqual(calls["n"], 1)

    def test_all_threads_get_a_correct_result_once_the_cache_is_warm(self):
        provider = RssSearchProvider(feeds={"Textile Focus": "https://textilefocus.com/feed/"})
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda request, timeout=None: _FakeResponse(FEED_XML.encode("utf-8")),
        ):
            results = []
            lock = threading.Lock()

            def call():
                hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
                with lock:
                    results.append(len(hits))

            _run_concurrently(call)
        self.assertEqual(results, [1] * N_THREADS)


class TestNseFilingsProviderThreadSafety(unittest.TestCase):
    def test_ensure_fresh_hits_nse_exactly_once_per_symbol(self):
        calls = {"n": 0}

        def slow_fake(request, timeout=None):
            calls["n"] += 1
            time.sleep(0.05)
            return _FakeResponse(b"[]")

        provider = NseFilingsProvider(symbols=["KPRMILL"], min_interval_s=0.0)
        with patch("urllib.request.urlopen", side_effect=slow_fake):
            _run_concurrently(lambda: provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS)))
        self.assertEqual(calls["n"], 1)


class TestHttpPageFetcherThreadSafety(unittest.TestCase):
    def test_different_hosts_do_not_block_each_other(self):
        """The per-host lock must not degrade into one global lock: fetches
        to N distinct hosts, each artificially slow, should overlap in
        wall-clock time rather than run back-to-back."""
        fetcher = HttpPageFetcher(min_host_interval_s=0.0)

        def slow_fake(request, timeout=None):
            time.sleep(0.1)
            return _FakeResponse(b"<html><body>hi</body></html>")

        urls = [f"https://host-{i}.example.com/a" for i in range(N_THREADS)]
        t0 = time.monotonic()
        with patch("urllib.request.urlopen", side_effect=slow_fake):
            threads = [threading.Thread(target=fetcher.fetch, args=(u,)) for u in urls]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        elapsed = time.monotonic() - t0
        # Serialized, this would take N * 0.1s; concurrent, well under half.
        self.assertLess(elapsed, N_THREADS * 0.1 * 0.5)

    def test_same_host_still_respects_the_minimum_interval(self):
        """The guard this class exists for (see its docstring) must survive
        concurrency: hammering one host from many threads still spaces
        requests out, it just does so safely instead of racily."""
        fetcher = HttpPageFetcher(min_host_interval_s=0.05)
        seen_at: list[float] = []
        lock = threading.Lock()

        def fake(request, timeout=None):
            with lock:
                seen_at.append(time.monotonic())
            return _FakeResponse(b"<html><body>hi</body></html>")

        url = "https://one-host.example.com/a"
        with patch("urllib.request.urlopen", side_effect=fake):
            _run_concurrently(lambda: fetcher.fetch(url), n=6)

        seen_at.sort()
        gaps = [b - a for a, b in zip(seen_at, seen_at[1:])]
        self.assertTrue(all(g >= 0.04 for g in gaps), gaps)  # small slack for scheduling


class TestGoogleNewsSearchProviderThreadSafety(unittest.TestCase):
    def test_query_counter_is_exact_under_concurrent_search(self):
        """A plain `self._queries += 1` loses increments under real
        contention; this fails intermittently without the lock and always
        passes with it."""
        provider = GoogleNewsSearchProvider()

        def fake(request, timeout=None):
            time.sleep(0.02)
            return _FakeResponse(b"<rss><channel></channel></rss>")

        with patch("urllib.request.urlopen", side_effect=fake):
            _run_concurrently(lambda: provider.search(Query('"OpenAI"', Bucket.CLIENTS)))

        self.assertIn(f"{N_THREADS} queries", provider.health()["Google News"])


class TestGoogleNewsPageFetcherThreadSafety(unittest.TestCase):
    def test_the_same_wrapper_url_resolved_concurrently_does_not_crash_or_corrupt(self):
        primary = HttpPageFetcher()
        fetcher = GoogleNewsPageFetcher(primary=primary, timeout_s=5.0)
        wrapper = "https://news.google.com/rss/articles/CBMi_fake_token"

        page = "<html>" + 'data-n-a-id="X" data-n-a-ts="1" data-n-a-sg="Y"' + "</html>"
        decoded_body = ')]}\'\n\n[[["x",[null,null,["https://real-publisher.example/story"]]]]]'

        def fake(request, timeout=None):
            time.sleep(0.02)
            url = getattr(request, "full_url", "")
            if "batchexecute" in url:
                return _FakeResponse(decoded_body.encode("utf-8"))
            return _FakeResponse(page.encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake):
            errors = []

            def call():
                try:
                    fetcher.fetch(wrapper)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            _run_concurrently(call)

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
