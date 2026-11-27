"""Deterministic boolean-query matching against document text.

Caught live, against a real article, during the first full-watchlist RSS
sweep (2026-09-12): the topic query
    ("textile industry" OR ...) AND ("AI" OR "automation" OR ...)
matched "Indian school students tackle India's 7.7mn tonne textile waste
challenge" -- an article that never mentions AI, automation, machine
learning, or robotics anywhere in its body (the only occurrence of "AI" on
the page is inside an unrelated author-bio sentence, not the article
itself). The keyword matcher then in use (flat bag-of-words token overlap,
requiring only *some* shared words) was satisfied by "textile" + "industry"
alone and never checked the AND clause at all -- it had no concept of the
query's boolean structure, only its vocabulary.

This module is not a general boolean-query parser. It is exactly general
enough for the shapes this project's watchlist actually produces (see
`rank.py` and the original monitoring brief), all of which are one of:

    "Name"                                   -- a bare phrase
    "Name" (word OR word OR word)            -- a phrase, implicitly
                                                 AND'ed with a parenthetical
                                                 OR-group (rank.py's
                                                 development-verb expansion)
    (P1 OR P2) AND (W1 OR W2 OR W3)           -- explicit AND between two
                                                 parenthetical OR-groups
                                                 (the brief's topic queries)
    "Name1" OR "Name2" OR "Name3"             -- a flat OR-chain, no parens
                                                 (rank.py's grouped queries)

Every keyword-matching provider (`live/rss_provider.py`, `live/demo_provider.py`)
uses this so that a query means the same thing everywhere it is evaluated,
instead of each provider inventing its own bag-of-words approximation.
"""

from __future__ import annotations

import re

#: Matches one parenthesized group with no nested parens -- sufficient for
#: every query shape this project produces, none of which nest parens.
_PAREN_GROUP = re.compile(r"\(([^()]*)\)")
_OR_SPLIT = re.compile(r"\bOR\b", re.IGNORECASE)
_AND_SPLIT = re.compile(r"\bAND\b", re.IGNORECASE)


def _alternatives(segment: str) -> list[str]:
    """Split one OR-joined segment into its bare alternatives, quotes
    stripped, blanks dropped."""
    out = []
    for piece in _OR_SPLIT.split(segment):
        piece = piece.strip().strip('"').strip()
        if piece:
            out.append(piece)
    return out


def parse_query(text: str) -> list[list[str]]:
    """Parse a watchlist query string into AND-clauses of OR-alternatives:
    `[[alt, alt, ...], [alt, ...], ...]` -- every clause must have at least
    one alternative present in a document for the query to match it.

    Never raises: a query shape this cannot make sense of degrades to a
    single AND-clause holding the whole (quote-stripped) string as its one
    alternative, which is still a sane, if blunt, match -- exactly the
    behaviour bag-of-words matching gave everywhere, but never worse.
    """
    remaining = text
    clauses: list[list[str]] = []

    def _consume_parens(_match: re.Match) -> str:
        alts = _alternatives(_match.group(1))
        if alts:
            clauses.append(alts)
        return " "

    remaining = _PAREN_GROUP.sub(_consume_parens, remaining)

    # Whatever is left after removing every parenthesized group: either
    # nothing (the query was fully parenthesized, e.g. the topic-query
    # shape), one bare phrase (the entity-query shape's leading name), or a
    # flat OR-chain of phrases with no parens at all (the grouped-query
    # shape). The literal word "AND" outside parens never joins two
    # *different* requirements in any shape this project produces, so it is
    # simply stripped rather than treated as a second split axis.
    remaining = _AND_SPLIT.sub(" ", remaining)
    leftover_alts = _alternatives(remaining)
    if leftover_alts:
        clauses.append(leftover_alts)

    if not clauses:
        # Nothing recognisable at all (e.g. an empty or malformed query) --
        # fall back to the raw text as a single required alternative.
        stripped = text.strip().strip('"').strip()
        if stripped:
            clauses.append([stripped])

    return clauses


def _contains(text_lower: str, alternative: str) -> bool:
    """Whole-word/whole-phrase containment, case-insensitive. Deliberately
    NOT a plain substring check: "AI" as a bare substring matches inside
    "again", "maintain", "Spain" -- this is exactly the false-positive risk
    a short, common abbreviation creates, and the reason this function
    exists rather than `alternative.lower() in text_lower`."""
    pattern = r"(?<!\w)" + re.escape(alternative.lower()) + r"(?!\w)"
    return re.search(pattern, text_lower) is not None


def matches(text: str, query_text: str) -> bool:
    """Does `text` satisfy every AND-clause of `query_text`?"""
    text_lower = (text or "").lower()
    clauses = parse_query(query_text)
    if not clauses:
        return False
    return all(
        any(_contains(text_lower, alt) for alt in alternatives)
        for alternatives in clauses
    )
