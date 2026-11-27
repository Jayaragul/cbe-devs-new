"""Collapsing syndicated copies of one development into a single row.

Trade press runs heavily on wire copy and verbatim press releases, so the
naive sweep returns the same announcement five times. Two findings shape the
approach here:

*   Hashing is no longer sufficient on its own. "Benchmarking Near-Duplicate
    Detection in the era of Pay-walled News" (WWW Companion 2025,
    doi:10.1145/3701716.3715303) reports that MinHash-LSH and SimHash score
    *significantly* worse on contemporary news than they did on the older
    SpotSigs benchmark, because paywalls and boilerplate truncate the very
    body text those fingerprints depend on. So body overlap is one signal
    among several here, never the sole test, and `Scorer` is a seam where a
    sentence-embedding model can be dropped in.
*   Preferring the earliest member of a cluster is first-story detection, the
    task defined in the Topic Detection and Tracking line of work (Petrović,
    Osborne & Lavrenko, "Streaming First Story Detection with Application to
    Twitter", NAACL 2010). The origin of a development is more useful to a
    reader than whichever copy a search engine happened to rank first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from .model import Candidate, SourceType

_WORD = re.compile(r"[a-z0-9]+")

#: Words that carry no disambiguating power in this domain: they appear in
#: nearly every headline, so leaving them in inflates title similarity.
_STOP = frozenset(
    """a an the and or of to in on for with at by from as is are was were be
    been it its this that these those new says said after amid over into up
    down per cent percent pc mn bn cr crore lakh inr usd rs""".split()
)


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    """Word k-grams. k=5 is the usual near-duplicate setting for prose."""
    ws = tokens(text)
    if len(ws) < k:
        return {tuple(ws)} if ws else set()
    return {tuple(ws[i : i + k]) for i in range(len(ws) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> float:
    """Bag-of-words overlap on headlines, normalised by the shorter one.

    Containment rather than Jaccard, because syndicators routinely append a
    publication name or a market suffix to an otherwise identical headline.
    """
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


#: A pluggable body-similarity function, so an embedding model can replace
#: shingle overlap without touching the clustering logic.
Scorer = Callable[[Candidate, Candidate], float]


def shingle_scorer(a: Candidate, b: Candidate) -> float:
    text_a = a.body or a.snippet
    text_b = b.body or b.snippet
    return jaccard(shingles(text_a), shingles(text_b))


@dataclass(frozen=True)
class Thresholds:
    body: float = 0.45
    title: float = 0.70
    #: Hours apart beyond which two items are treated as separate
    #: developments even if the text matches, since a re-run of the same
    #: story a week later is a new event for a 24h sweep.
    max_gap_hours: float = 72.0


def same_development(
    a: Candidate,
    b: Candidate,
    scorer: Scorer = shingle_scorer,
    thresholds: Thresholds = Thresholds(),
) -> bool:
    """Whether two candidates describe one development.

    Either strong body overlap or a near-identical headline is enough, but
    both are additionally gated on publication proximity and on the two items
    not being about disjoint sets of monitored entities.
    """
    if a.url == b.url:
        return True

    if a.published_at and b.published_at:
        gap = abs((a.published_at - b.published_at).total_seconds()) / 3600.0
        if gap > thresholds.max_gap_hours:
            return False

    ents_a, ents_b = set(a.entities), set(b.entities)
    if ents_a and ents_b and not (ents_a & ents_b):
        return False

    return (
        scorer(a, b) >= thresholds.body
        or title_similarity(a.title, b.title) >= thresholds.title
    )


#: Strongest first. A regulatory filing outranks a trade write-up of it,
#: and anything outranks an aggregator's copy.
_SOURCE_RANK = {
    SourceType.REGULATORY_FILING: 0,
    SourceType.ORIGINAL_REPORTING: 1,
    SourceType.TRADE_PUBLICATION: 2,
    SourceType.COMPANY_PRESS_RELEASE: 3,
    SourceType.UNKNOWN: 4,
    SourceType.AGGREGATOR: 5,
}


def cluster(
    candidates: Sequence[Candidate],
    scorer: Scorer = shingle_scorer,
    thresholds: Thresholds = Thresholds(),
) -> list[list[Candidate]]:
    """Greedy single-link clustering.

    Single-link is the right shape here: syndication forms chains (wire copy
    -> trade site -> aggregator) where the ends may not resemble each other
    even though every adjacent pair does. Candidate counts per sweep are in
    the hundreds, so the quadratic comparison is not worth optimising.
    """
    groups: list[list[Candidate]] = []
    for cand in candidates:
        for group in groups:
            if any(same_development(cand, other, scorer, thresholds) for other in group):
                group.append(cand)
                break
        else:
            groups.append([cand])
    return groups


def pick_canonical(group: Iterable[Candidate]) -> tuple[Candidate, list[Candidate]]:
    """Return (canonical, duplicates) for one cluster.

    Ordering: strongest source type, then earliest publication (first story),
    then the fullest text as a tie-break, since a truncated syndicated stub
    is less useful than the full article.
    """
    members = list(group)
    if not members:
        raise ValueError("cannot pick a canonical member of an empty cluster")

    def key(c: Candidate):
        # The trailing URL makes the ordering *total*. Without it, two copies
        # with the same source type, timestamp and length would be separated
        # only by input order, and the report would silently cite whichever
        # copy a search provider happened to rank first that morning.
        return (
            _SOURCE_RANK.get(c.source_type, 9),
            c.published_at.timestamp() if c.published_at else float("inf"),
            -len(c.body or c.snippet),
            c.url,
        )

    ranked = sorted(members, key=key)
    return ranked[0], ranked[1:]


def deduplicate(
    candidates: Sequence[Candidate],
    scorer: Scorer = shingle_scorer,
    thresholds: Thresholds = Thresholds(),
) -> list[tuple[Candidate, list[Candidate]]]:
    """Cluster, then reduce each cluster to one representative."""
    return [pick_canonical(g) for g in cluster(candidates, scorer, thresholds)]
