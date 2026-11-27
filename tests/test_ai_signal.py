"""Tests for the parts of the fetcher that need no network access.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal import dedup, entities, pipeline, rank, temporal, verify
from ai_signal.model import Bucket, Candidate, Query, Rejection, SourceType
from ai_signal.providers import PageContent, StaticFetcher, StaticProvider, classify_source

IST = temporal.IST
#: The moment the real 7 Sep 2026 sweep ran.
NOW = datetime(2026, 9, 7, 11, 18, 46, tzinfo=IST)


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


class TestWindow(unittest.TestCase):
    def test_window_is_rolling_not_calendar_day(self):
        w = temporal.window_ending_at(NOW, 24)
        self.assertEqual(w.end, NOW)
        self.assertEqual(w.start, datetime(2026, 9, 6, 11, 18, 46, tzinfo=IST))
        # A calendar-day reading would have started at 00:00 on the 7th.
        self.assertNotEqual(w.start, at(7))

    def test_label_names_both_ends_in_ist(self):
        label = temporal.window_ending_at(NOW, 24).label()
        self.assertIn("06 September 2026, 11:18", label)
        self.assertIn("07 September 2026, 11:18", label)
        self.assertTrue(label.endswith("IST"))

    def test_half_open_boundary(self):
        w = temporal.window_ending_at(NOW, 24)
        self.assertFalse(w.contains(w.start))   # exclusive
        self.assertTrue(w.contains(w.end))      # inclusive


class TestTemporalGate(unittest.TestCase):
    def setUp(self):
        self.w = temporal.window_ending_at(NOW, 24)

    def gate(self, finding):
        return temporal.classify(finding, self.w, NOW)

    def test_timestamped_inside_window_is_eligible(self):
        f = temporal.DateFinding(at(7, 9, 0), False, "meta datePublished")
        v = self.gate(f)
        self.assertTrue(v.eligible)
        self.assertIn("07 Sep 2026, 09:00 IST", v.display)

    def test_timestamped_before_window_is_out(self):
        f = temporal.DateFinding(at(6, 8, 0), False, "byline")
        v = self.gate(f)
        self.assertFalse(v.eligible)
        self.assertEqual(v.rejection, "OUT_OF_WINDOW")

    def test_date_only_today_is_eligible_because_observation_bounds_it(self):
        """The key inference: a page cannot be published after we read it."""
        f = temporal.DateFinding(at(7), True, "byline .latest-news-date-main")
        v = self.gate(f)
        self.assertTrue(v.eligible)
        self.assertIn("Time not stated", v.display)

    def test_date_only_yesterday_straddles_and_is_excluded(self):
        f = temporal.DateFinding(at(6), True, "byline")
        v = self.gate(f)
        self.assertFalse(v.eligible)
        self.assertEqual(v.rejection, "DATE_AMBIGUOUS")

    def test_date_only_well_before_window_is_out_not_ambiguous(self):
        f = temporal.DateFinding(at(4), True, "byline")
        v = self.gate(f)
        self.assertFalse(v.eligible)
        self.assertEqual(v.rejection, "OUT_OF_WINDOW")

    def test_missing_date_is_unverifiable(self):
        v = self.gate(None)
        self.assertFalse(v.eligible)
        self.assertEqual(v.rejection, "DATE_UNVERIFIABLE")

    def test_modified_only_timestamp_is_refused(self):
        """A re-stamped 2023 article must not read as today's news."""
        modified = temporal.DateFinding(at(7, 10, 0), False, "meta dateModified")
        resolved = temporal.resolve_publication(None, modified)
        self.assertTrue(resolved.modified_only)
        v = self.gate(resolved)
        self.assertFalse(v.eligible)
        self.assertEqual(v.rejection, "DATE_UNVERIFIABLE")

    def test_published_wins_over_modified(self):
        pub = temporal.DateFinding(at(7, 9, 0), False, "datePublished")
        mod = temporal.DateFinding(at(7, 10, 0), False, "dateModified")
        resolved = temporal.resolve_publication(pub, mod)
        self.assertFalse(resolved.modified_only)
        self.assertEqual(resolved.moment, at(7, 9, 0))


class TestDateParsing(unittest.TestCase):
    def test_fibre2fashion_byline_format(self):
        f = temporal.parse_page_date("07 Sep '26", "byline")
        self.assertIsNotNone(f)
        self.assertTrue(f.date_only)
        self.assertEqual(f.moment.date().isoformat(), "2026-09-07")

    def test_fibre2fashion_json_ld_datepublished_format(self):
        """Caught live: fetching the real 7 Sep 2026 article, its own
        JSON-LD carries {"datePublished": "9/7/2026 8:01:00 AM"} -- M/D/Y,
        not D/M/Y -- which the parser did not recognise until this test
        was added. Confirmed against the site's own separately-displayed
        "07 Sep '26" byline, so M/D/Y is the correct reading, not an
        ambiguous guess."""
        f = temporal.parse_page_date("9/7/2026 8:01:00 AM", "JSON-LD datePublished")
        self.assertIsNotNone(f)
        self.assertEqual(f.moment.date().isoformat(), "2026-09-07")
        # The clock time carries no timezone marker, so -- consistent with
        # every other non-ISO format here -- it is discarded rather than
        # assumed, and the result is date-only.
        self.assertTrue(f.date_only)

    def test_iso_with_offset_keeps_the_instant(self):
        f = temporal.parse_page_date("2026-09-07T09:30:00+05:30", "meta")
        self.assertFalse(f.date_only)
        self.assertEqual(f.moment.hour, 9)

    def test_clock_time_without_offset_is_downgraded_to_date_only(self):
        """An unzoned clock time is not a knowable instant."""
        f = temporal.parse_page_date("2026-09-07T09:30:00", "meta")
        self.assertTrue(f.date_only)
        self.assertIn("offset missing", f.provenance)

    def test_unparseable_returns_none(self):
        self.assertIsNone(temporal.parse_page_date("last Tuesday", "byline"))
        self.assertIsNone(temporal.parse_page_date("", "byline"))


class TestFusion(unittest.TestCase):
    def test_rrf_prefers_agreement_over_a_single_first_place(self):
        both = rank.rrf_score({"a": 3, "b": 3})
        one_first = rank.rrf_score({"a": 1})
        self.assertGreater(both, one_first)

    def test_rrf_ignores_absent_providers(self):
        self.assertEqual(rank.rrf_score({"a": 1}), rank.rrf_score({"a": 1, "b": 0}))

    def test_merge_accumulates_ranks_across_providers(self):
        q = Query("q", Bucket.TOPICS)
        c1 = Candidate(url="u1", title="t", bucket=Bucket.TOPICS, found_by=[q])
        c2 = Candidate(url="u1", title="t", bucket=Bucket.TOPICS, found_by=[q])
        c3 = Candidate(url="u2", title="o", bucket=Bucket.TOPICS, found_by=[q])
        merged = rank.merge_by_url({"alpha": [c1], "beta": [c3, c2]})
        by_url = {c.url: c for c in merged}
        self.assertEqual(by_url["u1"].ranks, {"alpha": 1, "beta": 2})
        ordered = rank.fuse(merged)
        # u1 appears in two providers, so it outranks u2's single first place.
        self.assertEqual(ordered[0].url, "u1")


class TestEntityResolution(unittest.TestCase):
    def setUp(self):
        self.profiles = entities.default_profiles(
            ["WFX", "K.P.R. Mill", "Poppys Group", "Bannari Amman"]
        )

    def test_punctuation_variants_of_a_name_all_match(self):
        p = entities.EntityProfile(name="K.P.R. Mill")
        for form in ("K.P.R. Mill", "KPR Mill", "K P R Mill", "kpr mill"):
            with self.subTest(form=form):
                self.assertIsNotNone(
                    entities.surface_match(f"Shares of {form} rose today.", p)
                )

    def test_name_inside_a_longer_word_does_not_match(self):
        p = entities.EntityProfile(name="WFX")
        self.assertIsNone(entities.surface_match("SWFXY ticker", p))

    def test_ambiguous_acronym_needs_industry_context(self):
        bare = entities.resolve("WFX reported record traffic.", self.profiles)
        self.assertFalse(bare.ok)
        self.assertTrue(any("ambiguous name" in r for _, r in bare.rejected))

        in_context = entities.resolve(
            "WFX, the apparel ERP vendor, signed a garment exporter in Tirupur.",
            self.profiles,
        )
        self.assertIn("WFX", in_context.matched)

    def test_identifier_admits_a_match_without_broad_context(self):
        d = entities.resolve("World Fashion Exchange announced WFX Cloud.", self.profiles)
        self.assertIn("WFX", d.matched)

    def test_exclusion_term_vetoes_a_lookalike(self):
        d = entities.resolve(
            "Poppys Hotel in Madurai reopened its restaurant.", self.profiles
        )
        self.assertNotIn("Poppys Group", d.matched)

    def test_unambiguous_name_matches_on_surface_alone(self):
        d = entities.resolve("K.P.R. Mill filed with the exchange.", self.profiles)
        self.assertIn("K.P.R. Mill", d.matched)


class TestDevelopmentCues(unittest.TestCase):
    """Caught live via the Force-refresh demo path (2026-09-11): a
    realistic article about a company "piloting" new technology was
    wrongly rejected as having no development, because "pilot" was
    entirely absent from the cue vocabulary -- not just its "-ing" form."""

    def test_pilot_and_its_inflections_count_as_development_language(self):
        for word in ("pilot", "piloting", "pilots", "piloted"):
            with self.subTest(word=word):
                self.assertIn(word, verify.DEVELOPMENT_CUES)

    def test_trial_including_british_spellings_counts(self):
        for word in ("trial", "trials", "trialled", "trialling"):
            with self.subTest(word=word):
                self.assertIn(word, verify.DEVELOPMENT_CUES)

    def test_a_real_piloting_sentence_is_recognised_as_a_development(self):
        text = (
            "Manufacturers are piloting digital twin technology on knitting "
            "production lines, creating virtual replicas of physical "
            "machines to predict maintenance needs and reduce downtime."
        )
        self.assertTrue(verify.cue_based_development(
            Candidate(url="https://example.com/x", title="Digital twin pilot",
                     bucket=Bucket.TOPICS, body=text)
        ))


class TestStructuralRejection(unittest.TestCase):
    def test_rejects_directories_quotes_jobs_and_market_reports(self):
        cases = {
            "https://www.zoominfo.com/c/eastman-apparels/21744138": "directory",
            "https://finance.yahoo.com/quote/KITEX.NS/": "stock-price",
            "https://groww.in/stocks/kpr-mill-ltd": "stock-price",
            "https://in.linkedin.com/company/shahi-exports-pvt-ltd": "social",
            "https://companies.naukri.com/shahi-jobs/about-us": "job",
            "https://www.marketsandmarkets.com/report/textile-ai": "market-report",
        }
        for url in cases:
            with self.subTest(url=url):
                self.assertIsNotNone(verify.structural_rejection(url))

    def test_admits_a_real_article_url(self):
        url = (
            "https://www.fibre2fashion.com/news/apparel-news/"
            "bangladesh-s-rmg-sector-bets-on-technology-313242-newsdetails.htm"
        )
        self.assertIsNone(verify.structural_rejection(url))

    def test_rss_provider_feed_hosts_are_classified_as_trade_publications(self):
        """Regression for a real gap: these three showed up as genuine,
        verified `sourceType: "unknown"` hits the first time the RSS
        provider was tested live against real feeds -- they were feed
        sources ai_signal.live.rss_provider reads from, just never added
        to this classification list."""
        for host, expected_name in (
            ("textilesresources.com", "Textiles Resources"),
            ("knittingviews.com", "Knitting Views"),
            ("globaltextiletimes.com", "Global Textile Times"),
        ):
            with self.subTest(host=host):
                source_type, name = classify_source(host)
                self.assertEqual(name, expected_name)

    def test_a_news_slug_containing_career_is_not_mistaken_for_a_jobs_page(self):
        """The pre-filter must not cost recall on genuine articles."""
        for url in (
            "https://www.fibre2fashion.com/news/textile-news/"
            "tirupur-career-growth-drives-wage-revision-1234-newsdetails.htm",
            "https://apparelresources.com/business-news/manufacturing/"
            "recruitment-drive-lifts-garment-output/",
            # A genuine Fibre2Fashion headline from the 6 Sep 2026 window.
            "https://www.fibre2fashion.com/news/textile-news/"
            "us-manufacturing-jobs-rise-16-000-in-august-313261-newsdetails.htm",
        ):
            with self.subTest(url=url):
                self.assertIsNone(verify.structural_rejection(url))

    def test_still_rejects_an_actual_careers_page(self):
        for url in (
            "https://www.example-mill.com/careers/",
            "https://www.example-mill.com/about/career",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(verify.structural_rejection(url))


class TestDedup(unittest.TestCase):
    def make(self, url, title, body, stype, when):
        return Candidate(
            url=url,
            title=title,
            bucket=Bucket.CLIENTS,
            body=body,
            entities=["K.P.R. Mill"],
            source_type=stype,
            published_at=when,
        )

    def test_syndicated_copies_collapse_to_the_strongest_source(self):
        body = (
            "K.P.R. Mill said on Monday it had commissioned a new garment unit "
            "in Tiruppur with an annual capacity of twelve million pieces, "
            "adding that commercial production would begin next quarter."
        )
        filing = self.make(
            "https://trendlyne.com/x", "KPR Mill commissions garment unit",
            body, SourceType.REGULATORY_FILING, at(7, 9, 0),
        )
        trade = self.make(
            "https://fibre2fashion.com/y", "KPR Mill commissions garment unit",
            body, SourceType.TRADE_PUBLICATION, at(7, 9, 30),
        )
        agg = self.make(
            "https://aggregator.example/z",
            "KPR Mill commissions garment unit in Tiruppur",
            body, SourceType.AGGREGATOR, at(7, 10, 0),
        )
        clusters = dedup.deduplicate([trade, agg, filing])
        self.assertEqual(len(clusters), 1)
        canonical, dupes = clusters[0]
        self.assertEqual(canonical.source_type, SourceType.REGULATORY_FILING)
        self.assertEqual(len(dupes), 2)

    def test_distinct_developments_stay_separate(self):
        a = self.make(
            "https://fibre2fashion.com/a", "KPR Mill commissions garment unit",
            "KPR Mill commissioned a new garment unit in Tiruppur this week.",
            SourceType.TRADE_PUBLICATION, at(7, 9, 0),
        )
        b = self.make(
            "https://fibre2fashion.com/b", "KPR Mill declares interim dividend",
            "The board of KPR Mill declared an interim dividend of five rupees.",
            SourceType.TRADE_PUBLICATION, at(7, 10, 0),
        )
        self.assertEqual(len(dedup.deduplicate([a, b])), 2)

    def test_same_text_far_apart_in_time_is_a_new_development(self):
        body = "Shahi Exports opened a training centre for machine operators."
        old = self.make("https://x.example/1", "Shahi opens centre", body,
                        SourceType.TRADE_PUBLICATION, at(1, 9, 0))
        new = self.make("https://x.example/2", "Shahi opens centre", body,
                        SourceType.TRADE_PUBLICATION, at(7, 9, 0))
        self.assertEqual(len(dedup.deduplicate([old, new])), 2)


class TestVerificationGate(unittest.TestCase):
    def setUp(self):
        self.w = temporal.window_ending_at(NOW, 24)
        self.profiles = entities.default_profiles(["K.P.R. Mill"])

    def candidate(self, **kw):
        base = dict(
            url="https://www.fibre2fashion.com/news/textile-news/a-313260-newsdetails.htm",
            title="K.P.R. Mill commissions new garment unit",
            bucket=Bucket.CLIENTS,
            body=(
                "K.P.R. Mill announced on Monday that it had commissioned a new "
                "garment unit in Tiruppur, and said the knitwear exporter would "
                "expand spinning capacity at the same textile site."
            ),
            source_type=SourceType.TRADE_PUBLICATION,
            published_at=at(7),
            date_only=True,
            date_provenance="byline .latest-news-date-main",
        )
        base.update(kw)
        return Candidate(**base)

    def test_complete_evidence_is_admitted(self):
        v = verify.gate(self.candidate(), self.w, NOW, self.profiles)
        self.assertTrue(v.included, msg=v.reason)
        self.assertIn("K.P.R. Mill", v.candidate.entities)

    def test_ai_company_scope_checks_the_article_lead(self):
        profiles = entities.default_profiles(["NVIDIA", "Microsoft"])
        candidate = self.candidate(
            title="NVIDIA previews new cloud servers",
            body="NVIDIA announced new servers. Related: Microsoft announces AI tools.",
        )
        kwargs = {"company_context_query": verify.AI_COMPANY_CONTEXT}
        rejected = verify.gate(
            candidate, self.w, NOW, profiles,
            publisher_summary="NVIDIA says the servers can stream more games.", **kwargs,
        )
        admitted = verify.gate(
            candidate, self.w, NOW, profiles,
            publisher_summary="NVIDIA says the servers use AI accelerators.", **kwargs,
        )
        self.assertEqual(rejected.rejection, Rejection.OFF_TOPIC)
        self.assertTrue(admitted.included, msg=admitted.reason)
        self.assertEqual(admitted.candidate.entities, ["NVIDIA"])

    def test_unopened_page_is_rejected_even_with_a_good_snippet(self):
        c = self.candidate(body="", snippet="KPR Mill announces expansion today")
        v = verify.gate(c, self.w, NOW, self.profiles)
        self.assertFalse(v.included)
        self.assertEqual(v.rejection, Rejection.NOT_RETRIEVABLE)

    def test_profile_page_without_event_language_is_rejected(self):
        c = self.candidate(
            title="K.P.R. Mill Limited — company overview",
            body=(
                "K.P.R. Mill is a vertically integrated textile company in "
                "Tiruppur spanning yarn, fabric and garments."
            ),
        )
        v = verify.gate(c, self.w, NOW, self.profiles)
        self.assertFalse(v.included)
        self.assertEqual(v.rejection, Rejection.NO_DEVELOPMENT)

    def test_out_of_window_is_rejected_with_the_date_in_the_reason(self):
        c = self.candidate(published_at=at(4), date_only=True)
        v = verify.gate(c, self.w, NOW, self.profiles)
        self.assertFalse(v.included)
        self.assertEqual(v.rejection, Rejection.OUT_OF_WINDOW)
        self.assertIn("04 Sep 2026", v.reason)

    def test_wrong_company_is_rejected(self):
        c = self.candidate(
            title="Poppys Hotel announces refurbishment",
            body=(
                "Poppys Hotel announced a refurbishment of its Madurai "
                "property, and said the hotel would reopen in October."
            ),
        )
        v = verify.gate(c, self.w, NOW, entities.default_profiles(["Poppys Group"]))
        self.assertFalse(v.included)
        self.assertEqual(v.rejection, Rejection.ENTITY_MISMATCH)

    def test_press_release_is_admitted_but_labelled(self):
        c = self.candidate(source_type=SourceType.COMPANY_PRESS_RELEASE)
        v = verify.gate(c, self.w, NOW, self.profiles)
        self.assertTrue(v.included, msg=v.reason)
        self.assertTrue(v.promotional)
        self.assertIn("not independently verified", verify.label_for(v))

    def test_topic_bucket_needs_no_entity(self):
        c = self.candidate(bucket=Bucket.TOPICS, title="AI adoption in garment plants")
        v = verify.gate(c, self.w, NOW, profiles=())
        self.assertTrue(v.included, msg=v.reason)


class TestEndToEnd(unittest.TestCase):
    """Replays the real 7 Sep 2026 sweep shape through the whole pipeline."""

    URL_HIT = (
        "https://www.fibre2fashion.com/news/apparel-news/"
        "bangladesh-s-rmg-sector-bets-on-technology-for-a-productivity-edge-"
        "313242-newsdetails.htm"
    )
    URL_STALE = (
        "https://www.fibre2fashion.com/news/upcoming-technology-product-news/"
        "coats-digital-debuts-gsd-realmotion-312778-newsdetails.htm"
    )
    URL_DIR = "https://www.zoominfo.com/c/eastman-apparels/21744138"

    def build(self):
        terms = [
            pipeline.WatchTerm("K.P.R. Mill", Bucket.CLIENTS, ["KPR Mill"]),
            pipeline.WatchTerm("Coats Digital", Bucket.COMPETITORS, ["GSDCost"]),
            pipeline.WatchTerm(
                '("garment manufacturing") AND ("AI")', Bucket.TOPICS
            ),
        ]

        proto_hit = Candidate(
            url=self.URL_HIT,
            title="Bangladesh's RMG sector bets on technology for a productivity edge",
            bucket=Bucket.TOPICS,
        )
        proto_stale = Candidate(
            url=self.URL_STALE,
            title="Coats Digital debuts GSD RealMotion to transform time studies",
            bucket=Bucket.COMPETITORS,
        )
        proto_dir = Candidate(url=self.URL_DIR, title="Eastman Apparels — Overview",
                              bucket=Bucket.CLIENTS)

        topic_q = '("garment manufacturing") AND ("AI")'
        provider = StaticProvider(
            name="fixture",
            results={
                topic_q: [proto_hit],
                '"Coats Digital"': [proto_stale],
                '"K.P.R. Mill"': [proto_dir],
            },
        )

        fetcher = StaticFetcher(
            pages={
                self.URL_HIT: PageContent(
                    body=(
                        "Bangladesh apparel exporters are adopting digital "
                        "monitoring, data-driven production and AI tools to raise "
                        "productivity in garment manufacturing. Factories that "
                        "deployed the systems reported gains of up to 25 per cent, "
                        "and several have adopted automated defect detection on "
                        "the textile shop floor."
                    ),
                    published=temporal.DateFinding(at(7), True, "byline"),
                    source_type=SourceType.TRADE_PUBLICATION,
                    source_name="Fibre2Fashion",
                ),
                self.URL_STALE: PageContent(
                    body=(
                        "Coats Digital announced GSD RealMotion, which uses AI and "
                        "computer vision to turn phone video into method studies "
                        "for apparel and garment engineers."
                    ),
                    published=temporal.DateFinding(
                        at(19 - 12, 0), True, "byline"  # 07 Aug stand-in; see below
                    ),
                    source_type=SourceType.TRADE_PUBLICATION,
                    source_name="Fibre2Fashion",
                ),
            }
        )
        # The real article is dated 19 Aug 2026; express that exactly.
        fetcher.pages[self.URL_STALE].published = temporal.DateFinding(
            datetime(2026, 8, 19, tzinfo=IST), True, "byline"
        )
        return terms, provider, fetcher

    def test_sweep_admits_only_the_verified_in_window_item(self):
        terms, provider, fetcher = self.build()
        res = pipeline.run(terms, [provider], fetcher, NOW)

        self.assertEqual(res.counts()["topics"], 1)
        self.assertEqual(res.counts()["clients"], 0)
        self.assertEqual(res.counts()["competitors"], 0)

        kept = res.included[Bucket.TOPICS][0]
        self.assertEqual(kept.candidate.url, self.URL_HIT)
        self.assertEqual(kept.candidate.source_name, "Fibre2Fashion")

    def test_sweep_records_why_each_drop_happened(self):
        terms, provider, fetcher = self.build()
        res = pipeline.run(terms, [provider], fetcher, NOW)
        reasons = res.rejections_by_reason()
        self.assertIn(Rejection.OUT_OF_WINDOW.value, reasons)
        self.assertIn(Rejection.NO_DEVELOPMENT.value, reasons)

    def test_window_and_check_time_are_reported(self):
        terms, provider, fetcher = self.build()
        res = pipeline.run(terms, [provider], fetcher, NOW)
        self.assertIn("06 September 2026, 11:18", res.window.label())
        self.assertEqual(res.checked_at, NOW)

    def test_a_failing_provider_becomes_a_stated_limitation(self):
        terms, provider, fetcher = self.build()

        class Broken:
            name = "broken"

            def search(self, query, limit=20):
                raise RuntimeError("HTTP 402 Payment Required")

        res = pipeline.run(terms, [provider, Broken()], fetcher, NOW)
        self.assertTrue(any("402" in m for m in res.limitations))
        # The working provider's result still comes through.
        self.assertEqual(res.counts()["topics"], 1)

    def test_query_plan_covers_each_entity_and_a_group_query(self):
        terms, _, _ = self.build()
        queries = [q.text for q in pipeline.plan(terms)]
        self.assertIn('"K.P.R. Mill"', queries)
        self.assertIn('"KPR Mill"', queries)          # alias searched separately
        self.assertIn('"Coats Digital"', queries)
        self.assertTrue(any(" OR " in q for q in queries))   # grouped query

class TestStockTipHosts(unittest.TestCase):
    """Hosts added after broader news discovery put two stock listicles into
    the clients bucket -- including "Readymade Garments Penny Stocks India
    2027: 5 Picks", which was that run's only client-bucket "news"."""

    def test_stock_tip_sites_are_refused_on_url_alone(self):
        for url in ("https://univest.in/blog/readymade-garments-penny-stocks",
                    "https://www.tickertape.in/stocks/kitex-garments",
                    "https://simplywall.st/stocks/in/kpr-mill"):
            with self.subTest(url=url):
                self.assertIsNotNone(verify.structural_rejection(url))

    def test_real_trade_press_is_untouched(self):
        for url in ("https://www.theinterline.com/orient-rongheng-coats-digital",
                    "https://apparelresources.com/business-news/manufacturing/x",
                    "https://www.livemint.com/companies/kpr-mill-expansion"):
            with self.subTest(url=url):
                self.assertIsNone(verify.structural_rejection(url))


class TestLookbackFor(unittest.TestCase):
    """A window can be widened in the UI now (see watchlist_store.window_hours)
    -- lookback_for is what stops that from silently capping recall at the
    old fixed 72h regardless of how wide a window was actually asked for."""

    def test_matches_the_existing_default_relationship(self):
        self.assertEqual(pipeline.lookback_for(24), 72)

    def test_scales_with_a_wider_window_rather_than_staying_fixed(self):
        """The whole bug this exists to prevent: a week-wide report window
        must not still only ever search the last 72 hours."""
        self.assertEqual(pipeline.lookback_for(24 * 7), 24 * 7 + 48)
        self.assertGreater(pipeline.lookback_for(24 * 7), 72)


if __name__ == "__main__":
    unittest.main(verbosity=2)
