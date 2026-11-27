"""Record and replay, so a sweep is reproducible rather than merely repeatable.

Everything in this package is deterministic: no models, no randomness, no
internal clock reads. But the pipeline's *inputs* come from the live web, and
those are not deterministic at all. A search API returns different results
next week as its index shifts, and a publisher can edit a page after we read
it. So re-running the sweep tomorrow can legitimately produce a different
report without a single line of code having changed.

That is the difference between two properties worth separating:

  repeatable    same inputs -> same output. True of this package already.
  reproducible  a past run can be re-executed exactly. Needs the inputs kept.

This module buys the second by recording every provider response and every
fetched page into a cassette, alongside the `now` the run used. Replaying the
cassette re-executes the identical run offline: no network, no keys, and a
byte-identical report. That makes a published report auditable -- when a
client disputes an item months later, the cassette shows exactly what the
page said at the time, which a re-fetch cannot.

Nothing here needs a third-party dependency; it is `json` and `hashlib`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from .model import Bucket, Candidate, Query, SourceType
from .providers import PageContent, PageFetcher, SearchProvider
from .temporal import DateFinding

CASSETTE_VERSION = 1


# --------------------------------------------------------------------------
# Stable serialisation
# --------------------------------------------------------------------------


def _dump_dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _load_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def dump_candidate(c: Candidate) -> dict[str, Any]:
    """Only the fields a provider supplies.

    `ranks` and `found_by` are deliberately excluded: the pipeline derives
    them from result *position*, which the recorded list order already
    preserves. Recording them too would let a cassette contradict itself.
    """
    return {
        "url": c.url,
        "title": c.title,
        "entities": list(c.entities),
        "snippet": c.snippet,
        "source_name": c.source_name,
        "source_type": c.source_type.value,
    }


def load_candidate(d: dict[str, Any], bucket: Bucket, query: Query) -> Candidate:
    return Candidate(
        url=d["url"],
        title=d.get("title", ""),
        bucket=bucket,
        entities=list(d.get("entities", [])),
        snippet=d.get("snippet", ""),
        source_name=d.get("source_name", ""),
        source_type=SourceType(d.get("source_type", SourceType.UNKNOWN.value)),
        found_by=[query],
    )


def dump_finding(f: Optional[DateFinding]) -> Optional[dict[str, Any]]:
    if f is None:
        return None
    return {
        "moment": _dump_dt(f.moment),
        "date_only": f.date_only,
        "provenance": f.provenance,
        "modified_only": f.modified_only,
    }


def load_finding(d: Optional[dict[str, Any]]) -> Optional[DateFinding]:
    if not d:
        return None
    return DateFinding(
        moment=_load_dt(d["moment"]),
        date_only=bool(d.get("date_only", False)),
        provenance=d.get("provenance", ""),
        modified_only=bool(d.get("modified_only", False)),
    )


def dump_page(p: PageContent) -> dict[str, Any]:
    return {
        "body": p.body,
        "published": dump_finding(p.published),
        "modified": dump_finding(p.modified),
        "source_type": p.source_type.value,
        "source_name": p.source_name,
        "ok": p.ok,
        "error": p.error,
        "resolved_url": p.resolved_url,
    }


def load_page(d: dict[str, Any]) -> PageContent:
    return PageContent(
        body=d.get("body", ""),
        published=load_finding(d.get("published")),
        modified=load_finding(d.get("modified")),
        source_type=SourceType(d.get("source_type", SourceType.UNKNOWN.value)),
        source_name=d.get("source_name", ""),
        ok=bool(d.get("ok", True)),
        error=d.get("error", ""),
        resolved_url=d.get("resolved_url", ""),
    )


def search_key(provider: str, query: Query, limit: int) -> str:
    """A content-addressed key for one search call.

    NUL-joined rather than concatenated so that no combination of provider
    name and query text can collide with a different combination.
    """
    raw = "\x00".join([provider, query.text, str(limit)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# The cassette
# --------------------------------------------------------------------------


@dataclass
class Cassette:
    """Everything a sweep read from the outside world, plus its clock."""

    #: The `now` the recorded run used. Replaying with a different value
    #: would move the window and is refused.
    now: Optional[datetime] = None
    window_hours: int = 24
    tz_name: str = "Asia/Kolkata"
    #: search_key -> {"provider", "query", "limit", "results": [candidate]}
    searches: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: url -> page dict
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> str:
        """Canonical JSON: sorted keys and fixed separators, so the text is
        itself stable and can be hashed or diffed meaningfully."""
        payload = {
            "version": CASSETTE_VERSION,
            "now": _dump_dt(self.now),
            "window_hours": self.window_hours,
            "tz_name": self.tz_name,
            "searches": self.searches,
            "pages": self.pages,
        }
        return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Cassette":
        raw = json.loads(text)
        version = raw.get("version")
        if version != CASSETTE_VERSION:
            raise ValueError(
                f"cassette version {version!r} cannot be read by this build "
                f"(expected {CASSETTE_VERSION}); re-record the sweep"
            )
        return cls(
            now=_load_dt(raw.get("now")),
            window_hours=int(raw.get("window_hours", 24)),
            tz_name=raw.get("tz_name", "Asia/Kolkata"),
            searches=raw.get("searches", {}),
            pages=raw.get("pages", {}),
        )

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Cassette":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def digest(self) -> str:
        """Fingerprint of the inputs. Two runs sharing this digest were fed
        identical evidence, which is what makes comparing their reports
        meaningful."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def stats(self) -> dict[str, int]:
        return {
            "searches": len(self.searches),
            "results": sum(len(s.get("results", [])) for s in self.searches.values()),
            "pages": len(self.pages),
        }


# --------------------------------------------------------------------------
# Wrappers
# --------------------------------------------------------------------------


@dataclass
class RecordingProvider:
    """Wraps a live provider and writes what it returned into a cassette."""

    inner: SearchProvider
    cassette: Cassette

    @property
    def name(self) -> str:
        return self.inner.name

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        results = self.inner.search(query, limit)
        self.cassette.searches[search_key(self.name, query, limit)] = {
            "provider": self.name,
            "query": query.text,
            "bucket": query.bucket.value,
            "limit": limit,
            "results": [dump_candidate(c) for c in results],
        }
        return results


@dataclass
class RecordingFetcher:
    """Wraps a live fetcher and stores each page body verbatim."""

    inner: PageFetcher
    cassette: Cassette

    def fetch(self, url: str) -> PageContent:
        page = self.inner.fetch(url)
        self.cassette.pages[url] = dump_page(page)
        return page


@dataclass
class ReplayProvider:
    """Serves a recorded provider's results. Never touches the network."""

    name: str
    cassette: Cassette
    #: When True, a call with no recording raises instead of returning
    #: nothing -- the safe default, since a silent empty list would look
    #: like "the web had no results" rather than "the cassette is short".
    strict: bool = True

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        entry = self.cassette.searches.get(search_key(self.name, query, limit))
        if entry is None:
            if self.strict:
                raise KeyError(
                    f"no recording for provider {self.name!r} query "
                    f"{query.text!r} limit {limit}"
                )
            return []
        return [
            load_candidate(d, query.bucket, query) for d in entry.get("results", [])
        ]


@dataclass
class ReplayFetcher:
    cassette: Cassette
    strict: bool = True

    def fetch(self, url: str) -> PageContent:
        entry = self.cassette.pages.get(url)
        if entry is None:
            if self.strict:
                raise KeyError(f"no recorded page for {url!r}")
            return PageContent(body="", ok=False, error="not in cassette")
        return load_page(entry)


# --------------------------------------------------------------------------
# Output fingerprinting
# --------------------------------------------------------------------------


def canonical_report(result) -> list[dict[str, Any]]:
    """A stable, comparable projection of a SweepResult.

    Deliberately excludes anything that is legitimately run-specific, so the
    comparison tests what the pipeline decided rather than when it ran.
    """
    rows: list[dict[str, Any]] = []
    for bucket in (Bucket.CLIENTS, Bucket.COMPETITORS, Bucket.TOPICS):
        for position, verdict in enumerate(result.included.get(bucket, []), start=1):
            cand = verdict.candidate
            rows.append(
                {
                    "bucket": bucket.value,
                    "position": position,
                    "url": cand.url,
                    "title": cand.title,
                    "entities": sorted(cand.entities),
                    "source_name": cand.source_name,
                    "source_type": cand.source_type.value,
                    "published_at": _dump_dt(cand.published_at),
                    "date_only": cand.date_only,
                    "duplicates": sorted(verdict.duplicates),
                }
            )
    return rows


def report_digest(result) -> str:
    """Fingerprint of a sweep's decisions. Equal digests mean equal reports."""
    text = json.dumps(canonical_report(result), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replay(result_now: datetime, cassette: Cassette) -> None:
    """Guard against replaying a cassette under a different clock.

    The window is derived from `now`, so replaying with a different `now`
    silently changes which items are in range -- the one way a replay could
    diverge from the original run without any code changing.
    """
    if cassette.now is not None and result_now != cassette.now:
        raise ValueError(
            f"cassette was recorded at {cassette.now.isoformat()} but replay "
            f"was given {result_now.isoformat()}; the reporting window would "
            f"differ, so the replay would not reproduce the original run"
        )
