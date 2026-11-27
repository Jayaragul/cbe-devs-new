"""Search and page-fetch adapters.

Two boundaries, deliberately narrow, because everything above them is
testable without network access and everything below them needs credentials.

`SearchProvider` is the fan-out boundary. Running more than one is the point,
not redundancy: RRF (see `rank`) is only worth anything with several
independent rankings to fuse, and providers disagree substantially about
recency. Brave, Serper, Tavily and Exa all fit this shape; none is wired up
here, since no key exists in this environment.

`PageFetcher` is the verification boundary, and it is the one that cannot be
skipped. The whole gate in `verify` rests on having opened the page, so a
provider snippet is never allowed to stand in for a fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from .model import Bucket, Candidate, Query, SourceType
from .temporal import DateFinding


class SearchProvider(Protocol):
    """Returns a *ranked* list of candidates; order carries the rank.

    A live implementation should honour `query.lookback_hours` if the API
    supports a freshness or date-range parameter (Brave's `freshness=`,
    Bing's date filters, a Google Custom Search `dateRestrict`, and so on).
    It is a recall hint only -- returning results older than that, or
    ignoring it entirely, does not corrupt the report: `verify.gate` is the
    sole authority on what is included, checked against `window_hours`, not
    against whatever the provider was asked for.
    """

    name: str

    def search(self, query: Query, limit: int = 20) -> list[Candidate]: ...


@dataclass
class PageContent:
    """What opening a page yielded."""

    body: str
    published: Optional[DateFinding] = None
    modified: Optional[DateFinding] = None
    source_type: SourceType = SourceType.UNKNOWN
    source_name: str = ""
    #: False when the fetch failed; the gate turns this into NOT_RETRIEVABLE.
    ok: bool = True
    error: str = ""
    #: Final publisher URL after HTTP redirects or an aggregator-wrapper
    #: resolution. The pipeline adopts it before source classification and
    #: story deduplication, so reports link to the article rather than a
    #: search-engine wrapper.
    resolved_url: str = ""
    summary: str = ""


class PageFetcher(Protocol):
    def fetch(self, url: str) -> PageContent: ...


@dataclass
class RoutingFetcher:
    """Sends each URL to the fetcher that can actually speak for it.

    Some candidates are not web pages. An exchange filing arrives with its
    content and its broadcast timestamp already retrieved from the
    exchange's announcement record, so re-opening the attached PDF over
    HTTP would be a worse witness to the same facts, not a better one.

    `pipeline.run` takes exactly one fetcher, deliberately -- every
    candidate must go through the same door. This preserves that: it is
    one fetcher, which consults specialists first and falls back to the
    ordinary page fetcher for everything else. Order is explicit rather
    than incidental, so which fetcher answers for a URL never depends on
    dict iteration or registration timing.
    """

    primary: PageFetcher
    #: Consulted in order; each must expose `handles(url)` and `fetch(url)`.
    specialists: Sequence = ()

    def fetch(self, url: str) -> PageContent:
        for specialist in self.specialists:
            if specialist.handles(url):
                return specialist.fetch(url)
        return self.primary.fetch(url)


# --------------------------------------------------------------------------
# Offline implementations, used by the tests and for dry runs
# --------------------------------------------------------------------------


@dataclass
class StaticProvider:
    """A provider backed by a fixed, ordered result list per query text."""

    name: str
    results: dict[str, list[Candidate]] = field(default_factory=dict)

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        out = []
        for proto in self.results.get(query.text, [])[:limit]:
            # Copy, so two providers returning the same URL do not share the
            # mutable rank/entity state that `merge_by_url` accumulates.
            out.append(
                Candidate(
                    url=proto.url,
                    title=proto.title,
                    bucket=query.bucket,
                    entities=list(proto.entities),
                    snippet=proto.snippet,
                    body=proto.body,
                    source_name=proto.source_name,
                    source_type=proto.source_type,
                    published_at=proto.published_at,
                    date_only=proto.date_only,
                    date_provenance=proto.date_provenance,
                    found_by=[query],
                )
            )
        return out


@dataclass
class StaticFetcher:
    """A fetcher backed by a URL -> PageContent map."""

    pages: dict[str, PageContent] = field(default_factory=dict)

    def fetch(self, url: str) -> PageContent:
        page = self.pages.get(url)
        if page is None:
            return PageContent(body="", ok=False, error="not in fixture set")
        return page


#: Hosts whose articles are original trade reporting, used to classify
#: sources when a fetcher cannot infer the type itself. Kept small and
#: explicit rather than guessed, since the label affects how the report
#: presents the item.
TRADE_HOSTS = {
    "fibre2fashion.com": "Fibre2Fashion",
    "apparelresources.com": "Apparel Resources",
    "just-style.com": "Just Style",
    "textiletoday.com.bd": "Textile Today",
    "textilefocus.com": "Textile Focus",
    "apparelviews.com": "Apparel Views",
    "indiantextilemagazine.in": "The Textile Magazine",
    "textileworld.com": "Textile World",
    "wwd.com": "Sourcing Journal (WWD)",
    "sourcingjournal.com": "Sourcing Journal",
    "textilesresources.com": "Textiles Resources",
    "knittingviews.com": "Knitting Views",
    "globaltextiletimes.com": "Global Textile Times",
}

REPORTING_HOSTS = {
    "arstechnica.com": "Ars Technica",
    "news.mit.edu": "MIT News",
    "techcrunch.com": "TechCrunch",
}

COMPANY_HOSTS = {
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "blog.google": "Google",
    "deepmind.google": "Google DeepMind",
    "huggingface.co": "Hugging Face",
    "aws.amazon.com": "Amazon Web Services",
    "blogs.nvidia.com": "NVIDIA",
    "github.blog": "The GitHub Blog",
    "microsoft.com": "Microsoft Research",
    "azure.microsoft.com": "Microsoft Azure",
}

EXCHANGE_HOSTS = {
    "bseindia.com": "BSE",
    "nseindia.com": "NSE",
    # Filed documents are served from the archive subdomain, not the main
    # host, so a filing's own URL would otherwise classify as UNKNOWN and
    # lose the source weight `dedup.pick_canonical` gives a filing.
    "nsearchives.nseindia.com": "NSE",
    "trendlyne.com": "Trendlyne (BSE/NSE mirror)",
}


def classify_source(host: str) -> tuple[SourceType, str]:
    """Map a host to a source type and display name."""
    host = host.lower().removeprefix("www.")
    if host in REPORTING_HOSTS:
        return SourceType.ORIGINAL_REPORTING, REPORTING_HOSTS[host]
    if host in COMPANY_HOSTS:
        return SourceType.COMPANY_PRESS_RELEASE, COMPANY_HOSTS[host]
    if host in TRADE_HOSTS:
        return SourceType.TRADE_PUBLICATION, TRADE_HOSTS[host]
    if host in EXCHANGE_HOSTS:
        return SourceType.REGULATORY_FILING, EXCHANGE_HOSTS[host]
    return SourceType.UNKNOWN, host
