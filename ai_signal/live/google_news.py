"""Optional keyword discovery through Google News RSS.

The feed describes itself as personal/non-commercial, so AI Signal only enables
this provider when the operator explicitly opts in. Google accepts a coarse
``when:Nd`` query. AI Signal then filters every parsed ``pubDate`` against the
exact retrieval bounds before spending its page-fetch budget. Feed dates are
never accepted as evidence: each retained article still has to pass AI Signal's
page-level publication-time gate.
"""

from __future__ import annotations

import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

from ..model import Candidate, Query
from ..providers import PageContent

_ENDPOINT = "https://news.google.com/rss/search"
_USER_AGENT = "AISignal/1.0 (+Google News RSS discovery)"
_TAG_RE = re.compile(r"<[^>]+>")
_DECODING_RE = re.compile(
    r'data-n-a-id="(?P<id>[^"]+)"[^>]*data-n-a-ts="(?P<ts>\d+)"'
    r'[^>]*data-n-a-sg="(?P<sg>[^"]+)"'
)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", unescape(value or ""))).strip()


def _published(item: ET.Element) -> datetime | None:
    raw = (item.findtext("pubDate") or "").strip()
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _in_bounds(moment: datetime, query: Query) -> bool:
    """Use the reporting window's half-open convention: (start, end]."""
    if query.retrieval_start is not None and moment <= query.retrieval_start:
        return False
    if query.retrieval_end is not None and moment > query.retrieval_end:
        return False
    return True


class GoogleNewsSearchProvider:
    name = "google-news"

    def __init__(
        self,
        *,
        language: str = "en-IN",
        country: str = "IN",
        edition: str = "IN:en",
        timeout_s: float = 12.0,
    ) -> None:
        self.language = language
        self.country = country
        self.edition = edition
        self.timeout_s = timeout_s
        self._queries = self._parsed = self._kept = 0
        self._stale = self._undated = self._failed = 0
        #: `search` is called once per query, and queries now run
        #: concurrently -- these six counters are the only state shared
        #: across those calls, so they are the only thing here that needs
        #: a lock. The RSS/text parsing above stays in call-local
        #: variables and is merged in under the lock only once, at the end.
        self._lock = threading.Lock()

    def _url(self, query: Query) -> str:
        days = max(1, math.ceil(max(query.lookback_hours, 1) / 24))
        return _ENDPOINT + "?" + urllib.parse.urlencode(
            {
                "q": f"{query.text} when:{days}d",
                "hl": self.language,
                "gl": self.country,
                "ceid": self.edition,
            }
        )

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        with self._lock:
            self._queries += 1
        if limit <= 0:
            return []
        request = urllib.request.Request(
            self._url(query),
            headers={
                "Accept": "application/rss+xml, application/xml, text/xml",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                root = ET.fromstring(response.read().lstrip())
        except urllib.error.HTTPError as exc:
            with self._lock:
                self._failed += 1
            raise RuntimeError(f"failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            with self._lock:
                self._failed += 1
            raise RuntimeError(f"failed: {exc.reason}") from exc
        except ET.ParseError as exc:
            with self._lock:
                self._failed += 1
            raise RuntimeError(f"failed: invalid RSS ({exc})") from exc

        out: list[Candidate] = []
        seen: set[str] = set()
        parsed = stale = undated = 0
        for item in root.iter("item"):
            parsed += 1
            item_url = (item.findtext("link") or "").strip()
            if not item_url:
                continue
            published = _published(item)
            if published is None:
                undated += 1
                continue
            if not _in_bounds(published, query):
                stale += 1
                continue
            key = item_url.split("#", 1)[0].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Candidate(
                    url=item_url,
                    title=_plain(item.findtext("title") or ""),
                    bucket=query.bucket,
                    snippet=_plain(item.findtext("description") or "")[:200],
                    source_name=(item.findtext("source") or "Google News").strip(),
                    found_by=[query],
                )
            )
            if len(out) >= max(0, limit):
                break

        with self._lock:
            self._parsed += parsed
            self._kept += len(out)
            self._stale += stale
            self._undated += undated
        return out

    def health(self) -> dict[str, str]:
        with self._lock:
            return {
                "Google News": (
                    f"{self._queries} queries; {self._kept} kept of {self._parsed} items; "
                    f"{self._stale} outside exact bounds; {self._undated} undated; "
                    f"{self._failed} failed"
                )
            }


class GoogleNewsPageFetcher:
    """Resolve Google's wrapper URL, then verify the publisher page normally.

    Current Google News RSS links are JavaScript wrapper pages, not HTTP
    redirects. The decoding RPC is undocumented and may change, so failures
    are returned explicitly as NOT_RETRIEVABLE evidence rather than falling
    back to treating Google's wrapper as the article.
    """

    _decode_endpoint = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

    def __init__(self, primary, timeout_s: float = 12.0) -> None:
        self.primary = primary
        self.timeout_s = timeout_s
        self._resolved: dict[str, str] = {}
        #: The fetch phase now runs candidates concurrently, so two worker
        #: threads can call `_resolve` for the same wrapper URL (a
        #: duplicate story picked up from two providers) at the same
        #: moment. Without a lock both would decode it independently --
        #: wasted work, not corruption -- but a plain dict write racing
        #: with a read is still worth closing off rather than relying on
        #: duplicates being rare.
        self._lock = threading.Lock()

    @staticmethod
    def handles(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc.lower() == "news.google.com" and (
            "/rss/articles/" in parsed.path or "/articles/" in parsed.path
        )

    def _resolve(self, url: str) -> str:
        with self._lock:
            cached = self._resolved.get(url)
        if cached:
            return cached
        # The two network calls below are deliberately made outside the
        # lock -- only the cache dict itself needs protecting, not the
        # decode work, so resolving different URLs concurrently still
        # runs in parallel rather than queuing behind one shared lock.
        token = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        article_url = f"https://news.google.com/articles/{token}"
        request = urllib.request.Request(article_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            page = response.read().decode("utf-8", errors="replace")
        match = _DECODING_RE.search(page)
        if not match:
            raise ValueError("Google News wrapper did not expose decoding parameters")

        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,'
            '"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,'
            f'[1,1,1],1,1,null,0,0,null,0],"{match.group("id")}",'
            f'{match.group("ts")},"{match.group("sg")}"]'
        )
        payload = [[[
            "Fbv4je",
            inner,
        ]]]
        body = urllib.parse.urlencode(
            {"f.req": json.dumps(payload, separators=(",", ":"))}
        ).encode("utf-8")
        decode_request = urllib.request.Request(
            self._decode_endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(decode_request, timeout=self.timeout_s) as response:
            decoded = response.read().decode("utf-8", errors="replace")
        chunks = decoded.split("\n\n", 1)
        if len(chunks) != 2:
            raise ValueError("Google News decoder returned an unexpected response")
        rows = json.loads(chunks[1])
        publisher_url = json.loads(rows[0][2])[1]
        if not isinstance(publisher_url, str) or not publisher_url.startswith(("http://", "https://")):
            raise ValueError("Google News decoder did not return a publisher URL")
        with self._lock:
            self._resolved[url] = publisher_url
        return publisher_url

    def fetch(self, url: str) -> PageContent:
        if not self.handles(url):
            return self.primary.fetch(url)
        try:
            resolved = self._resolve(url)
        except Exception as exc:
            return PageContent(body="", ok=False, error=f"Google News URL resolution failed: {exc}")
        page = self.primary.fetch(resolved)
        if page.ok and not page.resolved_url:
            page.resolved_url = resolved
        return page
