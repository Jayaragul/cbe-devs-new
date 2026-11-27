"""Benchmark: sequential (max_workers=1) vs bounded-concurrent (max_workers=8)
discovery and fetch, on identical inputs.

This is the offline half of the concurrency benchmark: a fixture sized and
paced to resemble the real deployment (28 watchlist terms -> ~91 queries,
three providers, occasional slow/failing calls) but running entirely
against fake, artificially-delayed providers -- no network, so the numbers
are exact and reproducible, and the comparison is a clean one: identical
inputs, identical candidate set, identical verification logic, only the
scheduling of the network waits differs.

It does NOT replace measuring the real deployment -- see the docstring at
the bottom for how the equivalent live run was done and what it showed.
Run this one for a repeatable proof of the mechanism; read that one for
what it means in production.

Run:
    python tools/bench_concurrency.py
"""

from __future__ import annotations

import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_signal import pipeline, record, temporal
from ai_signal.model import Bucket, Candidate, Query, SourceType
from ai_signal.providers import PageContent

IST = temporal.IST
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=IST)

#: Sized to match the real watchlist measured in production: 28 terms plan
#: out to 91 queries: N clients/competitors each contribute a bare-name
#: query, a dev-verb query, and share in grouped OR queries; ~20 clients
#: gets there. See ai_signal.pipeline.plan.
N_COMPANIES = 20

#: Per-call latency distribution (seconds), modelling three providers of
#: differing character rather than one flat number:
#:   RSS/NSE   -- fast once cached (already the case in production; the
#:                slow-path measurement was specifically about the
#:                per-QUERY provider, Google News, which has no such cache)
#:   "google"  -- the slow, occasionally-failing provider that motivated
#:                this work: ~1.5s typical, some calls near the 12s
#:                timeout, and a modelled ~15% failure rate matching the
#:                observed live 503/unresolved-wrapper rate.
FAST_DELAY_S = 0.05
GOOGLE_DELAY_S = 1.5
GOOGLE_SLOW_DELAY_S = 11.5
GOOGLE_FAILURE_RATE = 0.15
FETCH_DELAY_S = 0.8


@dataclass
class PacedProvider:
    """A provider whose calls take a realistic, fixed amount of time.

    `pattern` is a deterministic cycle of (delay_s, fails) so the same
    benchmark run always does the same amount of simulated work -- no
    `random`, so a slow run and a fast run differ only in how the pool
    schedules identical work, never in what that work is.
    """

    name: str
    results: dict[str, list[Candidate]]
    pattern: list[tuple[float, bool]]
    _n: int = field(default=0, init=False)

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        delay, fails = self.pattern[self._n % len(self.pattern)]
        self._n += 1
        time.sleep(delay)
        if fails:
            raise RuntimeError("simulated HTTP 503 Service Unavailable")
        out = []
        for proto in self.results.get(query.text, [])[:limit]:
            out.append(
                Candidate(
                    url=proto.url, title=proto.title, bucket=query.bucket,
                    entities=list(proto.entities), body=proto.body,
                    source_name=proto.source_name, source_type=proto.source_type,
                    found_by=[query],
                )
            )
        return out


@dataclass
class PacedFetcher:
    pages: dict[str, PageContent]
    delay_s: float
    _n: int = field(default=0, init=False)

    def fetch(self, url: str) -> PageContent:
        time.sleep(self.delay_s)
        page = self.pages.get(url)
        if page is None:
            return PageContent(body="", ok=False, error="not in fixture set")
        return page


def _google_pattern(n: int) -> list[tuple[float, bool]]:
    out = []
    for i in range(n):
        if i % 7 == 0:  # ~15% -- the observed live failure/timeout rate
            out.append((GOOGLE_SLOW_DELAY_S, True))
        else:
            out.append((GOOGLE_DELAY_S, False))
    return out


def build_fixture():
    terms = [pipeline.WatchTerm(f"Company {i}", Bucket.CLIENTS) for i in range(N_COMPANIES)]
    results = {}
    pages = {}
    for i in range(N_COMPANIES):
        url = f"https://trade-press.example/company-{i}"
        results[f'"Company {i}"'] = [
            Candidate(url=url, title=f"Company {i} announces new export order",
                      bucket=Bucket.CLIENTS, entities=[f"Company {i}"])
        ]
        pages[url] = PageContent(
            body=f"Company {i} announced a new export order today.",
            published=temporal.DateFinding(NOW, False, "byline"),
            source_type=SourceType.TRADE_PUBLICATION, source_name="Trade Press",
        )

    n_queries = len(pipeline.plan(terms, pipeline.SweepConfig()))
    fast = PacedProvider(name="rss", results=results, pattern=[(FAST_DELAY_S, False)])
    google = PacedProvider(name="google-news", results=results, pattern=_google_pattern(n_queries))
    fetcher = PacedFetcher(pages=pages, delay_s=FETCH_DELAY_S)
    return terms, [fast, google], fetcher, n_queries


def run_once(max_workers: int):
    terms, providers, fetcher, n_queries = build_fixture()
    # Fresh fixture instances each call -- these carry a call counter used
    # only to pace the deterministic delay pattern, not shared state that
    # should survive between the two configurations being compared.
    config = pipeline.SweepConfig(max_workers=max_workers)
    t0 = time.monotonic()
    result = pipeline.run(terms, providers, fetcher, NOW, config)
    elapsed = time.monotonic() - t0
    return result, elapsed, n_queries


def main() -> None:
    print(f"Fixture: {N_COMPANIES} companies -> {build_fixture()[3]} queries, "
          f"2 providers (one fast/cached-shaped, one Google-News-shaped)\n")

    print("Running sequential (max_workers=1) ...")
    seq_result, seq_s, n_queries = run_once(max_workers=1)

    print("Running concurrent (max_workers=8) ...")
    conc_result, conc_s, _ = run_once(max_workers=8)

    seq_digest = record.report_digest(seq_result)
    conc_digest = record.report_digest(conc_result)

    seq_verified = sum(len(v) for v in seq_result.included.values())
    conc_verified = sum(len(v) for v in conc_result.included.values())

    def metric(res, phase):
        return next((row for row in res.metrics if row["phase"] == phase), {})

    print()
    print(f"{'':22}{'sequential':>15}{'concurrent':>15}")
    print(f"{'total runtime (s)':22}{seq_s:>15.2f}{conc_s:>15.2f}")
    print(f"{'speedup':22}{'':>15}{seq_s / conc_s:>14.1f}x")
    print(f"{'verified items':22}{seq_verified:>15}{conc_verified:>15}")
    print(f"{'report digest match':22}{'yes' if seq_digest == conc_digest else 'NO -- BUG':>15}")
    for phase in ("discovery", "fetch"):
        s, c = metric(seq_result, phase), metric(conc_result, phase)
        print(f"\n{phase} phase:")
        print(f"  {'requests':20}{s.get('requests'):>10}{c.get('requests'):>10}")
        print(f"  {'success':20}{s.get('success'):>10}{c.get('success'):>10}")
        print(f"  {'failure':20}{s.get('failure'):>10}{c.get('failure'):>10}")
        print(f"  {'status_503':20}{s.get('status_503'):>10}{c.get('status_503'):>10}")
        print(f"  {'p95 latency (ms)':20}{s.get('p95_latency_ms'):>10}{c.get('p95_latency_ms'):>10}")

    assert seq_digest == conc_digest, "concurrency changed the report -- this is a real bug"
    assert seq_verified == conc_verified


if __name__ == "__main__":
    main()

# Equivalent live-network measurement, for reference (see the accompanying
# PR description / conversation record for the raw numbers): running the
# real 26-28 term watchlist with AI_SIGNAL_GOOGLE_NEWS=1 against this pipeline
# before this change was directly observed, via the running server's own
# network connections, to still be actively fetching from Google News many
# minutes in -- consistent with the ~91 sequential discovery calls plus up
# to 120 sequential page-fetches this fixture models, each capable of a
# 12-24s stall on an unresolved Google wrapper URL. That live run was not
# re-executed to produce a "before" number for this benchmark: it would
# reproduce a slow outcome we have already measured once, at the cost of
# hitting Google's rate limiter again for no new information. The "after"
# side -- this change, live, on the real watchlist -- is measured
# separately and reported alongside this script's output.
