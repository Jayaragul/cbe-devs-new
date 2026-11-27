"""Core data types for the fetcher.

The design keeps an explicit *evidence ledger* per candidate rather than a
single relevance score. That is a direct response to Liu, Zhang & Liang,
"Evaluating Verifiability in Generative Search Engines" (arXiv:2304.09848),
which found that in production generative search engines only 51.5% of
generated sentences were fully supported by their citations. A single score
cannot express "retrieved confidently but not verifiable", so we never
collapse retrieval confidence and verification status into one number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Bucket(str, Enum):
    """Which monitoring category a candidate belongs to."""

    CLIENTS = "clients"
    COMPETITORS = "competitors"
    TOPICS = "topics"


class SourceType(str, Enum):
    """How much independent weight a source carries.

    Ordering matters: when the same development appears from several
    sources, `dedup.pick_canonical` prefers the strongest type.
    """

    ORIGINAL_REPORTING = "original reporting"
    TRADE_PUBLICATION = "trade publication"
    REGULATORY_FILING = "regulatory filing"
    COMPANY_PRESS_RELEASE = "company press release"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


#: Source types that must be labelled as promotional rather than presented
#: as independently verified reporting.
PROMOTIONAL = frozenset({SourceType.COMPANY_PRESS_RELEASE})


class Rejection(str, Enum):
    """Why a candidate did not make the report.

    Every drop is recorded. A sweep that returns nothing must be able to
    show *what it rejected and why* — otherwise "no news" is
    indistinguishable from a broken pipeline.
    """

    OUT_OF_WINDOW = "published outside the reporting window"
    DATE_UNVERIFIABLE = "publication date could not be verified on the page"
    DATE_AMBIGUOUS = "date-only item whose eligibility is ambiguous"
    ENTITY_MISMATCH = "matched a different company with a similar name"
    NO_DEVELOPMENT = "passing mention, profile, or generated stock page"
    DUPLICATE = "same development already reported from a stronger source"
    NOT_RETRIEVABLE = "source page could not be opened"
    OFF_TOPIC = "no clear AI relevance"


@dataclass(frozen=True)
class Query:
    """One search string sent to one provider.

    `bucket` and `entity` are carried through so that a hit can be traced
    back to the watchlist row that caused the search — needed for the
    per-entity attribution in the report.
    """

    text: str
    bucket: Bucket
    entity: Optional[str] = None
    #: Set when the query is a machine-expanded variant rather than a
    #: watchlist term, so expansion can be evaluated separately.
    expanded_from: Optional[str] = None
    #: How far back a provider should be asked to search (a freshness
    #: hint passed to e.g. Brave's `freshness=` or Bing's date-range
    #: parameter). Deliberately NOT the reporting window: search engines
    #: index on a lag, so a query restricted to exactly 24h can miss an
    #: article that was genuinely published inside the window but crawled
    #: a few hours late. Widening the *search* net to 72h buys recall
    #: margin against that lag. It changes nothing about what is
    #: eventually reported -- `verify.gate` alone decides inclusion, by
    #: checking each page's actual publication time against the strict
    #: reporting window. A candidate found via a 72h search that turns out
    #: to be 50 hours old is still rejected as OUT_OF_WINDOW.
    lookback_hours: int = 72
    #: Exact retrieval bounds, stamped by ``pipeline.run`` from its explicit
    #: clock. Search providers may use these to discard stale index/feed
    #: entries before the page-fetch budget is spent. They remain retrieval
    #: hints only; the page-level verification gate is still authoritative.
    retrieval_start: Optional[datetime] = None
    retrieval_end: Optional[datetime] = None


@dataclass
class Candidate:
    """A retrieved document, before verification."""

    url: str
    title: str
    bucket: Bucket
    #: Watchlist entities this candidate is claimed to concern.
    entities: list[str] = field(default_factory=list)
    snippet: str = ""
    #: Body text, populated only once the page has actually been opened.
    body: str = ""
    source_name: str = ""
    source_type: SourceType = SourceType.UNKNOWN

    #: Publication instant, tz-aware, once verified on the page itself.
    published_at: Optional[datetime] = None
    #: True when the page gave a date but no time of day.
    date_only: bool = False
    #: Where on the page the date came from, so a reviewer can re-check it.
    date_provenance: str = ""

    #: Per-provider ranks, keyed by provider name. Consumed by RRF.
    ranks: dict[str, int] = field(default_factory=dict)
    #: Which queries surfaced this document.
    found_by: list[Query] = field(default_factory=list)

    fused_score: float = 0.0

    def __hash__(self) -> int:  # candidates are identified by URL
        return hash(self.url)


@dataclass
class Verdict:
    """The outcome of the verification gate for one candidate."""

    candidate: Candidate
    included: bool
    #: Populated when `included` is False.
    rejection: Optional[Rejection] = None
    #: Human-readable justification, shown in the verification record.
    reason: str = ""
    #: Other URLs found to describe the same development.
    duplicates: list[str] = field(default_factory=list)

    @property
    def promotional(self) -> bool:
        """Whether the item must be labelled as a company announcement."""
        return self.candidate.source_type in PROMOTIONAL
