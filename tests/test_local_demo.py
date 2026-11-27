"""Tests for the Force-refresh demo path: report_json, demo_provider, and
local_server's pure request-parsing helper. No network, no server started
-- importing local_server does not start it (guarded by __main__).

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import pipeline, temporal
from ai_signal.model import Bucket, Rejection
from ai_signal.live import local_server, report_json
from ai_signal.live.demo_provider import DEMO_ARTICLES, DemoFetcher, DemoSearchProvider

IST = temporal.IST
NOW = datetime(2026, 9, 11, 12, 40, tzinfo=IST)


class TestDemoMatching(unittest.TestCase):
    def setUp(self):
        self.provider = DemoSearchProvider()

    def q(self, text, bucket=Bucket.TOPICS):
        from ai_signal.model import Query
        return Query(text, bucket)

    def test_company_name_matches_its_own_demo_article(self):
        hits = self.provider.search(self.q('"OpenAI"', Bucket.CLIENTS))
        self.assertTrue(any("reasoning-model" in c.url for c in hits))

    def test_a_newly_added_plausible_topic_can_match(self):
        """The point of the demo: adding a topic the fixture set anticipates
        should surface something, without any code change."""
        hits = self.provider.search(self.q('"model benchmark"'))
        self.assertTrue(any("model-benchmark" in c.url for c in hits))

    def test_an_unrelated_topic_matches_nothing(self):
        hits = self.provider.search(self.q('"quantum computing hardware"'))
        self.assertEqual(hits, [])

    def test_every_demo_article_is_reachable_by_some_query(self):
        """Guards the fixture itself: an article with no plausible matching
        query would be permanently invisible and pointless to keep. A query
        built from that article's own first three title words must find it."""
        for article in DEMO_ARTICLES:
            words = article.title.split()
            hits = self.provider.search(self.q(" ".join(words[:3])))
            self.assertTrue(
                any(c.url == article.url for c in hits),
                msg=f"{article.url} unreachable by its own title",
            )


class TestReportJson(unittest.TestCase):
    def build_result(self):
        terms = [
            pipeline.WatchTerm("OpenAI", Bucket.CLIENTS, ["ChatGPT"]),
            pipeline.WatchTerm("NVIDIA", Bucket.COMPETITORS),
            pipeline.WatchTerm("Future Model Systems", Bucket.COMPETITORS),
            pipeline.WatchTerm("Meta AI", Bucket.CLIENTS, ["Meta"]),
            pipeline.WatchTerm('"model benchmark"', Bucket.TOPICS),
        ]
        return pipeline.run(
            terms, [DemoSearchProvider()], DemoFetcher(reference_time=NOW), NOW,
            pipeline.SweepConfig(window_hours=24, lookback_hours=72),
        )

    def test_shape_matches_the_frontend_contract(self):
        result = self.build_result()
        report = report_json.sweep_result_to_report(result)
        for key in ("date", "windowStart", "windowEnd", "checkedAt",
                    "totalVerified", "buckets", "notes", "demoData", "demoNote"):
            self.assertIn(key, report)
        for bucket_key in ("clients", "competitors", "topics"):
            self.assertIn(bucket_key, report["buckets"])
            self.assertIn("items", report["buckets"][bucket_key])
            self.assertIn("emptyNote", report["buckets"][bucket_key])
        self.assertTrue(report["demoData"])

    def test_live_report_carries_no_demo_note(self):
        """A `demo=False` report must not claim, even in an unrendered JSON
        field a client happens not to display, that it came from a local
        fixture set -- that was true before this fix, when the "not live"
        default note applied unconditionally regardless of `demo`."""
        result = self.build_result()
        report = report_json.sweep_result_to_report(result, demo=False)
        self.assertFalse(report["demoData"])
        self.assertIsNone(report["demoNote"])

    def test_admitted_items_carry_every_field_the_ui_reads(self):
        result = self.build_result()
        report = report_json.sweep_result_to_report(result)
        clients = report["buckets"]["clients"]["items"]
        self.assertTrue(clients, "expected the fresh OpenAI demo item to be admitted")
        item = clients[0]
        for key in ("topicOrCompany", "title", "sourceName", "sourceType",
                    "publishedDisplay", "url", "promotional"):
            self.assertIn(key, item)
        self.assertIn("OpenAI", item["topicOrCompany"])

    def test_topic_bucket_items_show_the_matched_query_not_a_repeated_title(self):
        """A topic-bucket item has no entity by design (topics aren't
        companies). The "topic or company" column must not just repeat the
        headline next to itself -- it should say which watched query the
        article satisfied."""
        result = self.build_result()
        report = report_json.sweep_result_to_report(result)
        topics = report["buckets"]["topics"]["items"]
        self.assertTrue(topics, "expected the model-benchmark demo item to be admitted")
        item = topics[0]
        self.assertNotEqual(item["topicOrCompany"], item["title"][:60],
                            "topicOrCompany must not just be a truncated repeat of the title")
        self.assertIn("model benchmark", item["topicOrCompany"].lower())

    def test_a_shared_competitor_watchlist_term_does_not_steal_a_topic_article(self):
        """Regression for query overlap. "Future Model Systems" and
        "model benchmark" share the word "model", and because candidates are
        deduplicated by URL across every query in query-plan order, the
        competitor query -- built first -- used to "win" and tag the
        article as a competitors-bucket candidate, which then failed
        entity resolution and vanished instead of appearing under topics."""
        result = self.build_result()
        topic_urls = {v.candidate.url for v in result.included.get(Bucket.TOPICS, [])}
        self.assertTrue(
            any("model-benchmark" in u for u in topic_urls),
            msg="the model-benchmark article should surface under Topics, not be "
                "silently claimed by the unrelated Future Model Systems competitor query",
        )

    def test_json_serialisable(self):
        import json
        result = self.build_result()
        report = report_json.sweep_result_to_report(result)
        json.dumps(report)  # raises if anything is not JSON-safe

    def test_stale_and_mismatched_demo_items_are_reflected_in_notes(self):
        """The stale NVIDIA item and the Meta Materials entity
        mismatch should show up as concrete exclusions, not vanish."""
        result = self.build_result()
        report = report_json.sweep_result_to_report(result)
        self.assertTrue(report["notes"], "expected at least one verification note")
        reasons = result.rejections_by_reason()
        self.assertIn(Rejection.OUT_OF_WINDOW.value, reasons)
        self.assertIn(Rejection.ENTITY_MISMATCH.value, reasons)


class TestLocalServerRequestParsing(unittest.TestCase):
    def test_parses_enabled_terms_by_category(self):
        payload = {
            "terms": [
                {"name": "OpenAI", "category": "clients", "aliases": ["ChatGPT"], "enabled": True},
                {"name": "NVIDIA", "category": "competitors", "enabled": True},
                {"name": "Retired Term", "category": "clients", "enabled": False},
            ]
        }
        terms = local_server._terms_from_payload(payload)
        self.assertEqual(len(terms), 3)  # disabled terms are still parsed; caller filters
        kpr = next(t for t in terms if t.name == "OpenAI")
        self.assertEqual(kpr.bucket, Bucket.CLIENTS)
        self.assertEqual(kpr.aliases, ["ChatGPT"])

    def test_unknown_category_falls_back_to_clients(self):
        terms = local_server._terms_from_payload({"terms": [{"name": "X", "category": "bogus"}]})
        self.assertEqual(terms[0].bucket, Bucket.CLIENTS)

    def test_blank_name_is_skipped(self):
        terms = local_server._terms_from_payload({"terms": [{"name": "   "}]})
        self.assertEqual(terms, [])

    def test_missing_terms_key_returns_empty_list(self):
        self.assertEqual(local_server._terms_from_payload({}), [])


class TestDemoFetcherClock(unittest.TestCase):
    """Regression for a real bug: an earlier `DemoFetcher` read the real
    wall clock (`datetime.now()`) instead of an explicit reference time.
    That broke silently -- every test in this file started failing the
    day after it was written, because `pipeline.run`'s window stayed
    pinned to the test's fixed `NOW` while the fetcher's timestamps kept
    advancing with real time, eventually pushing every demo article
    "published" after the window closed."""

    def test_published_at_is_derived_from_reference_time_not_the_real_clock(self):
        far_future = datetime(2099, 1, 1, tzinfo=IST)
        fetcher = DemoFetcher(reference_time=far_future)
        article = DEMO_ARTICLES[0]
        page = fetcher.fetch(article.url)
        self.assertTrue(page.ok)
        # Computed relative to the far-future reference (minus a few hours
        # for the article's own freshness offset), nowhere near the real
        # wall-clock date this test actually runs on.
        self.assertGreaterEqual(page.published.moment.year, 2098)

    def test_two_fetches_with_the_same_reference_time_are_identical(self):
        ref = datetime(2026, 9, 11, 12, 40, tzinfo=IST)
        article = DEMO_ARTICLES[0]
        a = DemoFetcher(reference_time=ref).fetch(article.url)
        b = DemoFetcher(reference_time=ref).fetch(article.url)
        self.assertEqual(a.published.moment, b.published.moment)

    def test_demo_fetcher_has_no_default_reference_time(self):
        """`reference_time` must stay a required field -- a default (even a
        `None` meaning "use real time") would silently reopen this exact
        bug for any future caller who forgets to pass one."""
        with self.assertRaises(TypeError):
            DemoFetcher()  # type: ignore[call-arg]


class TestBuildBackend(unittest.TestCase):
    """`build_backend` branches on environment presence alone -- these
    exercise every branch without touching the real environment, a real
    key, or the network. RSS is the default live backend (no key needed);
    Brave is a purely optional addition; AI_SIGNAL_DEMO_MODE is an explicit
    escape hatch back to the offline fixture set."""

    def test_default_with_no_env_vars_selects_live_rss_search(self):
        """The important behavioural change this session: real, keyless
        search is now the *default*, not something requiring a signup."""
        from ai_signal.live.http_fetcher import HttpPageFetcher
        from ai_signal.live.rss_provider import RssSearchProvider

        providers, fetcher_factory, is_live, note = local_server.build_backend(env={})
        self.assertTrue(is_live)
        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], RssSearchProvider)
        fetcher = fetcher_factory(datetime(2026, 9, 11, tzinfo=IST))
        self.assertIsInstance(fetcher, HttpPageFetcher)
        self.assertIn("no API key needed", note)

    def test_demo_override_forces_the_offline_dataset(self):
        providers, fetcher_factory, is_live, note = local_server.build_backend(
            env={"AI_SIGNAL_DEMO_MODE": "1"}
        )
        self.assertFalse(is_live)
        self.assertIsInstance(providers[0], DemoSearchProvider)
        fetcher = fetcher_factory(datetime(2026, 9, 11, tzinfo=IST))
        self.assertIsInstance(fetcher, DemoFetcher)
        self.assertIn("forcing the offline demo dataset", note)

    def test_demo_override_wins_even_if_a_brave_key_is_also_set(self):
        _, _, is_live, note = local_server.build_backend(
            env={"AI_SIGNAL_DEMO_MODE": "1", "BRAVE_API_KEY": "fake-key"}
        )
        self.assertFalse(is_live)
        self.assertIn("forcing the offline demo dataset", note)

    def test_a_brave_key_adds_brave_alongside_rss_not_instead_of_it(self):
        from ai_signal.live.brave import BraveSearchProvider
        from ai_signal.live.rss_provider import RssSearchProvider

        providers, fetcher_factory, is_live, note = local_server.build_backend(
            env={"BRAVE_API_KEY": "fake-test-key-not-a-real-secret"}
        )
        self.assertTrue(is_live)
        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], RssSearchProvider)
        self.assertIsInstance(providers[1], BraveSearchProvider)
        self.assertIn("also set", note)

    def test_blank_brave_key_is_treated_as_unset(self):
        providers, _, is_live, _ = local_server.build_backend(env={"BRAVE_API_KEY": ""})
        self.assertTrue(is_live)  # RSS alone still makes this live
        self.assertEqual(len(providers), 1)

    def test_google_news_is_explicitly_opt_in(self):
        from ai_signal.live.google_news import GoogleNewsSearchProvider

        providers, _, is_live, note = local_server.build_backend(
            env={"AI_SIGNAL_GOOGLE_NEWS": "1"}
        )
        self.assertTrue(is_live)
        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[1], GoogleNewsSearchProvider)
        self.assertIn("personal/non-commercial", note)

    def test_false_google_news_value_does_not_enable_it(self):
        providers, _, _, _ = local_server.build_backend(
            env={"AI_SIGNAL_GOOGLE_NEWS": "0"}
        )
        self.assertEqual(len(providers), 1)

    def test_demo_fetcher_factory_uses_the_time_it_is_given(self):
        _, fetcher_factory, _, _ = local_server.build_backend(env={"AI_SIGNAL_DEMO_MODE": "1"})
        t1 = datetime(2020, 1, 1, tzinfo=IST)
        t2 = datetime(2030, 1, 1, tzinfo=IST)
        self.assertEqual(fetcher_factory(t1).reference_time, t1)
        self.assertEqual(fetcher_factory(t2).reference_time, t2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
