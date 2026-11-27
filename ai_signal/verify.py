"""The verification gate.

This module exists because of one measurement. Liu, Zhang & Liang,
"Evaluating Verifiability in Generative Search Engines" (arXiv:2304.09848),
had humans audit Bing Chat, NeevaAI, Perplexity and YouChat and found that
only 51.5% of generated sentences were fully supported by their citations,
and only 74.5% of citations actually supported the sentence they were
attached to. Those systems are fluent and they cite -- they are simply not
*verified*. Copying that architecture without adding a gate would reproduce
the failure at roughly one unsupported claim in two.

So an item is admitted only when three things are independently attested on
the page that was actually opened:

    date       -- a publication timestamp, not a crawl or modified stamp
    entity     -- the mention resolves to the intended company
    development -- the page reports something that happened

Missing any one is a rejection with a recorded reason, never a hedge. The
three-part ledger is the same move as decomposing a response into atomic
units before checking each one, as in Min et al.'s FActScore
(arXiv:2305.14251) and Wei et al.'s SAFE (arXiv:2403.18802); the difference
is that we decompose the *inclusion decision* rather than generated prose,
because the report's job is to select documents, not to paraphrase them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

from . import entities as ent
from . import temporal
from .model import Candidate, Rejection, SourceType, Verdict
from .query_match import matches as query_matches


AI_COMPANY_CONTEXT = (
    '("AI" OR "artificial intelligence" OR "language model" OR "reasoning model" '
    'OR "multimodal model" OR "LLM" OR "machine learning" OR "neural network" '
    'OR "open-weight" OR "inference" OR "model weights" OR "GPU cluster")'
)

# --------------------------------------------------------------------------
# Structurally non-qualifying pages
# --------------------------------------------------------------------------

#: Company-profile and lead-generation databases. These carry an evergreen
#: profile, never a development, and they rank well for company names.
DIRECTORY_HOSTS = frozenset(
    """zoominfo.com owler.com tracxn.com pitchbook.com cbinsights.com
    crunchbase.com thecompanycheck.com tofler.in rocketreach.co justdial.com
    indiamart.com exportersindia.com dnb.com panjiva.com importinfo.com
    theofficialboard.com scribd.com es.scribd.com viatrademart.com
    wesuggestsoftware.com""".split()
)

#: Social platforms. The brief excludes social-media-only posts.
SOCIAL_HOSTS = frozenset(
    "linkedin.com facebook.com instagram.com twitter.com x.com youtube.com "
    "medium.com reddit.com".split()
)

#: Hosts that sell market reports; their press releases are advertisements
#: for a paid document rather than reporting.
MARKET_REPORT_HOSTS = frozenset(
    """marketsandmarkets.com researchandmarkets.com grandviewresearch.com
    bccresearch.com mordorintelligence.com globenewswire.com
    prnewswire.com""".split()
)

#: Retail stock-tip and screener sites. They publish a constant stream of
#: "5 penny stocks to buy" listicles that name dozens of companies, so they
#: rank for any listed client and carry event words ("dividend", "record
#: date") without reporting a development about anyone. Measured: enabling
#: broader news discovery put two of these straight into the clients
#: bucket -- "Readymade Garments Penny Stocks India 2027: 5 Picks" was the
#: first client-bucket item that run produced. They are matched by host
#: because the pattern is the whole publication, not one bad page.
STOCK_TIP_HOSTS = frozenset(
    """univest.in simplywall.st valueresearchonline.com tickertape.in
    multibagg.ai investywise.com sahi.com 5paisa.com angelone.in
    stockedge.com equitymaster.com moneyworks4me.com trendlyne.com
    marketsmojo.com""".split()
)

#: URL shapes that indicate an automatically generated quote page.
QUOTE_PATTERNS = (
    re.compile(r"/quote/", re.I),
    re.compile(r"/stocks?/[^/]+/?$", re.I),
    re.compile(r"/company/[A-Z0-9]+/(consolidated|standalone)", re.I),
    re.compile(r"share-price", re.I),
    re.compile(r"get-quotes", re.I),
    re.compile(r"/symbols?/", re.I),
)

#: Recruitment portals. They host per-company pages that rank well for
#: company names and never carry a development.
RECRUITMENT_HOSTS = frozenset(
    """naukri.com indeed.com glassdoor.com glassdoor.co.in monsterindia.com
    shine.com timesjobs.com foundit.in ziprecruiter.com""".split()
)

#: Matched against the URL *path only*, so a host such as "jobsite.com"
#: does not trip them. "jobs" is matched as a whole segment-word because
#: recruitment portals write it as a suffix, e.g. "/shahi-jobs/about-us".
#: Words that identify a careers page when they are a *complete* path
#: segment. Matching inside a slug is not safe: real trade headlines include
#: "US manufacturing jobs rise 16,000 in August" and "Recruitment drive lifts
#: garment output", both of which a hyphen-bounded pattern would discard.
#: This is a recall-first pipeline, so the pre-filter errs toward fetching.
#: Recruitment *portals* are caught by host instead, above.
JOB_SEGMENTS = frozenset(
    """job jobs career careers vacancy vacancies recruitment recruiting
    hiring apply-now""".split()
)


def _is_job_path(path: str) -> bool:
    return any(seg.lower() in JOB_SEGMENTS for seg in path.split("/") if seg)

#: Verbs whose presence marks an actual event, given as base forms. Writing
#: inflections out by hand is how this filter first went wrong: the list had
#: "unveiled" but not "unveils", and no "debuts" at all, so a real product
#: launch headlined "Coats Digital debuts GSD RealMotion" was scored as
#: having no development. Generating the forms removes that whole class of
#: gap, and keeps matching on *exact words* -- prefix matching would let
#: "sign" fire on "significant", which is everywhere in trade copy.
_CUE_VERB_BASES = (
    "announce launch debut unveil introduce release showcase demonstrate "
    "acquire merge partner sign invest expand commission inaugurate open "
    "appoint resign award win secure order contract file disclose approve "
    "reject raise list deploy adopt implement integrate upgrade complete "
    "start begin report certify recall invest divest relocate pilot trial"
).split()

#: Event nouns, which take no inflection rules.
_CUE_NOUNS = frozenset(
    """acquisition merger partnership investment expansion results profit
    revenue dividend orders contracts filing funding ipo shutdown closure
    strike layoffs rollout launch debut""".split()
)

#: Verbs whose past forms are irregular, so the rules below cannot derive
#: them. Kept explicit and small.
_CUE_IRREGULAR = {
    "win": ("won",),
    "begin": ("began", "begun"),
    "sign": ("signed",),
    "file": ("filed",),
    # British spellings ("trialled", "trialling"), common in the UK/India
    # trade press this pipeline actually reads.
    "trial": ("trialled", "trialling"),
}


def _inflect(base: str) -> set[str]:
    """Deterministically expand a base verb to the forms news copy uses."""
    forms = {base, base + "s"}
    if base.endswith("e"):
        forms.add(base + "d")          # announce -> announced
        forms.add(base[:-1] + "ing")   # announce -> announcing
    elif base.endswith("y"):
        forms.add(base[:-1] + "ies")   # certify -> certifies
        forms.add(base[:-1] + "ied")   # certify -> certified
        forms.add(base + "ing")
    else:
        forms.add(base + "ed")
        forms.add(base + "ing")
    forms.update(_CUE_IRREGULAR.get(base, ()))
    return forms


def _build_cues() -> frozenset[str]:
    cues: set[str] = set(_CUE_NOUNS)
    for base in _CUE_VERB_BASES:
        cues |= _inflect(base)
    return frozenset(cues)


DEVELOPMENT_CUES = _build_cues()


def _host(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _registrable(host: str) -> str:
    """Best-effort eTLD+1, sufficient for matching our host sets."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def structural_rejection(url: str) -> Optional[tuple[Rejection, str]]:
    """Reject a URL on shape alone, before spending a fetch on it."""
    host = _host(url)
    base = _registrable(host)
    path = urlparse(url).path or "/"

    if base in DIRECTORY_HOSTS or host in DIRECTORY_HOSTS:
        return Rejection.NO_DEVELOPMENT, f"{host} is a company directory"
    if base in SOCIAL_HOSTS or host in SOCIAL_HOSTS:
        return Rejection.NO_DEVELOPMENT, f"{host} is a social platform"
    if base in MARKET_REPORT_HOSTS or host in MARKET_REPORT_HOSTS:
        return Rejection.NO_DEVELOPMENT, f"{host} distributes market-report promotion"
    if base in STOCK_TIP_HOSTS or host in STOCK_TIP_HOSTS:
        return Rejection.NO_DEVELOPMENT, f"{host} publishes stock tips, not reporting"
    if base in RECRUITMENT_HOSTS or host in RECRUITMENT_HOSTS:
        return Rejection.NO_DEVELOPMENT, f"{host} is a recruitment portal"
    for pat in QUOTE_PATTERNS:
        if pat.search(url):
            return Rejection.NO_DEVELOPMENT, "automatically generated stock-price page"
    if _is_job_path(path):
        return Rejection.NO_DEVELOPMENT, "job or careers listing"
    return None


def development_cues(text: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    return words & DEVELOPMENT_CUES


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


@dataclass
class Ledger:
    """What was independently attested for one candidate."""

    date_ok: bool = False
    entity_ok: bool = False
    development_ok: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.date_ok and self.entity_ok and self.development_ok


#: Seam for a stronger development test -- an entailment model or an LLM
#: asked "does this page report a new event?" -- returning True/False.
#: Exchange announcement categories that are a real development.
#:
#: Filings are not written in news prose. "Outcome of Board Meeting" and
#: "Credit Rating- Revision" are unambiguously material, yet contain none
#: of the event verbs `development_cues` looks for -- measured against 29
#: real NSE filings for the watchlist's listed clients, the verb-based test
#: admitted exactly zero of them, including two board-meeting outcomes and
#: a credit-rating revision. The fix is not more verbs: NSE's `desc` field
#: is a controlled vocabulary, so the category itself is better evidence
#: than any phrasing inside the text, and matching it is exact rather than
#: fuzzy.
MATERIAL_FILING_SUBJECTS = (
    "outcome of board meeting",
    "financial result",
    "credit rating",
    "acquisition",
    "amalgamation",
    "merger",
    "scheme of arrangement",
    "fund raising",
    "preferential issue",
    "allotment",
    "investment",
    "expansion",
    "capacity",
    "new project",
    "joint venture",
    "order",
    "dividend",
    "bonus issue",
    "stock split",
    "change in director",
    "change in kmp",
    "appointment",
    "resignation",
    "disruption",
    "litigation",
    "insolvency",
    "default",
)

#: Categories that are procedural housekeeping, not news. An AGM notice or
#: a newspaper-publication copy tells a salesperson nothing has happened.
ROUTINE_FILING_SUBJECTS = (
    "copy of newspaper publication",
    "shareholders meeting",
    "analysts/institutional investor meet",
    "investor presentation",
    "record date",
    "trading window",
    "compliance certificate",
    "share transfer",
    "loss of share certificate",
    "reg. 74",
)

DevelopmentChecker = Callable[[Candidate], bool]


def filing_development(candidate: Candidate) -> bool:
    """Is an exchange filing a development, judged by its category?

    Explicitly material wins, explicitly routine loses, and anything else
    -- NSE's catch-all "Updates" and "General Updates" cover both a book
    closure and a board outcome -- falls back to the ordinary cue test on
    the announcement text, since the category has told us nothing.
    """
    subject = f"{candidate.title} {candidate.body}".lower()
    if any(s in subject for s in ROUTINE_FILING_SUBJECTS):
        return False
    if any(s in subject for s in MATERIAL_FILING_SUBJECTS):
        return True
    return cue_based_development(candidate)


def development_for_source(candidate: Candidate) -> bool:
    """Default checker: judge a filing as a filing, everything else as news."""
    if candidate.source_type is SourceType.REGULATORY_FILING:
        return filing_development(candidate)
    return cue_based_development(candidate)


def cue_based_development(candidate: Candidate) -> bool:
    """Default development test: at least two distinct event cues.

    Two rather than one, because a single cue fires on boilerplate ("our
    products are reported to..."). This is intentionally a blunt instrument;
    it is a *pre-filter* whose job is to be cheap and to fail visibly, with
    `DevelopmentChecker` available when precision matters more than cost.
    """
    text = f"{candidate.title}\n{candidate.body or candidate.snippet}"
    return len(development_cues(text)) >= 2


def gate(
    candidate: Candidate,
    window: temporal.Window,
    observed_at: datetime,
    profiles: Sequence[ent.EntityProfile] = (),
    development_checker: DevelopmentChecker = development_for_source,
    require_body: bool = True,
    company_context_query: str = "",
    publisher_summary: str = "",
) -> Verdict:
    """Decide whether one candidate enters the report."""
    ledger = Ledger()

    structural = structural_rejection(candidate.url)
    if structural is not None:
        rejection, why = structural
        return Verdict(candidate, False, rejection, why)

    # The page must actually have been opened. A snippet is a discovery aid,
    # not evidence -- the brief is explicit that search snippets without
    # accessible supporting content do not qualify.
    if require_body and not candidate.body:
        return Verdict(
            candidate,
            False,
            Rejection.NOT_RETRIEVABLE,
            "source page was not opened, so nothing on it could be verified",
        )

    # --- date -------------------------------------------------------------
    finding = (
        temporal.DateFinding(
            candidate.published_at,
            candidate.date_only,
            candidate.date_provenance or "page",
        )
        if candidate.published_at is not None
        else None
    )
    tv = temporal.classify(finding, window, observed_at)
    if not tv.eligible:
        return Verdict(
            candidate, False, Rejection[tv.rejection or "DATE_UNVERIFIABLE"], tv.note
        )
    ledger.date_ok = True
    ledger.notes.append(f"date {tv.display} via {candidate.date_provenance or 'page'}")

    # --- entity -----------------------------------------------------------
    if profiles:
        body = candidate.body or candidate.snippet
        if company_context_query:
            lead = f"{candidate.title}\n{publisher_summary.strip() or body[:1200]}"
            if not query_matches(lead, company_context_query):
                return Verdict(
                    candidate, False, Rejection.OFF_TOPIC,
                    "the headline and publisher summary do not establish an AI development",
                )
            text = lead
        else:
            text = f"{candidate.title}\n{body}"
        decision = ent.resolve(text, profiles)
        if not decision.ok:
            why = (
                "; ".join(f"{n}: {r}" for n, r in decision.rejected)
                or "no watchlist entity resolved on the page"
            )
            return Verdict(candidate, False, Rejection.ENTITY_MISMATCH, why)
        candidate.entities = list(decision.matched)
        ledger.entity_ok = True
        ledger.notes.append(f"entities {', '.join(decision.matched)}")
    else:
        # Topic bucket: there is no entity to resolve, only relevance, which
        # the topic query itself already expressed.
        ledger.entity_ok = True

    # --- development ------------------------------------------------------
    if not development_checker(candidate):
        return Verdict(
            candidate,
            False,
            Rejection.NO_DEVELOPMENT,
            "page carries no event language; reads as a profile or listing",
        )
    ledger.development_ok = True

    assert ledger.complete
    return Verdict(candidate, True, None, "; ".join(ledger.notes))


def label_for(verdict: Verdict) -> str:
    """How the report must describe the source's standing."""
    if verdict.candidate.source_type is SourceType.COMPANY_PRESS_RELEASE:
        return "company announcement — not independently verified"
    if verdict.candidate.source_type is SourceType.REGULATORY_FILING:
        return "regulatory / exchange disclosure"
    if verdict.candidate.source_type is SourceType.ORIGINAL_REPORTING:
        return "original reporting"
    if verdict.candidate.source_type is SourceType.TRADE_PUBLICATION:
        return "trade publication"
    return "unclassified source"
