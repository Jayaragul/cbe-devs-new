"""Convert a `SweepResult` into the JSON shape the page's `renderReportBody`
expects.

Deliberately pure: a function of a `SweepResult`, nothing else. It touches
no network and no filesystem, so it is unit-testable exactly like the rest
of the deterministic core, even though it lives under `live/` alongside the
network-touching modules -- it is the seam between "the pipeline produced a
result" and "the browser can render it," and keeping it pure means that
seam can be tested without ever starting a server.
"""

from __future__ import annotations

from typing import Optional

from ..model import Bucket, Candidate, Verdict
from ..pipeline import SweepResult

_BUCKET_KEYS = {
    Bucket.CLIENTS: "clients",
    Bucket.COMPETITORS: "competitors",
    Bucket.TOPICS: "topics",
}


def format_published(candidate: Candidate) -> str:
    """Human-readable publication string, matching the hand-written reports'
    convention: a full timestamp when one exists, otherwise a bare date with
    "Time not stated" -- never a guessed time."""
    if candidate.published_at is None:
        return "Time not stated"
    local = candidate.published_at
    if candidate.date_only:
        return local.strftime("%d %b %Y") + ", Time not stated"
    return local.strftime("%d %b %Y, %H:%M") + " IST"


def candidate_to_item(verdict: Verdict) -> dict:
    """Fills the report's "Topic or company" column.

    For clients/competitors this is the resolved entity name. Topic-bucket
    candidates carry no entity at all (topics aren't companies), so falling
    back to the article's own title -- as an earlier version of this
    function did -- just repeated the headline next to itself. The matched
    query text is far more useful there: it says which watched topic the
    article satisfied.
    """
    c = verdict.candidate
    if c.entities:
        topic_or_company = ", ".join(c.entities)
    else:
        q_entity = next((q.entity for q in c.found_by if q.entity), None)
        if q_entity:
            topic_or_company = q_entity
        elif c.found_by:
            topic_or_company = c.found_by[0].text[:70]
        else:
            topic_or_company = c.title[:60]
    return {
        "topicOrCompany": topic_or_company,
        "title": c.title,
        "sourceName": c.source_name or "",
        "sourceType": c.source_type.value,
        "publishedDisplay": format_published(c),
        "publishedAt": c.published_at.isoformat() if c.published_at is not None else None,
        "url": c.url,
        "promotional": bool(verdict.promotional),
    }


def bucket_payload(result: SweepResult, bucket: Bucket, empty_note: str = "") -> dict:
    verdicts = result.included.get(bucket, [])
    items = [candidate_to_item(v) for v in verdicts]
    return {"items": items, "emptyNote": "" if items else empty_note}


def build_notes(result: SweepResult) -> list[dict]:
    """A short, honest verification record -- what was rejected and why,
    the same discipline the hand-written reports follow: an empty section
    must be able to show what it examined, not just that it found nothing.
    """
    notes: list[dict] = []

    reasons = result.rejections_by_reason()
    if reasons:
        parts = "; ".join(f"{count} x {reason}" for reason, count in sorted(reasons.items()))
        notes.append({"label": "Rejections by reason", "text": parts})

    # A handful of concrete examples, skipping pure duplicate noise so the
    # list stays readable.
    concrete = [
        v for v in result.rejected
        if v.rejection is not None and v.rejection.value != "same development already reported from a stronger source"
    ][:6]
    if concrete:
        # v.reason carries the specific instance ("zoominfo.com is a company
        # directory"), not just the general category (v.rejection.value,
        # "passing mention, profile, or generated stock page") -- prefer it
        # when present, since a reader wants to know *why this one*, not
        # only which bucket of reasons it falls into.
        lines = "; ".join(
            f"{v.candidate.title!r} — {v.reason or v.rejection.value}" for v in concrete
        )
        notes.append({"label": "Notable exclusions", "text": lines})

    if result.limitations:
        notes.append({"label": "Limitations", "text": "; ".join(result.limitations)})

    return notes


def sweep_result_to_report(
    result: SweepResult,
    created_by: str = "force-refresh",
    demo: bool = True,
    demo_note: Optional[str] = None,
    empty_notes: Optional[dict] = None,
) -> dict:
    """The full conversion. `empty_notes` optionally supplies a per-bucket
    explanatory sentence for when that bucket has no items (keyed by
    "clients"/"competitors"/"topics"); omitted buckets get a generic note.
    """
    empty_notes = empty_notes or {}
    default_empty = "Nothing published inside the window cleared verification."

    end = result.window.end
    return {
        "date": end.strftime("%Y-%m-%d"),
        "windowStart": result.window.start.isoformat(),
        "windowEnd": result.window.end.isoformat(),
        "checkedAt": result.checked_at.isoformat(),
        "totalVerified": sum(len(v) for v in result.included.values()),
        "buckets": {
            key: bucket_payload(result, bucket, empty_notes.get(key, default_empty))
            for bucket, key in _BUCKET_KEYS.items()
        },
        "notes": build_notes(result),
        "createdBy": created_by,
        "demoData": demo,
        # Only ever fabricate a default note for a genuinely-demo report --
        # doing this unconditionally (the OR-fallback this replaced) meant a
        # real `demo=False` live result still carried a "not live" note in
        # its JSON payload, wrong on its face even though the frontend
        # happens not to render it while demoData is false.
        "demoNote": (
            demo_note or (
                "This sweep used fictional articles from the offline preview."
            )
        ) if demo else None,
    }
