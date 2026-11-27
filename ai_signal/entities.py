"""Deciding whether a hit is about *our* company.

"WFX", "Best Corporation" and "Poppys" are all overloaded strings. A sweep
that trusts a string match will report a Kenyan logistics firm as a client
development. The brief calls this out explicitly: confirm the result concerns
the intended textile, apparel or manufacturing-technology business.

The structure follows BLINK -- Wu, Petroni, Josifoski, Riedel & Zettlemoyer,
"Scalable Zero-shot Entity Linking with Dense Entity Retrieval" (EMNLP 2020,
aclanthology 2020.emnlp-main.519): cheap candidate generation over surface
forms, then a more expensive check of the mention *in its context* against a
description of the entity. Here the second stage is lexical rather than a
cross-encoder, because the candidate set is a fixed watchlist of a few dozen
firms rather than a knowledge base of millions, and because a lexical rule is
auditable -- a reviewer can see exactly which context term admitted a hit.
`context_score` is the seam where a cross-encoder replaces the rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Terms that establish a document is in this industry at all. Used as the
#: corroborating context for names that are ambiguous on their own.
DOMAIN_TERMS = frozenset(
    """textile textiles apparel garment garments knitwear knitted hosiery
    spinning mill mills yarn fabric fabrics weaving dyeing sewing rmg
    readymade clothing exporter exporters sourcing tirupur tiruppur coimbatore
    erp mes plm cutroom shopfloor sam gsd""".split()
)


@dataclass
class EntityProfile:
    """A watchlist entity plus what it takes to confirm a match."""

    name: str
    aliases: list[str] = field(default_factory=list)
    #: When True, a surface match alone is never enough -- the document must
    #: also carry industry context. Set this for short or generic names.
    ambiguous: bool = False
    #: Extra terms that specifically identify this entity (a founder, a home
    #: city, a brand, a ticker). Any one of these confirms a match outright.
    identifiers: list[str] = field(default_factory=list)
    #: Terms indicating a *different* organisation with a similar name.
    exclude: list[str] = field(default_factory=list)

    def surface_forms(self) -> list[str]:
        return [self.name, *self.aliases]


def _pattern(form: str) -> re.Pattern:
    """Word-boundary matcher tolerant of punctuation inside a name.

    "K.P.R. Mill", "KPR Mill" and "K P R Mill" must all match one pattern,
    so internal punctuation and spacing are treated as interchangeable
    separators rather than literal characters.
    """
    parts = [re.escape(p) for p in re.split(r"[^\w]+", form) if p]
    if not parts:
        return re.compile(r"(?!x)x")  # never matches
    body = r"[^\w]{0,3}".join(parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def surface_match(text: str, profile: EntityProfile) -> Optional[str]:
    """The first surface form of `profile` present in `text`, if any."""
    for form in profile.surface_forms():
        if _pattern(form).search(text or ""):
            return form
    return None


def context_score(text: str) -> int:
    """How many distinct industry terms the document carries."""
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    return len(words & DOMAIN_TERMS)


@dataclass(frozen=True)
class EntityDecision:
    matched: tuple[str, ...]
    #: Surface forms that matched but were rejected, with the reason.
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.matched)


def resolve(
    text: str,
    profiles: Sequence[EntityProfile],
    min_context: int = 2,
) -> EntityDecision:
    """Which watchlist entities this document is genuinely about.

    A match is admitted when the surface form appears and at least one of:
    the entity is unambiguous, a specific identifier is present, or the
    document carries `min_context` industry terms. Exclusion terms veto a
    match outright.
    """
    haystack = text or ""
    lowered = haystack.lower()
    ctx = context_score(haystack)
    matched: list[str] = []
    rejected: list[tuple[str, str]] = []

    for profile in profiles:
        form = surface_match(haystack, profile)
        if form is None:
            continue

        hit_exclusion = next(
            (x for x in profile.exclude if x.lower() in lowered), None
        )
        if hit_exclusion:
            rejected.append(
                (profile.name, f"page mentions {hit_exclusion!r}, a different entity")
            )
            continue

        has_identifier = any(i.lower() in lowered for i in profile.identifiers)
        if has_identifier or not profile.ambiguous or ctx >= min_context:
            matched.append(profile.name)
        else:
            rejected.append(
                (
                    profile.name,
                    f"{form!r} appears but the page carries only {ctx} industry "
                    f"term(s); an ambiguous name needs {min_context}",
                )
            )

    return EntityDecision(tuple(matched), tuple(rejected))


# --------------------------------------------------------------------------
# Profiles for names that need help resolving ambiguous mentions.
# Everything else is distinctive enough that a surface match is safe.
# --------------------------------------------------------------------------

RISKY_DEFAULTS: dict[str, dict] = {
    "Meta AI": {
        "aliases": ["Meta"],
        "ambiguous": True,
        "identifiers": ["meta ai", "llama", "meta platforms", "facebook"],
        "exclude": ["meta materials inc"],
    },
    "WFX": {
        "aliases": ["World Fashion Exchange"],
        "ambiguous": True,
        "identifiers": ["world fashion exchange", "wfx cloud", "wfx erp"],
        "exclude": ["wavefront", "weather", "wfx.tv"],
    },
    "Best Corporation": {
        "aliases": ["Best Corporation (P) Limited", "BCPL"],
        "ambiguous": True,
        "identifiers": ["tirupur", "tiruppur", "knitted"],
    },
    "Poppys Group": {
        "aliases": ["Poppys Knitwear", "Poppys"],
        "ambiguous": True,
        "identifiers": ["knitwear", "tirupur", "tiruppur"],
        "exclude": ["poppys hotel"],
    },
    "Bannari Amman": {
        "aliases": ["Bannari Amman Spinning Mills", "Bannari Amman Sugars"],
        "ambiguous": True,
        # Two distinct listed companies share this name; the sugar business
        # is not the textile one, so a hit must say which it is.
        "identifiers": ["spinning", "sugars", "banarisug", "textile"],
    },
    "Datatex": {
        "aliases": ["Data Tex", "Datatex NOW"],
        "ambiguous": False,
        "identifiers": ["textile", "erp"],
    },
    "Solvei8": {"aliases": ["solvei8"], "ambiguous": False},
}


def default_profiles(names: Sequence[str]) -> list[EntityProfile]:
    """Build profiles for a watchlist, applying the risky-name overrides."""
    out: list[EntityProfile] = []
    for name in names:
        spec = RISKY_DEFAULTS.get(name)
        if spec is None:
            out.append(EntityProfile(name=name))
            continue
        out.append(
            EntityProfile(
                name=name,
                aliases=list(spec.get("aliases", [])),
                ambiguous=bool(spec.get("ambiguous", False)),
                identifiers=list(spec.get("identifiers", [])),
                exclude=list(spec.get("exclude", [])),
            )
        )
    return out
