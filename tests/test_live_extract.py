"""Tests for ai_signal.live.extract -- pure HTML parsing, no network.

This is the boundary claim from ai_signal/live/__init__.py made concrete:
the network call is untested here (it can't be, honestly, without hitting
real sites), but everything downstream of "here is some HTML" is pure and
gets exactly the same test discipline as the rest of the package.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live.extract import extract_page
from ai_signal.model import SourceType

FIBRE2FASHION_LIKE = """<!doctype html><html><head>
<title>Bangladesh's RMG sector bets on technology for a productivity edge</title>
<meta property="og:title" content="Bangladesh's RMG sector bets on technology">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": "Bangladesh's RMG sector bets on technology for a productivity edge",
  "datePublished": "2026-09-07T00:00:00+05:30",
  "dateModified": "2026-09-07T09:15:00+05:30"
}
</script>
</head><body>
<script>var trackingPixel = "ignore me, not article text";</script>
<style>.hidden { display: none; }</style>
<h1>Bangladesh's RMG sector bets on technology for a productivity edge</h1>
<p>Bangladesh apparel exporters are adopting digital monitoring, data-driven
production and AI tools to raise productivity in garment manufacturing.</p>
<p>Factories that deployed the systems reported gains of up to 25 per cent.</p>
</body></html>"""

META_ONLY = """<!doctype html><html><head>
<title>Coats Digital debuts GSD RealMotion</title>
<meta property="article:published_time" content="2026-08-19T00:00:00Z">
<meta property="article:modified_time" content="2026-08-20T11:00:00Z">
</head><body>
<p>Coats Digital announced GSD RealMotion, which uses AI and computer vision
to turn phone video into structured method studies for garment engineers.</p>
</body></html>"""

NO_DATE_AT_ALL = """<!doctype html><html><head>
<title>K.P.R. Mill Limited -- company overview</title>
</head><body>
<p>K.P.R. Mill is a vertically integrated textile company in Tiruppur
spanning yarn, fabric and garments.</p>
</body></html>"""

MALFORMED_JSON_LD = """<!doctype html><html><head>
<title>Test page</title>
<script type="application/ld+json">{ this is not valid json at all </script>
<meta property="article:published_time" content="2026-09-06T14:00:00+05:30">
</head><body>
<p>Fallback to the meta tag when JSON-LD cannot be parsed, since one bad
block should never take down the whole page.</p>
</body></html>"""

EMPTY_BODY = """<!doctype html><html><head><title>Empty</title></head>
<body><script>var onlyJsHere = true;</script></body></html>"""

# Reproduces a real, currently-live bug: fetching the actual 7 Sep 2026
# Fibre2Fashion article, its own NewsArticle JSON-LD embeds a multi-line
# "description" field with LITERAL, unescaped CRLFs instead of "\n" --
# invalid per RFC 8259, but real and served by a real site. json.loads in
# strict mode rejects the whole block over one field, silently losing a
# datePublished that was genuinely present. The literal \r\n below is not
# a formatting accident -- it is byte-for-byte what the site sent.
REAL_WORLD_MALFORMED_JSON_LD = (
    "<!doctype html><html><head><title>Bangladesh's RMG sector bets on "
    "technology</title>\n"
    "<script type=\"application/ld+json\">\n"
    "{\r\n"
    '    "@context": "https://schema.org",\r\n'
    '    "@type": "NewsArticle",\r\n'
    '    "headline": "Bangladesh\'s RMG sector bets on technology",\r\n'
    '    "description": "Apparel/Garments\r\n\r\n'
    "Bangladesh's RMG sector bets on technology for a productivity "
    'edge\r\n\r\n    more text here",\r\n'
    '    "datePublished": "9/7/2026 8:01:00 AM",\r\n'
    '    "dateModified": "9/7/2026 8:01:00 AM"\r\n'
    "}\n"
    "</script>\n"
    "</head><body>\n"
    "<p>Bangladesh apparel exporters are adopting AI tools in garment "
    "manufacturing to raise productivity.</p>\n"
    "</body></html>"
)


class TestExtractPage(unittest.TestCase):
    def test_prefers_json_ld_over_meta_tags(self):
        page = extract_page(FIBRE2FASHION_LIKE, "https://www.fibre2fashion.com/x")
        self.assertTrue(page.ok, msg=page.error)
        self.assertIsNotNone(page.published)
        self.assertEqual(page.published.moment.hour, 0)  # 00:00 IST from JSON-LD
        self.assertFalse(page.published.date_only)
        self.assertIn("JSON-LD", page.published.provenance)

    def test_falls_back_to_meta_tags_when_no_json_ld(self):
        page = extract_page(META_ONLY, "https://www.fibre2fashion.com/y")
        self.assertTrue(page.ok, msg=page.error)
        self.assertIsNotNone(page.published)
        self.assertIn("meta article:published_time", page.published.provenance)
        self.assertIsNotNone(page.modified)

    def test_missing_date_returns_none_not_a_guess(self):
        page = extract_page(NO_DATE_AT_ALL, "https://example.com/profile")
        self.assertTrue(page.ok, msg=page.error)
        self.assertIsNone(page.published)
        self.assertIn("Tiruppur", page.body)

    def test_malformed_json_ld_does_not_break_the_page(self):
        """One bad script block must not take the rest of the page with it."""
        page = extract_page(MALFORMED_JSON_LD, "https://example.com/z")
        self.assertTrue(page.ok, msg=page.error)
        self.assertIsNotNone(page.published)
        self.assertIn("meta", page.published.provenance)

    def test_script_and_style_content_is_excluded_from_body(self):
        page = extract_page(FIBRE2FASHION_LIKE, "https://www.fibre2fashion.com/x")
        self.assertNotIn("tracking", page.body.lower())
        self.assertNotIn("display: none", page.body)
        self.assertIn("productivity", page.body.lower())

    def test_body_only_javascript_page_is_not_ok(self):
        page = extract_page(EMPTY_BODY, "https://example.com/spa")
        self.assertFalse(page.ok)
        self.assertIn("no readable text", page.error)

    def test_non_html_content_type_is_refused_without_parsing(self):
        page = extract_page("%PDF-1.4 binary garbage", "https://example.com/a.pdf",
                            content_type="application/pdf")
        self.assertFalse(page.ok)
        self.assertIn("non-HTML", page.error)

    def test_source_is_classified_from_the_url(self):
        page = extract_page(NO_DATE_AT_ALL, "https://www.fibre2fashion.com/news/x")
        self.assertEqual(page.source_name, "Fibre2Fashion")

    def test_unknown_host_gets_unknown_source_type_not_a_crash(self):
        page = extract_page(NO_DATE_AT_ALL, "https://some-random-blog.example/post")
        self.assertEqual(page.source_type, SourceType.UNKNOWN)

    def test_real_world_unescaped_crlf_in_json_ld_still_yields_a_date(self):
        """Regression for a bug caught live against the real 7 Sep 2026
        article: strict JSON parsing lost a genuinely-present datePublished
        because the site's own JSON-LD embeds raw CRLFs in a string field.
        json.loads(..., strict=False) is what fixes it -- confirmed here
        against the exact captured byte pattern, not a synthetic guess."""
        page = extract_page(REAL_WORLD_MALFORMED_JSON_LD,
                            "https://www.fibre2fashion.com/news/apparel-news/x")
        self.assertTrue(page.ok, msg=page.error)
        self.assertIsNotNone(page.published, "lost a real datePublished to a strict JSON parse")
        self.assertEqual(page.published.moment.date().isoformat(), "2026-09-07")

    def test_malformed_html_does_not_raise(self):
        broken = "<html><body><p>unterminated <div>nested <span>tags"
        page = extract_page(broken, "https://example.com/broken")
        self.assertTrue(page.ok)  # HTMLParser tolerates this; body still extracted


class TestClientRenderedPage(unittest.TestCase):
    """Regression for a real loss of same-day news: textiletoday.com.bd
    served a 14 KB shell for a story published that morning -- full <head>
    metadata including `article:published_time`, and zero <p> elements
    because the article body is rendered client-side. Bailing out on the
    empty body discarded a genuinely fresh, correctly-dated article before
    its date was ever read. Markup below mirrors that real page's shape."""

    SHELL = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Europe's textile recycling industry faces growing economic viability challenge</title>
<meta name="description" content="Europe's textile recycling challenge is turning into an economic issue">
<meta property="og:description" content="Europe's textile recycling challenge is turning into an economic issue">
<meta property="article:published_time" content="2026-09-13 13:24:00">
<meta name="DC.date.issued" content="2026-09-13 13:24:00">
</head><body><div id="app"></div></body></html>"""

    def test_date_survives_a_body_that_is_rendered_client_side(self):
        page = extract_page(self.SHELL, "https://textiletoday.com.bd/europes-textile-recycling")
        self.assertTrue(page.ok, msg=f"page was discarded: {page.error!r}")
        self.assertIsNotNone(page.published)
        self.assertEqual(page.published.moment.date().isoformat(), "2026-09-13")

    def test_description_stands_in_as_body_text(self):
        page = extract_page(self.SHELL, "https://textiletoday.com.bd/europes-textile-recycling")
        self.assertIn("recycling", page.body)

    def test_a_page_with_neither_body_nor_description_is_still_refused(self):
        empty = """<!DOCTYPE html><html><head><title>Nothing</title>
<meta property="article:published_time" content="2026-09-13 13:24:00">
</head><body><div id="app"></div></body></html>"""
        page = extract_page(empty, "https://example.com/empty")
        self.assertFalse(page.ok)
        self.assertIn("no readable text", page.error)

    def test_a_real_body_is_still_preferred_over_the_description(self):
        both = """<!DOCTYPE html><html><head>
<meta name="description" content="short summary meta">
</head><body><p>The full article body text is present here.</p></body></html>"""
        page = extract_page(both, "https://example.com/both")
        self.assertIn("full article body text", page.body)
        self.assertNotIn("short summary meta", page.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
