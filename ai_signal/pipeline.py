"""Orchestration: plan -> fan out -> fuse -> open -> gate -> dedupe -> report.

The stage order is load-bearing and differs from a consumer answer engine in
one respect. Perplexity-style pipelines rerank and then cut to a top-k before
generation, because the product is one answer. Here the cut happens at the
*verification* gate instead, and ranking only decides the order in which
candidates are reviewed. That follows the recall-first logic of
technology-assisted review (Cormack & Grossman, SIGIR 2015): in a monitoring
task the expensive error is a missed development, not a wasted fetch.

Deduplication runs *after* the gate rather than before it, which costs some
fetches but is deliberate: choosing a canonical source means comparing
verified publication times, and before the gate those times are exactly the
thing we do not yet trust.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence
from urllib.parse import urlparse

from . import dedup, entities, rank, temporal, verify
from .metrics import MetricsCollector
from .model import Bucket, Candidate, Query, Rejection, SourceType, Verdict
from .providers import PageFetcher, SearchProvider, classify_source


@dataclass
class WatchTerm:
    """One row of the shared watchlist."""

    name: str
    bucket: Bucket
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True
    #: NSE ticker, for companies that are listed. Set only on those: it is
    #: what lets the exchange-filings provider watch a company directly
    #: instead of waiting for trade press to notice its disclosures.
    nse_symbol: str = ""


@dataclass
class SweepConfig:
    #: The reporting window. This is the ONLY thing that decides which
    #: verified items appear in the report -- strict and non-negotiable,
    #: enforced solely by `verify.gate` against each page's own verified
    #: publication time. Changing this changes report *content*.
    window_hours: int = 24
    #: How far back search queries ask providers to look. Purely a recall
    #: aid against provider indexing lag (see `Query.lookback_hours`);
    #: wider than `window_hours` on purpose. Changing this changes what
    #: gets *considered*, never what gets *included* -- a candidate that
    #: is 50 hours old is still rejected by the gate regardless of how
    #: wide the search net was.
    lookback_hours: int = 72
    tz_name: str = "Asia/Kolkata"
    #: Results requested per query per provider.
    per_query: int = 28
    #: Cap on page fetches, the only expensive step. Candidates are opened
    #: in fused-rank order, so a cap degrades recall gracefully rather than
    #: arbitrarily.
    fetch_budget: int = 180
    #: Bounded, not unlimited: discovery and page-fetching are pure network
    #: waits, so running them concurrently is a large, safe speedup, but an
    #: unbounded pool would turn one sweep into a burst of ~90+ simultaneous
    #: requests at a single host we have already watched rate-limit us
    #: (Google News, 503). 8 is deliberately conservative -- enough to
    #: collapse "N requests, one after another" into a handful of waves,
    #: without looking like a denial-of-service attempt to anyone we query.
    max_workers: int = 8
    company_context_query: str = ""


#: Smallest and largest reporting window a sweep will honour. The floor
#: guards against a zero/negative value reaching `temporal.window_ending_at`;
#: the ceiling matches the archive's own 30-day retention (deploy/lifecycle.json)
#: -- a window wider than how long reports are even kept would be reporting
#: on history the bucket has already deleted.
MIN_WINDOW_HOURS = 1
MAX_WINDOW_HOURS = 24 * 30


def lookback_for(window_hours: int) -> int:
    """The recall margin `lookback_hours` needs for a given reporting window.

    The two were only ever tested together at their defaults (24h window,
    72h lookback -- a fixed 48h margin). Widening `window_hours` alone
    while leaving `lookback_hours` fixed at 72 would silently cap recall at
    3 days no matter how wide a window was asked for: a "1 week" report
    would always come back empty for days 4-7, not because nothing
    happened then, but because no provider was ever asked to look that far
    back. Preserves the same 48h margin at any window size rather than a
    fixed constant, so indexing lag is covered proportionally either way.
    """
    return window_hours + 48


@dataclass
class SweepResult:
    window: temporal.Window
    checked_at: datetime
    included: dict[Bucket, list[Verdict]] = field(default_factory=dict)
    rejected: list[Verdict] = field(default_factory=list)
    #: query text -> number of candidates it contributed
    coverage: dict[str, int] = field(default_factory=dict)
    #: Providers and fetchers that errored, so limitations can be stated
    #: rather than silently shrinking the result.
    limitations: list[str] = field(default_factory=list)
    #: Wall-clock network metrics per phase (discovery, fetch): request
    #: counts, success/failure, 429/503 counts, p95 latency. Diagnostic
    #: only -- never read by `record.canonical_report`, so it carries no
    #: weight in the determinism guarantee that governs report *content*.
    metrics: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {b.value: len(v) for b, v in self.included.items()}

    def rejections_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for v in self.rejected:
            if v.rejection:
                out[v.rejection.value] += 1
        return dict(out)


def plan(terms: Sequence[WatchTerm], config: SweepConfig = SweepConfig()) -> list[Query]:
    """Build the query set for a sweep.

    Every query is stamped with `config.lookback_hours` -- a hint a live
    provider can use to widen its own freshness filter. It never touches
    `config.window_hours`, which stays the gate's business alone.
    """
    queries: list[Query] = []
    live = [t for t in terms if t.enabled]

    for bucket in (Bucket.CLIENTS, Bucket.COMPETITORS):
        members = [t for t in live if t.bucket is bucket]
        for term in members:
            queries.extend(rank.plan_entity_queries(term.name, term.aliases, bucket))
        queries.extend(
            rank.plan_group_queries([t.name for t in members], bucket)
        )

    # Topic rows already *are* queries; they are passed through verbatim so
    # that what the team edits is exactly what gets searched.
    for term in (t for t in live if t.bucket is Bucket.TOPICS):
        queries.append(Query(term.name, Bucket.TOPICS))
        for variant in term.aliases:
            queries.append(
                Query(variant, Bucket.TOPICS, expanded_from=term.name)
            )

    # Stamped in one place so every query -- entity, group, and topic alike
    # -- carries the same search-recall hint, independent of how it was built.
    return [dataclasses.replace(q, lookback_hours=config.lookback_hours) for q in queries]


def run(
    terms: Sequence[WatchTerm],
    providers: Sequence[SearchProvider],
    fetcher: PageFetcher,
    now: datetime,
    config: SweepConfig = SweepConfig(),
) -> SweepResult:
    """Execute one sweep."""
    window = temporal.window_ending_at(now, config.window_hours, config.tz_name)
    result = SweepResult(window=window, checked_at=now)
    metrics = MetricsCollector()

    queries = [
        dataclasses.replace(
            query,
            retrieval_start=now - timedelta(hours=config.lookback_hours),
            retrieval_end=now,
        )
        for query in plan(terms, config)
    ]
    profiles_by_bucket = {
        bucket: entities.default_profiles(
            [t.name for t in terms if t.enabled and t.bucket is bucket]
        )
        for bucket in (Bucket.CLIENTS, Bucket.COMPETITORS)
    }

    # --- fan out ------------------------------------------------------
    # Every (query, provider) pair is an independent network call, so they
    # run in a bounded thread pool instead of one after another -- this is
    # the whole reason a sweep with Google News enabled used to take tens
    # of minutes instead of under two. What must NOT change is which
    # candidates come out, or their rank: `ThreadPoolExecutor.map` returns
    # results in the SAME order its inputs were given, regardless of which
    # thread finishes first, so `work` is built once, up front, in the
    # exact order the old sequential `for query: for provider` loop would
    # have visited, and everything below consumes results in that fixed
    # order -- only the *waiting* happens out of order, never the
    # bookkeeping. Each worker only performs the call and returns its
    # outcome; `per_provider`, `hits`, and `result.limitations` are only
    # ever touched back on this thread, so none of them need a lock.
    work = [(query, provider) for query in queries for provider in providers]

    def _search_one(item: tuple[Query, SearchProvider]):
        query, provider = item
        t0 = time.monotonic()
        try:
            found = provider.search(query, config.per_query)
            return query, provider, found, None, time.monotonic() - t0
        except Exception as exc:  # a dead provider must not kill the sweep
            return query, provider, [], exc, time.monotonic() - t0

    per_provider: dict[str, list[Candidate]] = defaultdict(list)
    hits: dict[str, int] = defaultdict(int)
    if work:
        with metrics.phase("discovery"):
            with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
                for query, provider, found, exc, elapsed in pool.map(_search_one, work):
                    metrics.record(
                        "discovery",
                        latency_ms=elapsed * 1000,
                        ok=exc is None,
                        error=str(exc) if exc else None,
                    )
                    if exc is not None:
                        msg = f"provider {provider.name!r} failed on {query.text!r}: {exc}"
                        if msg not in result.limitations:
                            result.limitations.append(msg)
                        continue
                    per_provider[provider.name].extend(found)
                    hits[query.text] += len(found)
    result.coverage = dict(hits)

    candidates = rank.fuse(rank.merge_by_url(per_provider))

    # --- open pages and gate -------------------------------------------
    # Structural rejections are resolved first and for free -- no network
    # call, so they neither need the thread pool nor count against
    # `fetch_budget`, exactly as before. Only what survives that filter is
    # eligible to be fetched, and only the first `fetch_budget` of those,
    # in fused-rank order -- identical to what the old sequential loop did,
    # just computed as two passes instead of one interleaved one, so the
    # budget still degrades recall gracefully rather than arbitrarily.
    verdicts: list[Verdict] = []
    fetchable: list[Candidate] = []
    for cand in candidates:
        structural = verify.structural_rejection(cand.url)
        if structural is not None:
            reason, why = structural
            verdicts.append(Verdict(cand, False, reason, why))
            continue
        fetchable.append(cand)

    to_fetch = fetchable[: config.fetch_budget]
    if len(fetchable) > config.fetch_budget:
        result.limitations.append(
            f"fetch budget of {config.fetch_budget} reached; "
            f"{len(fetchable) - config.fetch_budget} lower-ranked candidates were not opened"
        )

    def _fetch_one(cand: Candidate):
        t0 = time.monotonic()
        try:
            page = fetcher.fetch(cand.url)
            return cand, page, None, time.monotonic() - t0
        except Exception as exc:  # a fetcher must not take the whole sweep down
            return cand, None, exc, time.monotonic() - t0

    fetched_pairs: list[tuple[Candidate, object]] = []
    if to_fetch:
        with metrics.phase("fetch"):
            with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
                for cand, page, exc, elapsed in pool.map(_fetch_one, to_fetch):
                    ok = exc is None and page is not None and page.ok
                    metrics.record(
                        "fetch",
                        latency_ms=elapsed * 1000,
                        ok=ok,
                        error=(str(exc) if exc else (None if page is None else page.error)),
                    )
                    fetched_pairs.append((cand, page if exc is None else None))

    for cand, page in fetched_pairs:
        if page is None or not page.ok:
            verdicts.append(
                Verdict(
                    cand,
                    False,
                    Rejection.NOT_RETRIEVABLE,
                    (page.error if page is not None else None) or "page could not be opened",
                )
            )
            continue

        if page.resolved_url:
            cand.url = page.resolved_url
        cand.body = page.body
        if page.source_type is not SourceType.UNKNOWN:
            cand.source_type = page.source_type
            cand.source_name = page.source_name
        else:
            cand.source_type, cand.source_name = classify_source(
                urlparse(cand.url).netloc
            )

        finding = temporal.resolve_publication(page.published, page.modified)
        if finding is not None:
            cand.published_at = finding.moment
            cand.date_only = finding.date_only
            cand.date_provenance = finding.provenance
            if finding.modified_only:
                verdicts.append(
                    Verdict(
                        cand,
                        False,
                        Rejection.DATE_UNVERIFIABLE,
                        "only a modified timestamp was present, which does not "
                        "establish original publication",
                    )
                )
                continue
        else:
            cand.published_at = None

        verdicts.append(
            verify.gate(
                cand,
                window,
                now,
                profiles_by_bucket.get(cand.bucket, ()),
                company_context_query=config.company_context_query,
                publisher_summary=page.summary,
            )
        )

    # --- dedupe survivors -------------------------------------------------
    admitted = [v for v in verdicts if v.included]
    result.rejected = [v for v in verdicts if not v.included]

    by_bucket: dict[Bucket, list[Verdict]] = defaultdict(list)
    for v in admitted:
        by_bucket[v.candidate.bucket].append(v)

    for bucket, group in by_bucket.items():
        clusters = dedup.deduplicate([v.candidate for v in group])
        keep: list[Verdict] = []
        verdict_of = {v.candidate.url: v for v in group}
        for canonical, dupes in clusters:
            chosen = verdict_of[canonical.url]
            chosen.duplicates = [d.url for d in dupes]
            keep.append(chosen)
            for d in dupes:
                dropped = verdict_of[d.url]
                dropped.included = False
                dropped.rejection = Rejection.DUPLICATE
                dropped.reason = f"same development as {canonical.url}"
                result.rejected.append(dropped)
        keep.sort(
            key=lambda v: (
                v.candidate.date_only,  # timestamped items first
                -(v.candidate.published_at.timestamp() if v.candidate.published_at else 0),
                v.candidate.url,  # total order, so row order never varies
            )
        )
        result.included[bucket] = keep

    for bucket in (Bucket.CLIENTS, Bucket.COMPETITORS, Bucket.TOPICS):
        result.included.setdefault(bucket, [])
    result.metrics = metrics.summary()
    return result
