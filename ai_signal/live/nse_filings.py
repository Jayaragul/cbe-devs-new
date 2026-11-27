"""Corporate announcements straight from the exchange, for listed clients.

The measurement that motivated this file: across 150 articles pooled from
ten trade-press RSS feeds, only 3 of 24 watchlist companies were mentioned
even once. For the listed Indian firms on the watchlist -- K.P.R. Mill,
Kitex, SP Apparels, Bannari Amman -- the real, dated, checkable news
(results, expansions, board decisions, acquisitions) does not break in
international trade press at all. It breaks as an exchange filing. Waiting
for a trade publication to notice it is waiting for a second-hand account
of a document that was already public.

NSE publishes those announcements as JSON at

    /api/corporate-announcements?index=equities&symbol=<SYM>
        [&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY]

and its robots.txt is `User-agent: * / Allow: /` with only
`/market-data-test` disallowed, so this path is fetchable as published.
The date range matters and is not an optimisation detail: an unfiltered
symbol query returned 776 records / 551 KB for one company, the same query
bounded to a week returned 3 records / 2 KB.

Why this is a *stronger* date than anything on a news page: `an_dt` is the
timestamp the exchange itself broadcast the disclosure at. It is not a
publisher's CMS field that can be re-stamped on edit, and it is not a
search engine's guess. It is the authoritative record of when the
information became public.

The split this package insists on everywhere else is preserved exactly.
`NseFilingsProvider` is a `SearchProvider`: it returns bare candidates
carrying no date. `FilingsFetcher` is the matching `PageFetcher`, and it is
what supplies the broadcast timestamp -- the same shape as
`RssSearchProvider` + `HttpPageFetcher`, so `pipeline.run` needs no special
case for filings and the verification gate judges them on identical terms.
A routine "schedule of analyst meet" filing is expected to be rejected for
want of a real development, and that is correct, not a gap.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .. import temporal
from ..model import Candidate, Query, SourceType
from ..providers import PageContent
from ..query_match import matches as query_matches

_API = "https://www.nseindia.com/api/corporate-announcements"

#: NSE serves the JSON to an ordinary browser User-Agent but 403s the
#: default urllib one; `Referer` is sent because the endpoint is the same
#: one the public corporate-filings page calls.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AISignal/1.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}

#: NSE's own format in `an_dt`, e.g. "13-Sep-2026 19:14:31".
_AN_DT_FORMAT = "%d-%b-%Y %H:%M:%S"


def _parse_broadcast(raw: str) -> Optional[datetime]:
    """Parse an NSE broadcast timestamp as IST.

    Attaching IST here is not the same unverified guess this package
    refuses when a random publisher emits a clock time with no offset. The
    zone is a property of the source, not an assumption about it: NSE is an
    Indian exchange and its disclosure timestamps are IST by definition.
    """
    try:
        return datetime.strptime(raw.strip(), _AN_DT_FORMAT).replace(tzinfo=temporal.IST)
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class Filing:
    symbol: str
    company: str
    subject: str
    text: str
    url: str
    broadcast_at: datetime

    def searchable_text(self) -> str:
        return f"{self.company} {self.subject} {self.text}"


@dataclass
class NseFilingsProvider:
    """Polls NSE corporate announcements for a fixed set of symbols.

    Symbols come from the watchlist (`WatchTerm.nse_symbol`), so only
    companies explicitly marked as listed are queried -- this never
    wanders beyond the watchlist, and never crawls the exchange at large.
    """

    symbols: list[str] = field(default_factory=list)
    lookback_hours: int = 72
    timeout_s: float = 20.0
    #: Exchange endpoints deserve the same courtesy as any other host.
    min_interval_s: float = 1.0

    _filings: Optional[list] = field(default=None, init=False, repr=False)
    _health: dict = field(default_factory=dict, init=False, repr=False)
    _last_request_at: float = field(default=0.0, init=False, repr=False)
    #: Guards `_ensure_fresh`'s check-then-fetch. Every symbol already goes
    #: through `_throttle` one at a time inside that fetch, so holding this
    #: lock for the whole thing -- unlike the per-host lock in
    #: http_fetcher.py -- costs nothing extra: NSE's own rate limit already
    #: means only one request to it is ever in flight regardless.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    name = "nse-filings"

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _fetch_symbol(self, symbol: str, now: datetime) -> list[Filing]:
        start = now - timedelta(hours=self.lookback_hours)
        url = (
            f"{_API}?index=equities&symbol={symbol}"
            f"&from_date={start.strftime('%d-%m-%Y')}"
            f"&to_date={now.strftime('%d-%m-%Y')}"
        )
        self._throttle()
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"unexpected response shape: {type(payload).__name__}")

        out: list[Filing] = []
        for row in payload:
            broadcast = _parse_broadcast(row.get("an_dt") or "")
            link = (row.get("attchmntFile") or "").strip()
            # NSE writes a literal "-" in this field when there is no filed
            # document (seen live on an exchange-issued clarification
            # request), so an emptiness check alone lets "-" through as a
            # URL. Require something actually fetchable.
            if not link.lower().startswith("http"):
                link = ""
            if broadcast is None or not link:
                # Without a timestamp there is nothing to verify against the
                # window, and without the filed document there is nothing to
                # cite -- either way it cannot become a report line.
                continue
            out.append(
                Filing(
                    symbol=str(row.get("symbol") or symbol),
                    company=str(row.get("sm_name") or "").strip(),
                    subject=str(row.get("desc") or "").strip(),
                    text=str(row.get("attchmntText") or "").strip(),
                    url=link,
                    broadcast_at=broadcast,
                )
            )
        return out

    def _ensure_fresh(self, now: Optional[datetime] = None) -> None:
        with self._lock:
            if self._filings is not None:
                return
            now = now or datetime.now(temporal.IST)
            collected: list[Filing] = []
            seen: set[str] = set()
            self._health = {}
            for symbol in self.symbols:
                try:
                    rows = self._fetch_symbol(symbol, now)
                except Exception as exc:  # one bad symbol must not sink the sweep
                    self._health[symbol] = f"failed: {exc}"
                    continue
                self._health[symbol] = f"ok: {len(rows)} filing(s)"
                for filing in rows:
                    if filing.url in seen:
                        continue
                    seen.add(filing.url)
                    collected.append(filing)
            # Newest first, url as a tiebreak so the order is total and a
            # rerun on identical input produces an identical queue.
            collected.sort(key=lambda f: (-f.broadcast_at.timestamp(), f.url))
            self._filings = collected

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        self._ensure_fresh()
        out: list[Candidate] = []
        for filing in self._filings or []:
            if not query_matches(filing.searchable_text(), query.text):
                continue
            out.append(
                Candidate(
                    url=filing.url,
                    title=f"{filing.company}: {filing.subject}" if filing.subject
                    else filing.company,
                    bucket=query.bucket,
                    snippet=filing.text[:200],
                    found_by=[query],
                )
            )
            if len(out) >= limit:
                break
        return out

    def fetcher(self) -> "FilingsFetcher":
        """The `PageFetcher` that serves what this provider already holds."""
        self._ensure_fresh()
        return FilingsFetcher({f.url: f for f in (self._filings or [])})

    def health(self) -> dict:
        return dict(self._health)


@dataclass
class FilingsFetcher:
    """Serves filings already retrieved from the exchange.

    This does not re-open the PDF. The citable facts -- what was disclosed,
    by whom, and when the exchange broadcast it -- came from the exchange's
    own announcement record, which is the authoritative source for exactly
    those facts. Opening the attached PDF would add a second retrieval
    without adding a better witness to any of them.
    """

    filings_by_url: dict

    def handles(self, url: str) -> bool:
        return url in self.filings_by_url

    def fetch(self, url: str) -> PageContent:
        filing = self.filings_by_url.get(url)
        if filing is None:
            return PageContent(body="", ok=False, error="not a retrieved filing")
        return PageContent(
            body=f"{filing.subject}. {filing.text}".strip(),
            published=temporal.DateFinding(
                filing.broadcast_at,
                date_only=False,
                provenance="NSE exchange broadcast timestamp (an_dt)",
            ),
            source_type=SourceType.REGULATORY_FILING,
            source_name=f"NSE filing ({filing.symbol})",
            ok=True,
        )
