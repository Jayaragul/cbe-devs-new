"""Temporal gating: the rolling window, and what to do about missing times.

This is the strictest part of the brief and the part general-purpose search
gets wrong. Two failure modes are handled explicitly:

1.  **Freshness is not recency of indexing.** Search engines surface pages by
    when they crawled them, and news sites re-stamp `dateModified` on
    republish. `resolve_publication` therefore refuses to treat a
    modified-only timestamp as a publication time. FreshLLMs (Vu et al.,
    arXiv:2310.03214) shows search-augmented prompting lifts accuracy on
    fast-changing questions substantially, but its FreshPrompt design also
    depends on the retrieved evidence carrying a trustworthy date -- the
    date is part of the evidence, not metadata about it.

2.  **Date-only items are decidable more often than they look.** A page dated
    today with no clock time appears to straddle the window boundary. But the
    act of observing the page bounds its publication time from above: it
    cannot have been published after we read it. Folding the observation
    instant into the item's possible-publication interval turns most
    same-day, time-less items from "ambiguous, exclude" into "provably
    inside the window" -- without ever guessing a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

try:  # pragma: no cover - platform dependent
    from zoneinfo import ZoneInfo

    _HAVE_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAVE_ZONEINFO = False

#: Windows installs frequently lack the IANA database unless the `tzdata`
#: package is present, so the reporting zone has a hard-coded fallback.
#: India has not observed DST since 1945, which makes a fixed offset safe
#: here in a way it would not be for, say, Europe/London.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def zone(name: str):
    """Resolve a timezone name, falling back to IST's fixed offset."""
    if name in ("Asia/Kolkata", "Asia/Calcutta", "IST"):
        if _HAVE_ZONEINFO:
            try:
                return ZoneInfo(name if name != "IST" else "Asia/Kolkata")
            except Exception:
                return IST
        return IST
    if _HAVE_ZONEINFO:
        return ZoneInfo(name)
    raise LookupError(f"timezone {name!r} needs the tzdata package on this platform")


@dataclass(frozen=True)
class Window:
    """A half-open reporting window ``(start, end]`` in a reporting zone."""

    start: datetime
    end: datetime
    tz_name: str = "Asia/Kolkata"

    def contains(self, moment: datetime) -> bool:
        return self.start < moment <= self.end

    def label(self) -> str:
        """The window line that heads the report."""
        fmt = "%d %B %Y, %H:%M"
        tz = zone(self.tz_name)
        abbr = "IST" if self.tz_name.startswith("Asia/K") else self.tz_name
        return (
            f"{self.start.astimezone(tz).strftime(fmt)} to "
            f"{self.end.astimezone(tz).strftime(fmt)}, {abbr}"
        )


def window_ending_at(
    now: datetime, hours: int = 24, tz_name: str = "Asia/Kolkata"
) -> Window:
    """Build the rolling window of `hours` ending at `now`.

    Deliberately *not* the current calendar day: the brief asks for a
    rolling period ending at execution time.
    """
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    tz = zone(tz_name)
    end = now.astimezone(tz)
    return Window(start=end - timedelta(hours=hours), end=end, tz_name=tz_name)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

#: Formats actually seen on textile trade sites, most specific first.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %b '%y",      # Fibre2Fashion article byline, e.g. "07 Sep '26"
    "%d %B '%y",
    "%b %d, %Y",      # "Sep 07, 2026"
    "%B %d, %Y",      # "September 7, 2026"
    "%d %b %Y",
    "%d %B %Y",
    # Fibre2Fashion's own JSON-LD datePublished/dateModified, observed live:
    # {"datePublished": "9/7/2026 8:01:00 AM"} for the 07 Sep '26 byline
    # confirmed elsewhere on the same page -- M/D/Y, not D/M/Y. The clock
    # time carries no timezone marker, so it is still discarded below and
    # the result is date-only, consistent with every other non-ISO format
    # here; this format's only job is to recover the *date* reliably.
    "%m/%d/%Y %I:%M:%S %p",
    "%d/%m/%Y",
)


@dataclass(frozen=True)
class DateFinding:
    """A date read off a page, with where it came from."""

    #: Full instant when the page gave a time; otherwise midnight of the day.
    moment: datetime
    #: True when the page gave a calendar date but no clock time.
    date_only: bool
    #: Human-readable provenance, e.g. "byline .latest-news-date-main".
    provenance: str
    #: True when the only timestamp available was a modified/updated one.
    modified_only: bool = False


def parse_page_date(
    raw: str, provenance: str, tz_name: str = "Asia/Kolkata"
) -> Optional[DateFinding]:
    """Parse a date string lifted from a source page.

    Returns None when the string cannot be parsed. A naive datetime is
    interpreted in the *reporting* zone only when the site is known to
    publish in it; callers that cannot establish the site's zone should
    treat the result as date-only rather than inventing a time, which is
    why an unzoned clock time is downgraded here.
    """
    text = (raw or "").strip()
    if not text:
        return None
    tz = zone(tz_name)

    # ISO 8601 first, since structured metadata usually carries an offset.
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return DateFinding(parsed.astimezone(tz), False, provenance)
        # A clock time with no offset is not a knowable instant.
        has_clock = parsed.time() != time(0, 0)
        return DateFinding(
            datetime.combine(parsed.date(), time(0, 0), tzinfo=tz),
            date_only=True,
            provenance=provenance + (" (offset missing)" if has_clock else ""),
        )

    cleaned = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return DateFinding(
            datetime.combine(parsed.date(), time(0, 0), tzinfo=tz),
            date_only=True,
            provenance=provenance,
        )
    return None


def resolve_publication(
    published: Optional[DateFinding], modified: Optional[DateFinding]
) -> Optional[DateFinding]:
    """Choose which timestamp to trust.

    A published date always wins. A modified date alone is *not* evidence of
    publication inside the window -- that is precisely how a re-syndicated
    2023 article gets mistaken for today's news -- so it is returned flagged,
    and the gate rejects it.
    """
    if published is not None:
        return published
    if modified is not None:
        return DateFinding(
            modified.moment,
            modified.date_only,
            modified.provenance,
            modified_only=True,
        )
    return None


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalVerdict:
    eligible: bool
    #: One of the `Rejection` values, as a plain string, when not eligible.
    rejection: Optional[str] = None
    note: str = ""
    #: What the report prints in its Published column.
    display: str = ""


def _possible_interval(
    finding: DateFinding, observed_at: datetime
) -> tuple[datetime, datetime]:
    """The interval in which publication provably occurred.

    For a timestamped item this is a single instant. For a date-only item it
    is that calendar day, truncated at the moment we observed the page --
    because a page cannot have been published after it was read.
    """
    if not finding.date_only:
        return finding.moment, finding.moment
    day_start = finding.moment
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
    return day_start, min(day_end, observed_at)


def classify(
    finding: Optional[DateFinding],
    window: Window,
    observed_at: datetime,
) -> TemporalVerdict:
    """Decide whether an item belongs in the report."""
    if finding is None:
        return TemporalVerdict(
            False, "DATE_UNVERIFIABLE", "no publication date found on the page"
        )
    if finding.modified_only:
        return TemporalVerdict(
            False,
            "DATE_UNVERIFIABLE",
            "only an updated/modified timestamp was available, which does not "
            "establish original publication",
        )

    lo, hi = _possible_interval(finding, observed_at)
    if hi <= window.start or lo > window.end:
        return TemporalVerdict(
            False, "OUT_OF_WINDOW", f"published {_fmt(finding, window.tz_name)}"
        )

    fully_inside = window.start < lo and hi <= window.end
    if not fully_inside:
        return TemporalVerdict(
            False,
            "DATE_AMBIGUOUS",
            "date-only item straddling the window boundary; eligibility cannot "
            "be established without a publication time",
        )

    return TemporalVerdict(True, None, "", _fmt(finding, window.tz_name))


def _fmt(finding: DateFinding, tz_name: str) -> str:
    tz = zone(tz_name)
    local = finding.moment.astimezone(tz)
    abbr = "IST" if tz_name.startswith("Asia/K") else tz_name
    if finding.date_only:
        return f"{local.strftime('%d %b %Y')} — Time not stated"
    return f"{local.strftime('%d %b %Y, %H:%M')} {abbr}"
