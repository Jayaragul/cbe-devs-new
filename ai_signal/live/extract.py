"""Turn a fetched HTML page into a `PageContent` -- pure, deterministic.

Deliberately split from the module that issues the HTTP request
(`http_fetcher.py`). This half takes bytes already in hand and is a pure
function of (html, headers, url): same input, same output, every time. That
split means it can be tested directly with recorded HTML fixtures, with no
network and no real site's markup drifting under the tests -- the same
pattern as `StaticProvider`/`StaticFetcher` elsewhere in this package.

Uses only `html.parser.HTMLParser` (stdlib) rather than a third-party HTML
library. This is not a general-purpose scraper: it looks for the specific
signals a news page carries -- a `<title>`, `<meta>` tags, and JSON-LD
`NewsArticle`/`Article` blocks -- and falls back to plain visible text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

from .. import temporal
from ..model import SourceType
from ..providers import PageContent, classify_source
from ..temporal import DateFinding

#: Tags whose content is never part of the readable article body.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

#: Meta tag names/properties that carry a publication timestamp, checked in
#: this order (first match wins). Covers the schemes actually seen on trade
#: press: OpenGraph article tags, Dublin Core, and bare "date"/"pubdate".
_PUBLISHED_META = (
    "article:published_time",
    "og:article:published_time",
    "datePublished",
    "date.published",
    "dc.date.issued",
    "sailthru.date",
    "pubdate",
    "date",
)
_MODIFIED_META = (
    "article:modified_time",
    "og:article:modified_time",
    "dateModified",
    "date.modified",
    "og:updated_time",
    "lastmod",
)

#: Body length cap. A verification gate reads for event cues and entity
#: mentions in the first few paragraphs; capping keeps cassette files and
#: memory bounded against a page that turns out to be enormous.
MAX_BODY_CHARS = 20_000


@dataclass
class _Parsed:
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    ld_json_blocks: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


class _Extractor(HTMLParser):
    """One pass over the document: title, meta tags, JSON-LD, body text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = _Parsed()
        self._skip_depth = 0
        self._in_title = False
        self._in_ld_json = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrd = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            if tag == "script" and (attrd.get("type") or "").lower() == "application/ld+json":
                self._in_ld_json = True
                self._ld_buf = []
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attrd.get("property") or attrd.get("name")
            content = attrd.get("content")
            if key and content is not None:
                # First occurrence wins -- pages sometimes repeat a tag with
                # a blank fallback further down.
                self.out.meta.setdefault(key.strip(), content.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            if tag == "script" and self._in_ld_json:
                self._in_ld_json = False
                block = "".join(self._ld_buf).strip()
                if block:
                    self.out.ld_json_blocks.append(block)
                self._ld_buf = []
            return
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_buf.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.out.title += data
            return
        stripped = data.strip()
        if stripped:
            self.out.text_parts.append(stripped)


def _normalise_text(parts: list[str]) -> str:
    text = " ".join(parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_BODY_CHARS]


def _ld_json_date(blocks: list[str], key: str) -> Optional[str]:
    """First `key` found in any parseable JSON-LD block.

    A page can carry several JSON-LD blocks (breadcrumbs, organisation,
    the article itself); this walks all of them rather than assuming the
    article schema is first, and tolerates one block being malformed JSON
    without losing the others.
    """
    for raw in blocks:
        try:
            # strict=False permits literal control characters (raw \r\n,
            # \t) inside JSON string values. RFC 8259 forbids them, but
            # they show up in the wild: Fibre2Fashion's own NewsArticle
            # JSON-LD embeds a multi-line "description" with unescaped
            # CRLFs rather than "\n", which the strict parser rejects
            # outright -- silently losing a real, present datePublished.
            data = json.loads(raw, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and key in obj and obj[key]:
                return str(obj[key])
    return None


def _find_date(parsed: _Parsed, meta_names: tuple[str, ...], ld_key: str) -> Optional[DateFinding]:
    ld_value = _ld_json_date(parsed.ld_json_blocks, ld_key)
    if ld_value:
        found = temporal.parse_page_date(ld_value, f"JSON-LD {ld_key}")
        if found:
            return found
    for name in meta_names:
        if name in parsed.meta:
            found = temporal.parse_page_date(parsed.meta[name], f"meta {name}")
            if found:
                return found
    return None


def extract_page(html_text: str, url: str, content_type: str = "") -> PageContent:
    """Parse a fetched HTML document into a `PageContent`.

    Never raises on malformed markup -- `HTMLParser` is forgiving by design,
    and a page that yields no usable date or body still returns a
    `PageContent` (with `published=None`), which the verification gate
    correctly turns into DATE_UNVERIFIABLE rather than a crash.
    """
    if content_type and "html" not in content_type.lower():
        return PageContent(
            body="", ok=False,
            error=f"non-HTML content-type: {content_type!r}; extraction not attempted",
        )

    parser = _Extractor()
    try:
        parser.feed(html_text)
    except Exception as exc:  # HTMLParser is forgiving; this is a last resort
        return PageContent(body="", ok=False, error=f"HTML parsing failed: {exc}")

    parsed = parser.out
    summary = parsed.meta.get("og:description") or parsed.meta.get("description") or ""
    body = _normalise_text(parsed.text_parts)
    if not body:
        # A page whose article text is rendered client-side yields no
        # readable text at all, yet can still carry a perfectly good
        # publication date in its <head>. Caught live on a real, same-day
        # article (textiletoday.com.bd) that served a 14 KB shell with
        # `article:published_time` present and zero <p> elements: bailing
        # out here discarded a genuinely fresh, correctly-dated story
        # before the date was ever read.
        #
        # The page's own description meta stands in as body text. That is
        # not the snippet-trusting this package refuses elsewhere -- the
        # page WAS opened, and this is its own publisher-set summary from
        # the document just fetched, not a search engine's paraphrase of
        # it. It is thin, so the verification gate will often reject it
        # for want of event language; being rejected on the merits is the
        # point, rather than being dropped as unreadable.
        body = _normalise_text([summary])
        if not body:
            return PageContent(
                body="", ok=False, error="no readable text extracted from page"
            )

    published = _find_date(parsed, _PUBLISHED_META, "datePublished")
    modified = _find_date(parsed, _MODIFIED_META, "dateModified")

    from urllib.parse import urlparse

    source_type, source_name = classify_source(urlparse(url).netloc)

    return PageContent(
        body=body,
        published=published,
        modified=modified,
        source_type=source_type,
        source_name=source_name,
        ok=True,
        summary=summary,
    )
