"""Tests for RssSearchProvider against mocked HTTP responses -- no network,
no dependency on any real feed being up right now. The feed XML samples
below were captured from real requests to the actual feeds during
development (see rss_provider.py's module docstring for which ones and
why), not invented.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live.rss_provider import RssSearchProvider, _strip_html
from ai_signal.model import Bucket, Query

FEED_A = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Textile Focus</title>
<item>
  <title>K.P.R. Mill commissions new garment unit in Tiruppur</title>
  <link>https://textilefocus.com/kpr-mill-unit/</link>
  <pubDate>Fri, 11 Sep 2026 09:00:00 +0000</pubDate>
  <description><![CDATA[<p>The textile exporter said commercial production begins next quarter.</p>]]></description>
</item>
<item>
  <title>Scandex cricket tournament held</title>
  <link>https://textilefocus.com/scandex-cricket/</link>
  <pubDate>Thu, 10 Sep 2026 09:27:40 +0000</pubDate>
  <description><![CDATA[<p>An internal sports event, nothing to do with textiles news.</p>]]></description>
</item>
</channel></rss>"""

FEED_B = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Just Style</title>
<item>
  <title>K.P.R. Mill commissions new garment unit in Tiruppur</title>
  <link>https://textilefocus.com/kpr-mill-unit/?utm_source=x</link>
  <pubDate>Fri, 11 Sep 2026 09:05:00 +0000</pubDate>
  <content:encoded><![CDATA[<p>Syndicated copy of the same story from a different feed.</p>]]></content:encoded>
</item>
<item>
  <title>BlueKaktus raises funding round for apparel supply chain AI</title>
  <link>https://www.just-style.com/bluekaktus-funding/</link>
  <pubDate>Fri, 11 Sep 2026 06:00:00 +0000</pubDate>
  <description><![CDATA[<p>The AI-powered supply chain platform closed a new round.</p>]]></description>
</item>
</channel></rss>"""


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen_for(feeds_by_url: dict):
    def _fake(request, timeout=None):
        url = request.full_url
        if url not in feeds_by_url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _FakeResponse(feeds_by_url[url])
    return _fake


class TestStripHtml(unittest.TestCase):
    def test_strips_tags_and_common_entities(self):
        text = _strip_html("<p>Hello &amp; welcome&#8230;</p>")
        self.assertEqual(text, "Hello & welcome...")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(_strip_html(""), "")
        self.assertEqual(_strip_html(None), "")


class TestTolerantXml(unittest.TestCase):
    """Regressions for two real, distinct malformations caught live during
    the first full-watchlist RSS sweep (2026-09-12) -- both diagnosed by
    fetching the actual feed bytes and inspecting them directly, not
    guessed. See `_tolerant()`'s docstring in rss_provider.py."""

    def test_leading_tab_before_xml_declaration_is_tolerated(self):
        """Real bug: Apparel Resources' feed response began with a literal
        tab character before <?xml ...?>, which the XML spec requires to
        be the very first thing in the document -- a WordPress quirk, not
        a fetch error. Without the fix, ET.fromstring raises "XML or text
        declaration not at start of entity"."""
        feed = (
            "\t<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<rss version=\"2.0\"><channel><item>\n"
            "  <title>Example story</title>\n"
            "  <link>https://apparelresources.com/example/</link>\n"
            "  <description>An example description.</description>\n"
            "</item></channel></rss>"
        )
        provider = RssSearchProvider(
            feeds={"Apparel Resources": "https://apparelresources.com/feed/"}
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_for({"https://apparelresources.com/feed/": feed}),
        ):
            hits = provider.search(Query("example story", Bucket.TOPICS))
        self.assertTrue(hits)
        self.assertIn("ok", provider.health()["Apparel Resources"])

    def test_bare_ampersand_in_embedded_script_is_tolerated(self):
        """Real bug: Sourcing Journal's feed embeds a raw JavaScript geo-
        targeting snippet containing an unescaped "&&", which is genuinely
        invalid XML -- a lone "&" must start a real entity reference.
        Without the fix, ET.fromstring raises "not well-formed (invalid
        token)". Escaping only bare ampersands must leave a real entity
        like "&amp;" elsewhere in the same document untouched."""
        feed = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<rss version=\"2.0\"><channel><item>\n"
            "  <title>Example story &amp; more</title>\n"
            "  <link>https://sourcingjournal.com/example/</link>\n"
            "  <description>if ( 'object' === typeof pmc_fastly_geo_data "
            "&& 'string' === typeof pmc_fastly_geo_data.region ) { }</description>\n"
            "</item></channel></rss>"
        )
        provider = RssSearchProvider(
            feeds={"Sourcing Journal": "https://sourcingjournal.com/feed/"}
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_for({"https://sourcingjournal.com/feed/": feed}),
        ):
            hits = provider.search(Query("example story", Bucket.TOPICS))
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Example story & more")

    def test_a_response_that_is_not_actually_rss_fails_fast_with_a_clear_reason(self):
        """Real bug: Sourcing Journal's /feed/ URL (and every common
        alternate feed path tried) returned the site's full HTML homepage,
        not RSS -- Content-Type: text/html, no <rss>/<channel>/<item>
        anywhere. No amount of XML tolerance can fix a response that was
        never XML meant as a feed in the first place, so this must fail
        fast with a clear reason instead of a cryptic expat parse error
        surfaced from thousands of lines of embedded page HTML/JS."""
        html_homepage = (
            "<!DOCTYPE html>\n<html lang=\"en-US\"><head>"
            "<meta charset=\"UTF-8\"/></head><body>Not a feed.</body></html>"
        )
        provider = RssSearchProvider(
            feeds={"Sourcing Journal": "https://sourcingjournal.com/feed/"}
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=_fake_urlopen_for({"https://sourcingjournal.com/feed/": html_homepage}),
        ):
            hits = provider.search(Query("example story", Bucket.TOPICS))
        self.assertEqual(hits, [])
        self.assertIn("not RSS/Atom", provider.health()["Sourcing Journal"])


class TestRecencyOrdering(unittest.TestCase):
    """Regression for a real bug: a broad topic query spent 16 of its 20
    result slots on month-old feature articles from one large feed and
    truncated before reaching that day's news, so a 24-hour report found
    nothing while the feeds carried same-day stories. Pooled items must be
    ordered newest-first so that "first N matches" means "N freshest"."""

    STALE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Big Stale Feed</title>
<item><title>Textile automation retrospective</title>
  <link>https://stale.example/a/</link>
  <pubDate>Mon, 03 Aug 2026 15:33:08 +0000</pubDate>
  <description>A textile automation feature.</description></item>
<item><title>Textile automation yearbook</title>
  <link>https://stale.example/b/</link>
  <pubDate>Tue, 04 Aug 2026 10:11:20 +0000</pubDate>
  <description>Another textile automation feature.</description></item>
</channel></rss>"""

    FRESH_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Small Fresh Feed</title>
<item><title>Textile automation deal signed today</title>
  <link>https://fresh.example/today/</link>
  <pubDate>Sun, 13 Sep 2026 12:54:00 +0000</pubDate>
  <description>A textile automation development.</description></item>
</channel></rss>"""

    def build(self):
        # The stale feed is listed FIRST, exactly as the real one was.
        feeds = {"Big Stale Feed": "https://stale.example/feed/",
                 "Small Fresh Feed": "https://fresh.example/feed/"}
        urls = {"https://stale.example/feed/": self.STALE_FEED,
                "https://fresh.example/feed/": self.FRESH_FEED}
        return RssSearchProvider(feeds=feeds), urls

    def test_freshest_item_wins_a_slot_even_when_its_feed_is_listed_last(self):
        provider, urls = self.build()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urls)):
            hits = provider.search(Query('"textile automation"', Bucket.TOPICS), limit=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(
            hits[0].url, "https://fresh.example/today/",
            msg="the single result slot must go to the newest matching item, "
                "not to whichever feed happened to be listed first",
        )

    def test_pooled_items_are_ordered_newest_first(self):
        provider, urls = self.build()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urls)):
            provider.search(Query('"textile automation"', Bucket.TOPICS))
        dates = [i.published_hint for i in provider._items]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_dated_entries_before_retrieval_window_are_skipped(self):
        from datetime import datetime, timezone

        provider, urls = self.build()
        query = Query(
            '"textile automation"', Bucket.TOPICS,
            retrieval_start=datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urls)):
            hits = provider.search(query)
        self.assertEqual([candidate.url for candidate in hits], ["https://fresh.example/today/"])

    def test_undated_items_sort_last_but_are_still_returned(self):
        undated = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
  <title>Textile automation undated piece</title>
  <link>https://undated.example/x/</link>
  <description>No pubDate at all.</description>
</item></channel></rss>"""
        feeds = {"Undated": "https://undated.example/feed/",
                 "Small Fresh Feed": "https://fresh.example/feed/"}
        urls = {"https://undated.example/feed/": undated,
                "https://fresh.example/feed/": self.FRESH_FEED}
        provider = RssSearchProvider(feeds=feeds)
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urls)):
            hits = provider.search(Query('"textile automation"', Bucket.TOPICS))
        self.assertEqual([c.url for c in hits],
                         ["https://fresh.example/today/", "https://undated.example/x/"])

    def test_a_feed_date_never_reaches_the_candidate(self):
        """The ordering hint must stay a retrieval concern. Only opening
        the real page may establish a publication date, so a Candidate
        leaving this provider still carries none."""
        provider, urls = self.build()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urls)):
            hits = provider.search(Query('"textile automation"', Bucket.TOPICS))
        self.assertTrue(hits)
        for c in hits:
            self.assertIsNone(c.published_at)

    def test_ordering_is_total_so_same_input_gives_same_queue(self):
        """Two items sharing a timestamp must not swap order run to run."""
        tie = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Textile automation one</title><link>https://t.example/b/</link>
  <pubDate>Sun, 13 Sep 2026 09:00:00 +0000</pubDate>
  <description>x</description></item>
<item><title>Textile automation two</title><link>https://t.example/a/</link>
  <pubDate>Sun, 13 Sep 2026 09:00:00 +0000</pubDate>
  <description>x</description></item>
</channel></rss>"""
        runs = []
        for _ in range(3):
            provider = RssSearchProvider(feeds={"T": "https://t.example/feed/"})
            with patch("urllib.request.urlopen",
                       side_effect=_fake_urlopen_for({"https://t.example/feed/": tie})):
                hits = provider.search(Query('"textile automation"', Bucket.TOPICS))
            runs.append([c.url for c in hits])
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])
        self.assertEqual(runs[0], ["https://t.example/a/", "https://t.example/b/"])


class TestRssSearchProvider(unittest.TestCase):
    def make_provider(self):
        feeds = {"Textile Focus": "https://textilefocus.com/feed/",
                 "Just Style": "https://www.just-style.com/feed/"}
        provider = RssSearchProvider(feeds=feeds, cache_ttl_s=600.0)
        urlopen_map = {
            "https://textilefocus.com/feed/": FEED_A,
            "https://www.just-style.com/feed/": FEED_B,
        }
        return provider, urlopen_map

    def test_parses_real_captured_feed_shapes(self):
        provider, urlopen_map = self.make_provider()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urlopen_map)):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertTrue(any("kpr-mill-unit" in c.url for c in hits))

    def test_prefers_content_encoded_over_description_when_both_present(self):
        """Isolated from the cross-feed dedup test above on purpose: that
        test's two feeds legitimately describe the *same* story, so they
        correctly collapse to one item, which would make this assertion
        depend on which copy dedup happened to keep rather than on the
        content:encoded-vs-description preference actually being tested."""
        feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><item>
  <title>Example story</title>
  <link>https://example.com/story/</link>
  <description><![CDATA[<p>Short description text.</p>]]></description>
  <content:encoded><![CDATA[<p>Full content:encoded body text.</p>]]></content:encoded>
</item></channel></rss>"""
        provider = RssSearchProvider(feeds={"Example": "https://example.com/feed/"})
        with patch("urllib.request.urlopen",
                   side_effect=_fake_urlopen_for({"https://example.com/feed/": feed})):
            hits = provider.search(Query("example story", Bucket.TOPICS))
        self.assertEqual(len(hits), 1)
        self.assertIn("Full content:encoded body", hits[0].snippet)
        self.assertNotIn("Short description", hits[0].snippet)

    def test_duplicate_urls_across_feeds_are_collapsed_by_normalised_url(self):
        """The same story, syndicated with a tracking query string on one
        feed, must not appear twice just because the URLs differ by a
        query parameter."""
        provider, urlopen_map = self.make_provider()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urlopen_map)):
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        urls = [i.url.split("?")[0].rstrip("/").lower() for i in provider._items]
        self.assertEqual(len(urls), len(set(urls)))

    def test_unrelated_query_does_not_match_a_single_common_word(self):
        """Same discipline as the demo provider: a multi-word query needs
        real overlap, not one generic shared word, to match."""
        provider, urlopen_map = self.make_provider()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urlopen_map)):
            hits = provider.search(Query('"quantum computing hardware"', Bucket.TOPICS))
        self.assertEqual(hits, [])

    def test_one_dead_feed_does_not_sink_the_others(self):
        feeds = {"Dead Feed": "https://dead.example/feed/",
                 "Textile Focus": "https://textilefocus.com/feed/"}
        provider = RssSearchProvider(feeds=feeds, cache_ttl_s=600.0)
        urlopen_map = {"https://textilefocus.com/feed/": FEED_A}
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urlopen_map)):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertTrue(hits)
        health = provider.health()
        self.assertIn("Dead Feed", health)
        self.assertIn("failed", health["Dead Feed"])
        self.assertIn("Textile Focus", health)
        self.assertIn("ok", health["Textile Focus"])

    def test_a_feed_that_suddenly_returns_zero_items_is_marked_degraded(self):
        """Observed live: a feed serving 10 items returned an empty channel
        on the next poll and still reported "ok: 0 items" -- indistinguishable
        from "this source published nothing", which is the one thing an empty
        report must never be ambiguous about."""
        empty = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Textile Focus</title></channel></rss>"""
        provider = RssSearchProvider(feeds={"Textile Focus": "https://textilefocus.com/feed/"},
                                     cache_ttl_s=0.0)
        with patch("urllib.request.urlopen",
                   side_effect=_fake_urlopen_for({"https://textilefocus.com/feed/": FEED_A})):
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertIn("ok", provider.health()["Textile Focus"])

        with patch("urllib.request.urlopen",
                   side_effect=_fake_urlopen_for({"https://textilefocus.com/feed/": empty})):
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertIn("DEGRADED", provider.health()["Textile Focus"])

    def test_a_feed_that_has_only_ever_been_empty_is_not_called_degraded(self):
        """Without a prior non-empty poll there is no regression to report --
        only an absence of evidence, which must not be dressed up as a fault."""
        empty = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>New Feed</title></channel></rss>"""
        provider = RssSearchProvider(feeds={"New Feed": "https://new.example/feed/"})
        with patch("urllib.request.urlopen",
                   side_effect=_fake_urlopen_for({"https://new.example/feed/": empty})):
            provider.search(Query('"anything"', Bucket.TOPICS))
        self.assertNotIn("DEGRADED", provider.health()["New Feed"])

    def test_health_is_empty_before_the_first_search(self):
        provider, _ = self.make_provider()
        self.assertEqual(provider.health(), {})

    def test_candidates_carry_no_published_date(self):
        """A feed's own pubDate must never be treated as a verified
        publication time -- only HttpPageFetcher, by actually opening the
        page, establishes that. Confirms the provider does not smuggle a
        date onto the Candidate some other way."""
        provider, urlopen_map = self.make_provider()
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen_for(urlopen_map)):
            hits = provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        for c in hits:
            self.assertIsNone(c.published_at)

    def test_second_search_within_ttl_does_not_refetch(self):
        provider, urlopen_map = self.make_provider()
        call_count = {"n": 0}
        real_fake = _fake_urlopen_for(urlopen_map)

        def counting_fake(request, timeout=None):
            call_count["n"] += 1
            return real_fake(request, timeout)

        with patch("urllib.request.urlopen", side_effect=counting_fake):
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
            provider.search(Query('"BlueKaktus"', Bucket.COMPETITORS))
        # Two feeds, fetched once each across both search() calls.
        self.assertEqual(call_count["n"], 2)

    def test_stale_cache_triggers_a_refetch(self):
        provider, urlopen_map = self.make_provider()
        provider.cache_ttl_s = 0.0  # always stale
        call_count = {"n": 0}
        real_fake = _fake_urlopen_for(urlopen_map)

        def counting_fake(request, timeout=None):
            call_count["n"] += 1
            return real_fake(request, timeout)

        with patch("urllib.request.urlopen", side_effect=counting_fake):
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
            provider.search(Query('"K.P.R. Mill"', Bucket.CLIENTS))
        self.assertEqual(call_count["n"], 4)  # 2 feeds x 2 fetch passes


if __name__ == "__main__":
    unittest.main(verbosity=2)
