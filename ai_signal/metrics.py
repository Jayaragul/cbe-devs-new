"""Phase-level network metrics for one sweep.

This exists because "why did that take so long" was previously answerable
only by staring at a wall-clock and guessing. A sweep now records, per
phase (discovery fan-out, page fetch), how many requests went out, how many
succeeded, how many hit a 429 or 503, and the p95 latency -- the numbers
needed to tell "the network is genuinely slow today" apart from "something
regressed" without re-instrumenting anything by hand.

Deliberately separate from `SweepResult`'s report content: these are
wall-clock, run-specific measurements, and the report's own determinism
guarantee (`record.report_digest`) is scoped to *decisions* -- which items
were verified and why -- not to how long deciding took. Nothing here is
read by `canonical_report`.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

#: Provider and fetcher errors already encode the HTTP status in their
#: message text ("HTTP 503: Service Unavailable", "failed: HTTP 429 Too
#: Many Requests") -- see http_fetcher.py and google_news.py. Reading it
#: back out here means no provider has to be modified to expose a status
#: code as a separate field just for metrics.
_STATUS_RE = re.compile(r"HTTP (\d{3})")


def extract_status(error_text: Optional[str]) -> Optional[int]:
    if not error_text:
        return None
    match = _STATUS_RE.search(error_text)
    return int(match.group(1)) if match else None


@dataclass
class PhaseMetrics:
    """Counters and latencies for one named phase (e.g. "discovery", "fetch")."""

    name: str
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    status_429: int = 0
    status_503: int = 0
    duration_s: float = 0.0
    #: Individual call latencies, for p95. Not exposed raw -- see to_dict --
    #: since the count of requests already says how many there were.
    _latencies_ms: list = field(default_factory=list, repr=False)

    def p95_latency_ms(self) -> Optional[float]:
        if not self._latencies_ms:
            return None
        ordered = sorted(self._latencies_ms)
        # Nearest-rank method: the smallest value at or above the 95th
        # percentile, which for small samples (a handful of requests) is
        # more honest than an interpolated figure implying false precision.
        index = min(len(ordered) - 1, -(-95 * len(ordered) // 100) - 1)
        return ordered[index]

    def to_dict(self) -> dict:
        p95 = self.p95_latency_ms()
        return {
            "phase": self.name,
            "requests": self.request_count,
            "success": self.success_count,
            "failure": self.failure_count,
            "status_429": self.status_429,
            "status_503": self.status_503,
            "duration_s": round(self.duration_s, 3),
            "p95_latency_ms": round(p95, 1) if p95 is not None else None,
        }


class MetricsCollector:
    """Thread-safe recorder for one sweep's phase metrics.

    Calls to `record` are expected from a single consumer thread in the
    current pipeline design (worker threads only perform the network call
    and return their outcome; all bookkeeping happens back on the main
    thread via `ThreadPoolExecutor.map`'s ordered iteration) -- the lock
    exists so that guarantee is not a silent precondition future code has
    to remember, but an enforced one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phases: dict[str, PhaseMetrics] = {}

    def _get(self, phase: str) -> PhaseMetrics:
        pm = self._phases.get(phase)
        if pm is None:
            pm = PhaseMetrics(name=phase)
            self._phases[phase] = pm
        return pm

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Times the wall-clock span of an entire phase (wraps a thread pool)."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            with self._lock:
                self._get(name).duration_s += elapsed

    def record(
        self, phase: str, *, latency_ms: float, ok: bool, error: Optional[str] = None
    ) -> None:
        """Records one individual request's outcome within a phase."""
        with self._lock:
            pm = self._get(phase)
            pm.request_count += 1
            pm._latencies_ms.append(latency_ms)
            if ok:
                pm.success_count += 1
                return
            pm.failure_count += 1
            status = extract_status(error)
            if status == 429:
                pm.status_429 += 1
            elif status == 503:
                pm.status_503 += 1

    def summary(self) -> list[dict]:
        return [pm.to_dict() for pm in self._phases.values()]
