"""Fan-out and fusion.

Perplexity-style engines cast a deliberately wide net and then narrow it in
stages: hybrid lexical + dense retrieval for recall, then cross-encoder
reranking for precision. This module owns the fusion step between the two.

Reciprocal Rank Fusion is the merge. Cormack, Clarke & Büttcher, "Reciprocal
Rank Fusion outperforms Condorcet and individual Rank Learning Methods"
(SIGIR 2009), showed the one-line rank-only combiner beating Condorcet Fuse,
CombMNZ and every individual learning-to-rank method they tested. Rank-only
is what we need: search APIs return positions, not calibrated scores, so
score-based fusion would be comparing incommensurable numbers.

One deliberate departure from Perplexity's design. A consumer engine ranks
for precision at position one. This sweep is a *recall-first* task closer to
technology-assisted review, where missing a relevant document is the
expensive error (Cormack & Grossman, "Multi-Faceted Recall of Continuous
Active Learning for Technology-Assisted Review", SIGIR 2015,
doi:10.1145/2766462.2767771). So fusion here feeds a verification gate rather
than a top-k cut: ranking decides review *order*, not membership.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from .model import Bucket, Candidate, Query

#: Cormack et al.'s constant. It damps the influence of top ranks so a
#: single provider cannot dominate the fused order.
RRF_K = 60


def rrf_score(ranks: Mapping[str, int], k: int = RRF_K) -> float:
    """Reciprocal Rank Fusion over one document's per-provider ranks.

    `ranks` maps provider name -> 1-based position. Providers that did not
    return the document simply omit it, which is the whole point: RRF needs
    no score normalisation and no imputation for missing systems.
    """
    return sum(1.0 / (k + rank) for rank in ranks.values() if rank >= 1)


def fuse(candidates: Iterable[Candidate], k: int = RRF_K) -> list[Candidate]:
    """Score and order candidates by RRF, highest first."""
    ranked = list(candidates)
    for cand in ranked:
        cand.fused_score = rrf_score(cand.ranks, k)
    ranked.sort(key=lambda c: (-c.fused_score, c.url))
    return ranked


def merge_by_url(results: Mapping[str, Sequence[Candidate]]) -> list[Candidate]:
    """Collapse per-provider result lists into one candidate per URL.

    `results` maps provider name -> that provider's ranked list. Positions
    are read from list order, and the per-provider ranks are accumulated on
    the surviving candidate so `fuse` can combine them.
    """
    merged: dict[str, Candidate] = {}
    for provider, items in results.items():
        for position, cand in enumerate(items, start=1):
            existing = merged.get(cand.url)
            if existing is None:
                cand.ranks[provider] = position
                merged[cand.url] = cand
                continue
            existing.ranks[provider] = min(
                position, existing.ranks.get(provider, position)
            )
            # Keep the richest metadata seen across providers.
            if len(cand.snippet) > len(existing.snippet):
                existing.snippet = cand.snippet
            if cand.body and not existing.body:
                existing.body = cand.body
            for q in cand.found_by:
                if q not in existing.found_by:
                    existing.found_by.append(q)
            for e in cand.entities:
                if e not in existing.entities:
                    existing.entities.append(e)
    return list(merged.values())


# --------------------------------------------------------------------------
# Query planning
# --------------------------------------------------------------------------


def plan_entity_queries(name: str, aliases: Sequence[str], bucket: Bucket) -> list[Query]:
    """Queries for one watched company.

    The brief asks for each entity individually *and* in groups. Individual
    queries protect recall for small private firms that a grouped OR query
    would let a larger name crowd out; the grouped query is what catches a
    development naming several of them at once.
    """
    forms = [name, *aliases]
    queries = [Query(f'"{f}"', bucket, entity=name) for f in forms]
    # Pair the company with words that signal an actual development, which
    # suppresses directory and stock-quote pages without excluding them by
    # domain (a real filing can live on a market-data host).
    queries.append(
        Query(
            f'"{name}" (announces OR launches OR expansion OR order OR results '
            f"OR acquisition OR partnership OR filing)",
            bucket,
            entity=name,
            expanded_from=name,
        )
    )
    return queries


def plan_group_queries(
    names: Sequence[str], bucket: Bucket, per_group: int = 6
) -> list[Query]:
    """Grouped OR queries, chunked to stay inside provider query limits."""
    out: list[Query] = []
    for i in range(0, len(names), per_group):
        chunk = names[i : i + per_group]
        out.append(Query(" OR ".join(f'"{n}"' for n in chunk), bucket))
    return out


def coverage(queries: Sequence[Query], hits: Mapping[str, int]) -> dict[str, int]:
    """How many candidates each query contributed.

    Recorded so a sweep that finds nothing can demonstrate it actually ran
    every planned query, rather than silently skipping some.
    """
    counts: dict[str, int] = defaultdict(int)
    for q in queries:
        counts[q.text] = hits.get(q.text, 0)
    return dict(counts)
