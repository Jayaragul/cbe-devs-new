"""Brave Search API adapter -- the live `SearchProvider`.

UNVERIFIED AGAINST THE REAL API. Written from Brave's published Web Search
API shape (https://api.search.brave.com/res/v1/web/search), not observed
against a live response in this environment -- no key was available here.
Smoke-test it against a couple of watchlist queries before trusting its
output, and expect to adjust field names if the response shape has moved.

The API key is never a literal in this file, a constructor default, or
anything this code could log. `from_env` is the only way to obtain one: it
reads the named environment variable and raises immediately if it is
missing, rather than silently sending an empty-token request. Set the key
yourself, outside of any AI assistant, with:

    setx BRAVE_API_KEY "your-key-here"        (Windows, new shells)
    $env:BRAVE_API_KEY = "your-key-here"       (current PowerShell session)

Get a key at https://brave.com/search/api/ -- the free tier is 2,000
queries/month, 1 query/second. This watchlist's current 30 terms expand to
roughly 90-100 queries per sweep (see `pipeline.plan`), so the free tier
covers on the order of 20 sweeps a month at today's watchlist size; a
larger watchlist or a tighter cadence will need a paid plan.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from ..model import Candidate, Query

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_USER_AGENT = "AISignal/1.0 (+deterministic verification pipeline)"

#: Brave's freshness parameter takes a preset or a custom `from:to` date
#: range. `Query.lookback_hours` is an hour count, which does not map onto
#: a preset exactly; this picks the closest preset that does not undershoot
#: the requested lookback -- rounding UP preserves the recall-margin intent
#: (the point of lookback_hours) over precision, since the verification
#: gate re-checks every candidate's real publication time regardless.
def _freshness_for(lookback_hours: int) -> str:
    if lookback_hours <= 24:
        return "pd"   # past day
    if lookback_hours <= 24 * 7:
        return "pw"   # past week
    if lookback_hours <= 24 * 31:
        return "pm"   # past month
    return "py"       # past year


class BraveApiKeyMissing(RuntimeError):
    """Raised by `from_env` when the configured environment variable is unset."""


@dataclass
class BraveSearchProvider:
    """A live `SearchProvider` backed by the Brave Web Search API.

    Paced to at most one request per `min_interval_s`: the free tier is
    1 query/second, and one sweep over a real watchlist fires 90+ queries
    (see `pipeline.plan`). Without pacing, the run would burn straight
    through the rate limit on its first handful of queries -- and each
    failure is silently absorbed by `pipeline.run`'s per-query exception
    handling as a recall loss, not a loud error, so an unpaced run would
    quietly return almost nothing rather than visibly failing.
    """

    api_key: str
    name: str = "brave"
    country: str = ""     # e.g. "IN"; blank = no country restriction
    timeout_s: float = 10.0
    min_interval_s: float = 1.1  # slightly slower than the 1/s free-tier cap
    _last_call_at: Optional[float] = field(default=None, repr=False, compare=False)

    def _throttle(self) -> None:
        if self._last_call_at is not None:
            wait = self.min_interval_s - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    @classmethod
    def from_env(cls, env_var: str = "BRAVE_API_KEY", **kwargs) -> "BraveSearchProvider":
        """Build a provider from an environment variable.

        Fails loudly rather than constructing a provider that would send
        empty-credential requests and look like a silent zero-result day.
        """
        key = os.environ.get(env_var)
        if not key:
            raise BraveApiKeyMissing(
                f"environment variable {env_var!r} is not set. Get a key at "
                f"https://brave.com/search/api/ and set it yourself before "
                f"running a live sweep -- this code never reads or stores it "
                f"any other way."
            )
        return cls(api_key=key, **kwargs)

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        self._throttle()
        params = {
            "q": query.text,
            # Brave caps a single call's results; 20 is its documented max.
            "count": str(min(max(limit, 1), 20)),
            "freshness": _freshness_for(query.lookback_hours),
            "safesearch": "off",
        }
        if self.country:
            params["country"] = self.country

        url = f"{_ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Surfaced as a normal exception so `pipeline.run` records it as
            # a stated limitation for *this query only* rather than raising
            # out of the sweep or silently returning zero results.
            #
            # Brave's error body is structured and far more specific than
            # the bare status -- observed live against the real endpoint
            # with a deliberately invalid key:
            #   {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID",
            #              "detail": "The provided subscription token is
            #              invalid.", ...}, "type": "ErrorResponse"}
            # and notably returned as HTTP 422, not 401/403 as the REST
            # convention would suggest -- guessing "422 means a malformed
            # request" here would have been a wrong, misleading hint on the
            # single most common real failure (a bad key).
            detail = None
            try:
                body = json.loads(exc.read().decode("utf-8"))
                err = body.get("error") or {}
                if err.get("code") or err.get("detail"):
                    detail = f"{err.get('code', '?')}: {err.get('detail', '')}".strip(": ")
            except Exception:
                pass  # fall through to the generic status-based hint below

            if detail is None:
                if exc.code == 429:
                    detail = "the free-tier rate limit (1 query/sec, 2,000/month) was hit despite pacing"
                else:
                    detail = f"HTTP {exc.code} {exc.reason}"
            raise RuntimeError(
                f"Brave Search API rejected query {query.text!r}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not reach Brave Search API: {exc.reason}") from exc

        results = (payload.get("web") or {}).get("results") or []
        out: list[Candidate] = []
        for item in results:
            item_url = item.get("url")
            if not item_url:
                continue
            out.append(
                Candidate(
                    url=item_url,
                    title=item.get("title", ""),
                    bucket=query.bucket,
                    snippet=item.get("description", ""),
                    found_by=[query],
                )
            )
        return out
