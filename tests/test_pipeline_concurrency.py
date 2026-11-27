"""Proofs for the concurrent discovery/fetch phases in pipeline.run() --
no network.

The claim under test is narrow and specific: running network calls through
a bounded thread pool must change *how long a sweep takes* and nothing
else -- same candidates, same verification, same ranking, same report,
same digest, run after run, regardless of which call happens to finish
first. Speed is checked too, but only as a sanity check that the pool is
actually doing something; the determinism proofs are the ones that matter.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import sys
import threading
import time
import unittest
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import pipeline, record, temporal
from ai_signal.model import Bucket, Candidate, Query, SourceType
from ai_signal.providers import PageContent

IST = temporal.IST
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=IST)

#: Small, deterministic per-call delay pattern (ms), not random -- a fixed
#: cycle is enough to guarantee calls finish in a different order than they
#: were issued (proving order-preservation is doing real work) while
#: keeping the whole suite fast and exactly reproducible between runs.
_DELAYS_MS = [1, 9, 3, 7, 2, 8, 4, 6, 5, 1, 8, 2]


@dataclass
class SlowProvider:
    """Like StaticProvider, but each call sleeps -- so N queries actually
    take N * delay run sequentially, and something meaningfully less than
    that concurrently."""

    name: str
    results: dict[str, list[Candidate]] = field(default_factory=dict)
    _n: int = field(default=0, init=False)

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        i = self._n
        self._n += 1  # test-only tally; not thread-safety-critical here,
        # since the delay pattern only needs to vary, not be exactly counted
        time.sleep(_DELAYS_MS[i % len(_DELAYS_MS)] / 1000)
        out = []
        for proto in self.results.get(query.text, [])[:limit]:
            out.append(
                Candidate(
                    url=proto.url, title=proto.title, bucket=query.bucket,
                    entities=list(proto.entities), body=proto.body,
                    source_name=proto.source_name, source_type=proto.source_type,
                    found_by=[query],
                )
            )
        return out


@dataclass
class SlowFetcher:
    """Like StaticFetcher, but each call sleeps by a varying amount."""

    pages: dict[str, PageContent] = field(default_factory=dict)
    _n: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def fetch(self, url: str) -> PageContent:
        with self._lock:
            i = self._n
            self._n += 1
        time.sleep(_DELAYS_MS[i % len(_DELAYS_MS)] / 1000)
        page = self.pages.get(url)
        if page is None:
            return PageContent(body="", ok=False, error="not in fixture set")
        return page


class _ManyCompanies:
    """20 watchlist companies, each with one real article -- enough
    candidates that a bounded pool of 8 workers needs multiple waves,
    which is exactly the shape a real sweep has."""

    N = 20

    @classmethod
    def terms(cls):
        return [
            pipeline.WatchTerm(f"Company {i}", Bucket.CLIENTS)
            for i in range(cls.N)
        ]

    @classmethod
    def provider(cls):
        results = {}
        for i in range(cls.N):
            url = f"https://trade-press.example/company-{i}"
            results[f'"Company {i}"'] = [
                Candidate(
                    url=url,
                    title=f"Company {i} announces new export order",
                    bucket=Bucket.CLIENTS,
                    entities=[f"Company {i}"],
                )
            ]
        return SlowProvider(name="fixture", results=results)

    @classmethod
    def fetcher(cls):
        pages = {}
        for i in range(cls.N):
            url = f"https://trade-press.example/company-{i}"
            pages[url] = PageContent(
                body=f"Company {i} announced a new export order today, an "
                     f"expansion of its production capacity.",
                published=temporal.DateFinding(NOW, False, "byline"),
                source_type=SourceType.TRADE_PUBLICATION,
                source_name="Trade Press",
            )
        return SlowFetcher(pages=pages)

    @classmethod
    def run(cls, **config_kwargs):
        provider = cls.provider()
        fetcher = cls.fetcher()
        config = pipeline.SweepConfig(**config_kwargs)
        return pipeline.run(cls.terms(), [provider], fetcher, NOW, config)


class TestConcurrencyPreservesTheReport(unittest.TestCase):
    def test_same_digest_across_ten_concurrent_runs(self):
        """The determinism claim, specifically under the new concurrent
        code path: completion order varies run to run (the delay pattern
        guarantees some calls race), the report must not."""
        digests = {record.report_digest(_ManyCompanies.run(max_workers=8)) for _ in range(10)}
        self.assertEqual(len(digests), 1)

    def test_sequential_and_concurrent_produce_an_identical_report(self):
        """max_workers=1 is, in effect, the old sequential loop. Its
        output must match the concurrent path exactly -- same items, same
        order, same everything the digest covers."""
        sequential = record.report_digest(_ManyCompanies.run(max_workers=1))
        concurrent = record.report_digest(_ManyCompanies.run(max_workers=8))
        self.assertEqual(sequential, concurrent)

    def test_verified_item_count_is_unaffected_by_worker_count(self):
        for workers in (1, 2, 8, 32):
            with self.subTest(max_workers=workers):
                res = _ManyCompanies.run(max_workers=workers)
                self.assertEqual(len(res.included[Bucket.CLIENTS]), _ManyCompanies.N)


class TestFetchBudgetUnderConcurrency(unittest.TestCase):
    def test_budget_still_caps_total_fetches_in_rank_order(self):
        res = _ManyCompanies.run(max_workers=8, fetch_budget=5)
        self.assertEqual(len(res.included[Bucket.CLIENTS]), 5)
        self.assertTrue(
            any("fetch budget of 5 reached" in note for note in res.limitations)
        )

    def test_a_budget_at_or_above_the_candidate_count_fetches_everything(self):
        res = _ManyCompanies.run(max_workers=8, fetch_budget=1000)
        self.assertEqual(len(res.included[Bucket.CLIENTS]), _ManyCompanies.N)
        self.assertFalse(any("fetch budget" in note for note in res.limitations))


class TestOneBadProviderDoesNotSinkTheSweep(unittest.TestCase):
    @dataclass
    class _FlakyProvider:
        name: str = "flaky"

        def search(self, query: Query, limit: int = 20) -> list[Candidate]:
            raise RuntimeError("simulated provider outage")

    def test_every_query_still_gets_a_limitation_recorded_not_a_crash(self):
        good = _ManyCompanies.provider()
        bad = self._FlakyProvider()
        res = pipeline.run(
            _ManyCompanies.terms(), [good, bad], _ManyCompanies.fetcher(), NOW,
            pipeline.SweepConfig(max_workers=8),
        )
        # The good provider's results are unaffected by the bad one failing
        # alongside it, concurrently, on every single query.
        self.assertEqual(len(res.included[Bucket.CLIENTS]), _ManyCompanies.N)
        self.assertTrue(any("flaky" in note for note in res.limitations))


class TestMetrics(unittest.TestCase):
    def test_discovery_and_fetch_phases_are_both_recorded(self):
        res = _ManyCompanies.run(max_workers=8)
        phases = {row["phase"] for row in res.metrics}
        self.assertEqual(phases, {"discovery", "fetch"})

    def test_discovery_request_count_matches_the_planned_query_count(self):
        """20 companies is not 20 queries: plan() also emits a dev-verb
        variant per company and grouped OR queries -- the metric should
        reflect exactly what was actually sent, whatever that number is."""
        terms = _ManyCompanies.terms()
        planned = len(pipeline.plan(terms, pipeline.SweepConfig()))
        res = _ManyCompanies.run(max_workers=8)
        by_phase = {row["phase"]: row for row in res.metrics}
        self.assertEqual(by_phase["discovery"]["requests"], planned)
        self.assertEqual(by_phase["discovery"]["success"], planned)

    def test_fetch_request_count_matches_the_unique_candidate_count(self):
        """Unlike discovery, one company has exactly one article URL here,
        however many queries found it -- merge_by_url collapses the
        duplicates before a single page is ever opened."""
        res = _ManyCompanies.run(max_workers=8)
        by_phase = {row["phase"]: row for row in res.metrics}
        self.assertEqual(by_phase["fetch"]["requests"], _ManyCompanies.N)
        self.assertEqual(by_phase["fetch"]["success"], _ManyCompanies.N)


class TestConcurrencyIsActuallyFaster(unittest.TestCase):
    def test_bounded_pool_beats_one_worker_by_a_wide_margin(self):
        """Not a tight timing assertion (real CI machines vary) -- just
        confirms the pool is doing its one job: 20 calls at up to 9ms each
        sequentially is at least ~120ms; concurrently, with 8 workers, it
        cannot exceed 3 waves of the slowest delay plus overhead."""
        t0 = time.monotonic()
        _ManyCompanies.run(max_workers=1)
        sequential_s = time.monotonic() - t0

        t0 = time.monotonic()
        _ManyCompanies.run(max_workers=8)
        concurrent_s = time.monotonic() - t0

        self.assertLess(concurrent_s, sequential_s * 0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
