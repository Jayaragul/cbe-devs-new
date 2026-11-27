"""Tests for the NSE corporate-filings adapter -- no network.

The JSON below is the real shape returned by
`/api/corporate-announcements?index=equities&symbol=KPRMILL`, captured from
a live response during development (field names, date format and the
`attchmntText` phrasing are verbatim), not invented.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import temporal
from ai_signal.live.nse_filings import (
    FilingsFetcher,
    NseFilingsProvider,
    _parse_broadcast,
)
from ai_signal.model import Bucket, Candidate, Query, SourceType
from ai_signal.providers import PageContent, RoutingFetcher

NOW = datetime(2026, 9, 13, 19, 0, 0, tzinfo=temporal.IST)

REAL_ROWS = [
    {
        "an_dt": "10-Sep-2026 15:41:24",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/KPRMILL_10092026154124.pdf",
        "attchmntText": "K.P.R. Mill Limited has informed the Exchange about Schedule of meet",
        "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
        "sm_name": "K.P.R. Mill Limited",
        "symbol": "KPRMILL",
        "sort_date": "2026-09-10 15:41:24",
    },
    {
        "an_dt": "12-Sep-2026 09:05:00",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/KPRMILL_12092026090500.pdf",
        "attchmntText": (
            "K.P.R. Mill Limited has informed the Exchange that the Board "
            "approved an expansion of its garment capacity."
        ),
        "desc": "Board Meeting Outcome",
        "sm_name": "K.P.R. Mill Limited",
        "symbol": "KPRMILL",
        "sort_date": "2026-09-12 09:05:00",
    },
    {
        # No attachment: nothing citable, must be dropped.
        "an_dt": "12-Sep-2026 10:00:00",
        "attchmntFile": "",
        "attchmntText": "Some announcement with no filed document.",
        "desc": "Other",
        "sm_name": "K.P.R. Mill Limited",
        "symbol": "KPRMILL",
    },
]


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload_by_symbol: dict):
    def _fake(request, timeout=None):
        url = request.full_url
        for symbol, payload in payload_by_symbol.items():
            if f"symbol={symbol}" in url:
                return _FakeResponse(payload)
        return _FakeResponse([])
    return _fake


class TestParseBroadcast(unittest.TestCase):
    def test_parses_nse_format_as_ist(self):
        moment = _parse_broadcast("13-Sep-2026 19:14:31")
        self.assertIsNotNone(moment)
        self.assertEqual(moment.isoformat(), "2026-09-13T19:14:31+05:30")

    def test_unparseable_input_returns_none_rather_than_raising(self):
        for bad in ("", "not a date", None, "2026-09-13"):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_broadcast(bad))


class TestNseFilingsProvider(unittest.TestCase):
    def build(self):
        provider = NseFilingsProvider(symbols=["KPRMILL"], lookback_hours=168)
        return provider, _fake_urlopen({"KPRMILL": REAL_ROWS})

    def test_finds_a_filing_for_a_watchlist_company(self):
        provider, fake = self.build()
        with patch("urllib.request.urlopen", side_effect=fake):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertTrue(hits)
        self.assertIn("KPRMILL", hits[0].url)

    def test_a_filing_without_an_attached_document_is_dropped(self):
        """No filed document means nothing citable to put in a report."""
        provider, fake = self.build()
        with patch("urllib.request.urlopen", side_effect=fake):
            provider._ensure_fresh(NOW)
        self.assertEqual(len(provider._filings), 2)
        self.assertTrue(all(f.url for f in provider._filings))

    def test_filings_are_ordered_newest_first(self):
        provider, fake = self.build()
        with patch("urllib.request.urlopen", side_effect=fake):
            provider._ensure_fresh(NOW)
        moments = [f.broadcast_at for f in provider._filings]
        self.assertEqual(moments, sorted(moments, reverse=True))

    def test_candidates_carry_no_date_of_their_own(self):
        """Same discipline as every other provider: a search result never
        asserts a publication time -- the fetcher does."""
        provider, fake = self.build()
        with patch("urllib.request.urlopen", side_effect=fake):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        for c in hits:
            self.assertIsNone(c.published_at)

    def test_an_unrelated_company_query_matches_nothing(self):
        provider, fake = self.build()
        with patch("urllib.request.urlopen", side_effect=fake):
            hits = provider.search(Query('"Shahi Exports"', Bucket.CLIENTS))
        self.assertEqual(hits, [])

    def test_the_request_is_bounded_by_a_date_range(self):
        """An unbounded symbol query returned 776 records / 551 KB live;
        the bounded one returned 3 / 2 KB. The range is load-bearing."""
        seen = {}

        def capture(request, timeout=None):
            seen["url"] = request.full_url
            return _FakeResponse(REAL_ROWS)

        provider = NseFilingsProvider(symbols=["KPRMILL"], lookback_hours=168)
        with patch("urllib.request.urlopen", side_effect=capture):
            provider._ensure_fresh(NOW)
        self.assertIn("from_date=", seen["url"])
        self.assertIn("to_date=", seen["url"])

    def test_one_failing_symbol_does_not_sink_the_others(self):
        def flaky(request, timeout=None):
            if "symbol=BROKEN" in request.full_url:
                raise OSError("connection reset")
            return _FakeResponse(REAL_ROWS)

        provider = NseFilingsProvider(symbols=["BROKEN", "KPRMILL"], min_interval_s=0.0)
        with patch("urllib.request.urlopen", side_effect=flaky):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertTrue(hits)
        self.assertIn("failed", provider.health()["BROKEN"])
        self.assertIn("ok", provider.health()["KPRMILL"])

    def test_a_non_list_response_is_treated_as_a_failure_not_a_crash(self):
        def html_instead(request, timeout=None):
            return _FakeResponse({"error": "blocked"})

        provider = NseFilingsProvider(symbols=["KPRMILL"], min_interval_s=0.0)
        with patch("urllib.request.urlopen", side_effect=html_instead):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertEqual(hits, [])
        self.assertIn("failed", provider.health()["KPRMILL"])


class TestFilingsFetcher(unittest.TestCase):
    def build(self):
        provider = NseFilingsProvider(symbols=["KPRMILL"], lookback_hours=168)
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen({"KPRMILL": REAL_ROWS})):
            return provider.fetcher()

    def test_supplies_the_exchange_broadcast_timestamp(self):
        fetcher = self.build()
        url = "https://nsearchives.nseindia.com/corporate/KPRMILL_12092026090500.pdf"
        page = fetcher.fetch(url)
        self.assertTrue(page.ok)
        self.assertEqual(page.published.moment.isoformat(), "2026-09-12T09:05:00+05:30")
        self.assertFalse(page.published.date_only)
        self.assertIn("NSE", page.published.provenance)

    def test_marks_the_source_as_a_regulatory_filing(self):
        fetcher = self.build()
        page = fetcher.fetch(
            "https://nsearchives.nseindia.com/corporate/KPRMILL_12092026090500.pdf"
        )
        self.assertIs(page.source_type, SourceType.REGULATORY_FILING)

    def test_body_carries_the_subject_and_the_announcement_text(self):
        fetcher = self.build()
        page = fetcher.fetch(
            "https://nsearchives.nseindia.com/corporate/KPRMILL_12092026090500.pdf"
        )
        self.assertIn("Board Meeting Outcome", page.body)
        self.assertIn("expansion", page.body)

    def test_an_unknown_url_is_refused_rather_than_guessed_at(self):
        fetcher = self.build()
        page = fetcher.fetch("https://example.com/not-a-filing")
        self.assertFalse(page.ok)
        self.assertFalse(fetcher.handles("https://example.com/not-a-filing"))


class TestFilingMateriality(unittest.TestCase):
    """Subjects below are verbatim NSE `desc` values from live filings for
    the watchlist's listed clients. Measured before this check existed, the
    news-verb test admitted 0 of 29 real filings -- including two board
    outcomes and a credit-rating revision."""

    def judge(self, subject, text="", source=SourceType.REGULATORY_FILING):
        from ai_signal.verify import development_for_source
        return development_for_source(
            Candidate(
                url="https://nsearchives.nseindia.com/corporate/X.pdf",
                title=f"Kitex Garments Limited: {subject}",
                bucket=Bucket.CLIENTS,
                body=f"{subject}. {text}",
                source_type=source,
            )
        )

    def test_material_categories_are_admitted(self):
        for subject in ("Outcome of Board Meeting", "Credit Rating- Revision",
                        "Credit Rating", "Financial Results", "Acquisition"):
            with self.subTest(subject=subject):
                self.assertTrue(self.judge(subject), f"{subject!r} is material")

    def test_routine_housekeeping_is_refused(self):
        for subject in ("Copy of Newspaper Publication", "Shareholders meeting",
                        "Analysts/Institutional Investor Meet/Con. Call Updates",
                        "Record Date", "Trading Window"):
            with self.subTest(subject=subject):
                self.assertFalse(self.judge(subject), f"{subject!r} is routine")

    def test_routine_wins_over_a_material_word_appearing_in_the_text(self):
        """An AGM notice that happens to mention an acquisition in passing is
        still an AGM notice -- the category is the better evidence."""
        self.assertFalse(
            self.judge("Shareholders meeting",
                       "Notice of AGM. Agenda includes approval of an acquisition.")
        )

    def test_an_ambiguous_category_falls_back_to_the_cue_test(self):
        """NSE's catch-all "Updates" covers both a book closure and a board
        outcome, so the category alone cannot decide it."""
        self.assertTrue(
            self.judge("General Updates",
                       "The Board approved and announced an expansion of capacity.")
        )
        self.assertFalse(
            self.judge("General Updates", "Intimation of book closure dates.")
        )

    def test_a_news_article_is_still_judged_as_news(self):
        """The filing vocabulary must not leak onto ordinary trade press: a
        news page whose text merely contains "order" is not thereby a
        development."""
        self.assertFalse(
            self.judge("Company profile", "An order of magnitude more efficient.",
                       source=SourceType.TRADE_PUBLICATION)
        )


class TestRoutingFetcher(unittest.TestCase):
    class _Primary:
        def __init__(self):
            self.calls = []

        def fetch(self, url):
            self.calls.append(url)
            return PageContent(body="from the web", ok=True)

    def test_a_filing_url_goes_to_the_specialist_not_over_http(self):
        primary = self._Primary()
        specialist = FilingsFetcher({"https://x/f.pdf": None})

        # A specialist that handles the url must be consulted first, even
        # though this one will refuse -- what matters is that the primary
        # never saw it.
        router = RoutingFetcher(primary=primary, specialists=[specialist])
        router.fetch("https://x/f.pdf")
        self.assertEqual(primary.calls, [])

    def test_everything_else_falls_through_to_the_primary(self):
        primary = self._Primary()
        router = RoutingFetcher(primary=primary, specialists=[FilingsFetcher({})])
        page = router.fetch("https://news.example/story")
        self.assertEqual(primary.calls, ["https://news.example/story"])
        self.assertEqual(page.body, "from the web")

    def test_with_no_specialists_it_is_a_transparent_passthrough(self):
        primary = self._Primary()
        router = RoutingFetcher(primary=primary)
        router.fetch("https://news.example/story")
        self.assertEqual(primary.calls, ["https://news.example/story"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
