"""Opens a real URL over HTTPS and hands the bytes to `extract.py`.

This file is the whole reason `live/` is not claimed to be deterministic:
it makes a network call. Everything it does beyond that call -- decoding,
parsing, date extraction -- is delegated to `extract.extract_page`, which
is pure and is what the test suite actually exercises.

UNVERIFIED AGAINST REAL SITES. This was written from the documented shape
of HTTP responses and common publisher markup, not observed against live
traffic in this environment. Smoke-test it against a handful of real
watchlist-relevant URLs (a Fibre2Fashion article is a good first case,
since its date markup is already known from the manual sweeps) before
trusting its output in a report.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..providers import PageContent
from .extract import extract_page

#: A real browser UA. Several trade sites 403 the default urllib UA
#: ("Python-urllib/3.x"), which would otherwise silently look like every
#: watchlist company had gone quiet.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 AISignal/1.0"
)


@dataclass
class HttpPageFetcher:
    """A `PageFetcher` that actually opens the URL.

    `timeout_s` and `max_bytes` exist because this fetcher runs unattended
    against sites it does not control: a hung connection or an
    unexpectedly large response must not stall or blow up a sweep over one
    bad URL.
    """

    timeout_s: float = 15.0
    max_bytes: int = 4_000_000  # 4 MB; a news article page is a few hundred KB
    #: Minimum seconds between two requests to the same host. Added after
    #: directly observing the failure mode: fetching one real Fibre2Fashion
    #: article five times in under a minute (while diagnosing an unrelated
    #: bug) got the next request answered with HTTP 202 and an empty body --
    #: soft bot-mitigation, not a real error, and one this class must not
    #: hand back to the gate as if the page had no content. A full crawler
    #: would also want a shared cross-run cache and robots.txt awareness;
    #: this is the minimum guard for a single sweep's fetch loop.
    min_host_interval_s: float = 3.0
    _last_request_at: dict[str, float] = field(default_factory=dict, repr=False)
    #: One lock per host, created on demand, so a burst of concurrent
    #: fetches to the *same* host still queues up and respects
    #: `min_host_interval_s` -- the whole reason that guard exists -- while
    #: fetches to *different* hosts never wait on each other. A single
    #: shared lock held across the sleep would serialize every fetch in the
    #: sweep regardless of host, which defeats concurrent fetching entirely.
    _host_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _host_locks_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _lock_for_host(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock_for_host(host):
            last = self._last_request_at.get(host)
            if last is not None:
                wait = self.min_host_interval_s - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_request_at[host] = time.monotonic()

    def fetch(self, url: str) -> PageContent:
        self._throttle(url)
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                content_type = resp.headers.get("Content-Type", "")
                final_url = resp.geturl() if hasattr(resp, "geturl") else url
                raw = resp.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            return PageContent(body="", ok=False, error=f"HTTP {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            return PageContent(body="", ok=False, error=f"could not connect: {exc.reason}")
        except TimeoutError:
            return PageContent(body="", ok=False, error=f"timed out after {self.timeout_s}s")
        except Exception as exc:  # a fetcher must not take the whole sweep down
            return PageContent(body="", ok=False, error=f"unexpected fetch error: {exc}")

        if len(raw) > self.max_bytes:
            return PageContent(
                body="", ok=False,
                error=f"response exceeded {self.max_bytes} bytes; not fetched fully",
            )

        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            html_text = raw.decode(charset, errors="replace")
        except LookupError:  # an unrecognised charset name
            html_text = raw.decode("utf-8", errors="replace")

        page = extract_page(html_text, final_url, content_type)
        page.resolved_url = final_url
        return page
