"""Proves window_hours (report inclusion) and lookback_hours (search recall)
are genuinely decoupled -- the specific claim behind the 24h/72h split.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import pipeline, record, temporal
from ai_signal.model import Bucket, Query
from ai_signal.providers import StaticFetcher

IST = temporal.IST
NOW = datetime(2026, 9, 7, 11, 18, 46, tzinfo=IST)


class TestLookbackConfig(unittest.TestCase):
    def test_defaults_match_the_intended_split(self):
        cfg = pipeline.SweepConfig()
        self.assertEqual(cfg.window_hours, 24)
        self.assertEqual(cfg.lookback_hours, 72)

    def test_plan_stamps_every_query_with_the_configured_lookback(self):
        terms = [
            pipeline.WatchTerm("K.P.R. Mill", Bucket.CLIENTS, ["KPR Mill"]),
            pipeline.WatchTerm("Coats Digital", Bucket.COMPETITORS),
            pipeline.WatchTerm('("garment") AND ("AI")', Bucket.TOPICS),
        ]
        queries = pipeline.plan(terms, pipeline.SweepConfig(lookback_hours=48))
        self.assertTrue(queries, "plan produced no queries")
        self.assertTrue(all(q.lookback_hours == 48 for q in queries))

    def test_default_plan_call_uses_72h_lookback(self):
        """plan() with no config still stamps the 72h default -- callers
        that pass only `terms` (as every earlier test in this suite does)
        must not silently get an unstamped or zero lookback."""
        terms = [pipeline.WatchTerm('("AI")', Bucket.TOPICS)]
        queries = pipeline.plan(terms)
        self.assertTrue(all(q.lookback_hours == 72 for q in queries))

    def test_run_stamps_exact_retrieval_bounds_from_its_explicit_clock(self):
        class CapturingProvider:
            name = "capture"

            def __init__(self):
                self.queries = []

            def search(self, query, limit=20):
                self.queries.append(query)
                return []

        provider = CapturingProvider()
        pipeline.run(
            [pipeline.WatchTerm('"AI"', Bucket.TOPICS)],
            [provider],
            StaticFetcher(),
            NOW,
            pipeline.SweepConfig(lookback_hours=48),
        )
        self.assertTrue(provider.queries)
        self.assertTrue(all(q.retrieval_end == NOW for q in provider.queries))
        self.assertTrue(
            all(q.retrieval_start == NOW - timedelta(hours=48) for q in provider.queries)
        )


class TestDecoupling(unittest.TestCase):
    """The report must depend on window_hours and be indifferent to
    lookback_hours -- the whole point of separating the two knobs."""

    def build(self):
        # Not `from tests.test_determinism import ...`: a real `tests`
        # package elsewhere on sys.path (e.g. in site-packages) can shadow
        # this project's own `tests/` directory, since a regular package
        # wins over a namespace package regardless of path order. Every
        # other cross-file import in this suite relies on the same
        # top-level module name that `unittest discover -s tests` puts on
        # sys.path, so this matches that pattern instead.
        from test_determinism import _Fixture

        return _Fixture

    def test_widening_lookback_alone_does_not_change_what_is_reported(self):
        F = self.build()
        baseline = pipeline.run(
            F.terms(), [F.provider()], F.fetcher(), NOW,
            pipeline.SweepConfig(window_hours=24, lookback_hours=72),
        )
        wider_net = pipeline.run(
            F.terms(), [F.provider()], F.fetcher(), NOW,
            pipeline.SweepConfig(window_hours=24, lookback_hours=168),
        )
        self.assertEqual(
            record.report_digest(baseline), record.report_digest(wider_net),
            msg="lookback_hours must not affect which items are included",
        )

    def test_narrowing_window_alone_changes_what_is_reported(self):
        """The contrasting case, so the test above cannot pass by both
        configs simply always agreeing regardless of any setting."""
        F = self.build()
        with_24h_window = pipeline.run(
            F.terms(), [F.provider()], F.fetcher(), NOW,
            pipeline.SweepConfig(window_hours=24, lookback_hours=72),
        )
        with_1h_window = pipeline.run(
            F.terms(), [F.provider()], F.fetcher(), NOW,
            pipeline.SweepConfig(window_hours=1, lookback_hours=72),
        )
        self.assertNotEqual(
            record.report_digest(with_24h_window),
            record.report_digest(with_1h_window),
        )

    def test_an_old_candidate_is_rejected_even_if_the_search_net_was_wide(self):
        """The concrete failure mode this design prevents: a 72h-wide
        search surfaces something from 50 hours ago and it must still be
        refused by the gate, never smuggled into a 24h report."""
        F = self.build()
        res = pipeline.run(
            F.terms(), [F.provider()], F.fetcher(), NOW,
            pipeline.SweepConfig(window_hours=24, lookback_hours=72),
        )
        self.assertNotIn(
            F.URL_STALE,  # the 19 Aug item, real-world stale by weeks
            [v.candidate.url for group in res.included.values() for v in group],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
