"""Discover AI news from public RSS feeds.

Feed timestamps limit and prioritize discovery. Publisher pages supply publication evidence.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from ..model import Candidate, Query
from ..query_match import matches as query_matches

_USER_AGENT = "AISignal/1.0 (+public RSS reader)"

_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml"

DEFAULT_FEEDS: dict[str, str] = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google AI": "https://blog.google/innovation-and-ai/technology/ai/rss/",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "AWS Machine Learning": "https://aws.amazon.com/blogs/machine-learning/feed/",
    "NVIDIA Blog": "https://blogs.nvidia.com/feed/",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Microsoft Research": "https://www.microsoft.com/en-us/research/feed/",
    "GitHub AI": "https://github.blog/ai-and-ml/feed/",
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "MIT News AI": "https://news.mit.edu/rss/topic/artificial-intelligence2",
    "Microsoft Azure": "https://azure.microsoft.com/en-us/blog/feed/",
}

_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|#39|#8230|nbsp);")
_ENTITY_MAP = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'",
    "#8230": "...", "nbsp": " ",
}

#: Matches a bare "&" not already starting a real XML entity reference.
_BARE_AMPERSAND_RE = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")

#: A cheap, cheap-to-check signal that a response is actually RSS/Atom
#: before handing it to the strict XML parser -- distinguishes "this feed
#: has a fixable escaping quirk" from "this URL never returns feed content
#: at all" (Sourcing Journal: every feed path served the full HTML
#: homepage instead), so the latter fails fast with a clear reason rather
#: than a cryptic expat parse error several thousand lines of embedded
#: HTML/JS later.
_LOOKS_LIKE_FEED_RE = re.compile(rb"<(rss[\s>]|feed[\s>])", re.IGNORECASE)


def _tolerant(raw: bytes) -> bytes:
    """Real-world RSS is not always well-formed XML. One specific
    malformation was caught live, fetching a real feed, and is corrected
    here rather than left to crash the parser: a leading tab before the
    XML declaration (Apparel Resources) -- the XML spec requires the
    declaration to be the very first thing in the document, so a strict
    parser correctly rejects anything before it as "not at start of
    entity". A common WordPress quirk, not a fetch error.

    Bare, unescaped "&" characters (e.g. from embedded, non-CDATA-wrapped
    JavaScript) are escaped too, for the same reason: a lone "&" must
    start a real entity reference per the XML spec, so a strict parser
    correctly rejects one that doesn't. Only ampersands not already part
    of a valid entity are touched, so real entities like "&amp;" pass
    through unchanged. This alone was not enough to make Sourcing
    Journal's feed parse -- that feed turned out not to be RSS at all
    (see DEFAULT_FEEDS' comment) -- but it is a genuine, general fix for
    any feed that otherwise validly wraps a stray "&&" in inline script.

    Neither correction is a parsing choice -- expat is right to reject both
    inputs as written; this is deliberate leniency toward real feeds that
    do not quite follow the spec, the same trade-off every RSS reader in
    the wild makes.
    """
    raw = raw.lstrip()
    return _BARE_AMPERSAND_RE.sub(b"&amp;", raw)


def _strip_html(html: str) -> str:
    """Minimal entity/tag stripping for an RSS description or
    content:encoded block -- enough to give the verification gate plain
    text to scan for event-cue words; not a general HTML-to-text tool."""
    text = _TAG_RE.sub(" ", html or "")
    text = _ENTITY_RE.sub(lambda m: _ENTITY_MAP.get(m.group(1), m.group(0)), text)
    return re.sub(r"\s+", " ", text).strip()


#: Timestamp used to order undated items last. Any real feed date sorts
#: above it, and it is a fixed constant rather than "now" so ordering never
#: depends on when the sweep runs.
_NO_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _feed_date(item: ET.Element) -> Optional[datetime]:
    """A feed date is a discovery hint. It is never copied to a candidate."""
    for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date"):
        raw = (item.findtext(tag) or "").strip()
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if parsed is None:
            continue
        # A feed may omit the zone offset entirely; assume UTC rather than
        # returning a naive datetime, which would blow up the sort below
        # the moment it met a tz-aware one from another feed.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    body: str
    source_name: str
    #: Feed-declared timestamp, for ordering only -- see `_feed_date`.
    published_hint: Optional[datetime] = None


@dataclass
class RssSearchProvider:
    """Polls a fixed set of RSS feeds once, caches the parsed items for
    `cache_ttl_s`, and matches watchlist queries against that cache using
    `query_match` (boolean AND/OR structure, not bag-of-words token
    overlap -- see that module's docstring for why) -- feeds are polled,
    not searched per-query, so caching is what keeps one sweep's 90+
    queries from re-fetching every feed 90+
    times."""

    name: str = "rss"
    feeds: dict = field(default_factory=lambda: dict(DEFAULT_FEEDS))
    cache_ttl_s: float = 600.0
    timeout_s: float = 12.0

    _items: Optional[list] = field(default=None, init=False, repr=False)
    _fetched_at: Optional[float] = field(default=None, init=False, repr=False)
    _health: dict = field(default_factory=dict, init=False, repr=False)
    #: Items each feed returned the last time it returned any, so a sudden
    #: drop to zero can be told apart from a feed that is simply quiet.
    _last_success_count: dict = field(default_factory=dict, init=False, repr=False)
    #: Guards the check-then-fetch below. Without it, several worker
    #: threads calling `search` around the same time -- the whole point of
    #: running discovery concurrently -- could all see a stale cache at
    #: once and each poll all fifteen feeds themselves, instead of one
    #: poll shared by all of them.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _ensure_fresh(self) -> None:
        with self._lock:
            now = time.monotonic()
            if (
                self._items is not None
                and self._fetched_at is not None
                and (now - self._fetched_at) < self.cache_ttl_s
            ):
                return
            self._items = self._fetch_all()
            self._fetched_at = now

    def _fetch_all(self) -> list[FeedItem]:
        items: list[FeedItem] = []
        seen: set[str] = set()
        self._health = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            feeds = {
                name: pool.submit(self._fetch_one, url, name)
                for name, url in self.feeds.items()
            }
            for name, pending in feeds.items():
                try:
                    entries = pending.result()
                except Exception as exc:  # One unavailable feed does not block the rest.
                    self._health[name] = f"failed: {exc}"
                    continue
                last_seen = self._last_success_count.get(name)
                if not entries and last_seen:
                    self._health[name] = f"DEGRADED: returned 0 items, was {last_seen} on the previous poll"
                    continue
                if entries:
                    self._last_success_count[name] = len(entries)
                self._health[name] = f"ok: {len(entries)} items"
                for entry in entries:
                    key = entry.url.split("?")[0].rstrip("/").lower()
                    if key not in seen:
                        seen.add(key)
                        items.append(entry)

        # Retain stable ordering even when feeds complete at different speeds.
        items.sort(key=lambda i: (-(i.published_hint or _NO_DATE).timestamp(), i.url))
        return items

    def _fetch_one(self, feed_url: str, source_name: str) -> list[FeedItem]:
        request = urllib.request.Request(
            feed_url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
            raw = resp.read()
        if not _LOOKS_LIKE_FEED_RE.search(raw[:4096]):
            raise ValueError("response is not RSS/Atom (no <rss> or <feed> tag found)")
        root = ET.fromstring(_tolerant(raw))
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            content = item.findtext(_CONTENT_ENCODED) or ""
            desc = item.findtext("description") or ""
            body = _strip_html(content or desc)
            out.append(
                FeedItem(
                    title=title,
                    url=link,
                    body=body,
                    source_name=source_name,
                    published_hint=_feed_date(item),
                )
            )
        return out

    def health(self) -> dict:
        """Per-feed status from the most recent fetch, e.g.
        `{"Just Style": "ok: 24 items", "Apparel Views": "failed: HTTP 403"}`.
        Empty until the first `search()` call triggers a fetch."""
        return dict(self._health)

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        self._ensure_fresh()
        out: list[Candidate] = []
        for entry in self._items or []:
            if (
                query.retrieval_start is not None
                and entry.published_hint is not None
                and entry.published_hint < query.retrieval_start
            ):
                continue
            if not query_matches(entry.title + " " + entry.body, query.text):
                continue
            out.append(
                Candidate(
                    url=entry.url,
                    title=entry.title,
                    bucket=query.bucket,
                    snippet=entry.body[:200],
                    found_by=[query],
                )
            )
            if len(out) >= limit:
                break
        return out
