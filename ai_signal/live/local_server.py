"""Local server for the "Run sweep" button -- real search by default.

Serves this project's static files (so the page itself is reachable) and
one POST endpoint, /api/refresh, that runs the real ai_signal pipeline
and returns a report in exactly the shape the page's renderReportBody()
expects.

Which backend runs is decided ONCE, at startup, checked once rather than
per-request so the server's behaviour cannot change mid-session without a
visible restart -- the startup banner always says which one is live:

    default            -> ai_signal.live.rss_provider.RssSearchProvider
                          (real, public RSS feeds -- no API key, no signup)
                          + HttpPageFetcher for real page verification.
                          `demoData: false`. This is the normal path.
    BRAVE_API_KEY set  -> Brave Search results are ALSO fused in alongside
                          the RSS feeds (two independent providers feeding
                          the same rank-fusion step) -- entirely optional,
                          never required. Set it yourself, never into a
                          chat with an AI assistant:
                              $env:BRAVE_API_KEY = "your-key-here"
                          Get one at https://brave.com/search/api/ (free
                          tier: 2,000 queries/month, 1/second -- paced
                          automatically).
    AI_SIGNAL_DEMO_MODE=1  -> forces the offline demo dataset regardless of the
                          above -- for testing the UI wiring with no
                          network access at all. `demoData: true`.

A full watchlist sweep is 90+ queries against real feeds and a real page
fetch per surviving candidate; expect it to take a while, not to be
instant -- that is normal, not a hang.

Run:
    python ai_signal/live/local_server.py [port]
Then open:
    http://localhost:<port>/index.html
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

SRC_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root, for imports
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ai_signal import pipeline, temporal, verify  # noqa: E402
from ai_signal.model import Bucket  # noqa: E402
from ai_signal.live import appdirs  # noqa: E402
from ai_signal.live.demo_provider import DemoFetcher, DemoSearchProvider  # noqa: E402
from ai_signal.live.report_json import sweep_result_to_report  # noqa: E402
from ai_signal.live import storage as store  # noqa: E402
from ai_signal.live import watchlist_store  # noqa: E402

#: Where the dashboard and its assets live -- this
#: build's own files, not a user's data. See appdirs.app_root()'s
#: docstring for why that differs from SRC_ROOT above under a frozen
#: (PyInstaller) build: imports need the real source tree, but static
#: files need wherever *this build* bundled them.
ROOT = appdirs.app_root()

IST = temporal.IST
DEFAULT_PORT = 8787
BRAVE_KEY_VAR = "BRAVE_API_KEY"
DEMO_OVERRIDE_VAR = "AI_SIGNAL_DEMO_MODE"
GOOGLE_NEWS_VAR = "AI_SIGNAL_GOOGLE_NEWS"


def _enabled(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_backend(env: Optional[dict] = None):
    """Decide (providers, fetcher_factory, is_live, note) from the environment.

    `fetcher_factory` is `now -> PageFetcher` rather than a ready-made
    fetcher: `DemoFetcher` needs an explicit reference time (see its
    docstring -- it deliberately does not read the wall clock itself), and
    that time is only known per-request, not at server startup when this
    function runs. `HttpPageFetcher` has no such need but is wrapped the
    same way so callers do not have to care which mode they are in.

    Pure with respect to its `env` argument (defaults to `os.environ` only
    at the call site in `main`), so the branching is unit-testable without
    ever touching the real environment or the network -- pass a plain dict
    to exercise either path deterministically.
    """
    env = os.environ if env is None else env

    if _enabled(env.get(DEMO_OVERRIDE_VAR)):
        return (
            [DemoSearchProvider()],
            lambda now: DemoFetcher(reference_time=now),
            False,
            f"{DEMO_OVERRIDE_VAR} is set -- forcing the offline demo dataset "
            f"regardless of network access.",
        )

    # Imported lazily: importing ai_signal.live.demo_provider (the module
    # this file already needs regardless) must not require network-capable
    # modules to load too, so these stay local to the branch that uses them.
    from ai_signal.live.http_fetcher import HttpPageFetcher
    from ai_signal.live.rss_provider import RssSearchProvider

    rss = RssSearchProvider()
    providers = [rss]
    notes = [
        f"Live RSS search over {len(rss.feeds)} real AI news feeds "
        f"(see ai_signal/live/rss_provider.py for the list) -- no API "
        f"key needed."
    ]

    google_news_enabled = _enabled(env.get(GOOGLE_NEWS_VAR))
    if google_news_enabled:
        from ai_signal.live.google_news import GoogleNewsSearchProvider

        providers.append(GoogleNewsSearchProvider())
        notes.append(
            f"{GOOGLE_NEWS_VAR} is enabled -- Google News keyword RSS is "
            "fused in with exact feed-timestamp bounds; confirm that its "
            "personal/non-commercial usage terms fit this deployment."
        )

    key = env.get(BRAVE_KEY_VAR)
    if key:
        from ai_signal.live.brave import BraveSearchProvider

        # Constructed directly from the already-resolved `key`, not via
        # `BraveSearchProvider.from_env()` -- that classmethod re-reads the
        # real `os.environ` internally, which would silently ignore an
        # `env` dict a caller (a test, in particular) passed in here.
        providers.append(BraveSearchProvider(api_key=key))
        notes.append(
            f"{BRAVE_KEY_VAR} is also set -- Brave Search is fused in "
            f"alongside the RSS feeds (real API quota, paced automatically)."
        )

    def fetcher_factory(now):
        http = HttpPageFetcher()
        if google_news_enabled:
            from ai_signal.live.google_news import GoogleNewsPageFetcher

            return GoogleNewsPageFetcher(primary=http)
        return http

    return (providers, fetcher_factory, True, " ".join(notes))


def _terms_from_payload(payload: dict) -> list[pipeline.WatchTerm]:
    out: list[pipeline.WatchTerm] = []
    for t in payload.get("terms") or []:
        raw_bucket = str(t.get("category") or "clients").strip().lower()
        try:
            bucket = Bucket(raw_bucket)
        except ValueError:
            bucket = Bucket.CLIENTS
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        out.append(
            pipeline.WatchTerm(
                name=name,
                bucket=bucket,
                aliases=[str(a) for a in (t.get("aliases") or [])],
                enabled=bool(t.get("enabled", True)),
                # Accepted in either spelling: the page writes camelCase,
                # the scratch watchlist files use snake_case.
                nse_symbol=str(t.get("nseSymbol") or t.get("nse_symbol") or "").strip(),
            )
        )
    return out


class Handler(SimpleHTTPRequestHandler):
    """Static file server (GET) plus one JSON API route (POST).

    `providers`/`fetcher`/`is_live` are class attributes, assigned once by
    `main()` before the server starts -- `http.server` builds a fresh
    Handler instance per request, so per-instance state would not survive
    between requests anyway, and fixing the backend at boot is also what
    makes the startup banner an honest, complete description of what every
    request will do.
    """

    providers: list = []
    fetcher_factory = None  # now -> PageFetcher; see build_backend
    is_live = False
    storage = None  # set by main(); see ai_signal/live/storage.py

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[ai-signal-local-server] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # CORS preflight, harmless even same-origin
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_json_body(self) -> Optional[dict]:
        """The request body as a dict, or None once an error was sent."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return None

    def do_GET(self) -> None:
        """Static files, plus the stored watchlist."""
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if self.path.rstrip("/") == "/api/watchlist":
            try:
                self._send_json(200, watchlist_store.load_document(self.storage))
            except Exception as exc:
                self._send_json(500, {"error": f"could not read watchlist: {exc}"})
            return
        if path == "/api/archive":
            raw_query = self.path.split("?", 1)[1] if "?" in self.path else ""
            limit_param = dict(
                pair.split("=", 1) for pair in raw_query.split("&") if "=" in pair
            ).get("limit")
            try:
                limit = int(limit_param) if limit_param else 60
                self._send_json(200, {"reports": store.list_archive(self.storage, limit=limit)})
            except Exception as exc:
                self._send_json(500, {"error": f"could not read archive: {exc}"})
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/index.html")
            self.end_headers()
            return
        if path not in {"/index.html", "/assets/signal-orbit.webp", "/assets/dashboard.css", "/assets/cbe-devs-mark.png"}:
            self._send_json(404, {"error": "no such endpoint: " + self.path})
            return
        super().do_GET()

    def do_PUT(self) -> None:
        """Replace the stored watchlist.

        The whole document is replaced rather than patched: the page always
        holds the complete list, and a last-writer-wins replace is easier to
        reason about than a merge whose conflict rules nobody has specified.
        """
        if self.path.rstrip("/") != "/api/watchlist":
            self._send_json(404, {"error": "no such endpoint: " + self.path})
            return

        payload = self._read_json_body()
        if payload is None:
            return
        try:
            watchlist_store.save_document(self.storage, payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(500, {"error": f"could not save watchlist: {exc}"})
            return
        self._send_json(200, {"saved": len(payload.get("terms") or [])})

    def do_POST(self) -> None:
        if self.path != "/api/refresh":
            self._send_json(404, {"error": "no such endpoint: " + self.path})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        terms = _terms_from_payload(payload)
        if not terms:
            self._send_json(400, {"error": "no enabled watchlist terms were sent"})
            return

        try:
            # Not `payload.get("windowHours") or 24` -- that treats an
            # explicit 0 as "absent" and silently turns it into 24 instead
            # of letting the range check below reject it. Caught by a test
            # sending exactly that value, not spotted by inspection.
            raw_window = payload.get("windowHours")
            window_hours = int(raw_window) if raw_window is not None else 24
        except (TypeError, ValueError):
            self._send_json(400, {"error": "windowHours must be a number"})
            return
        if not (pipeline.MIN_WINDOW_HOURS <= window_hours <= pipeline.MAX_WINDOW_HOURS):
            self._send_json(400, {
                "error": f"windowHours must be between {pipeline.MIN_WINDOW_HOURS} "
                         f"and {pipeline.MAX_WINDOW_HOURS}"
            })
            return
        now = datetime.now(IST)
        config = pipeline.SweepConfig(
            window_hours=window_hours,
            lookback_hours=pipeline.lookback_for(window_hours),
            company_context_query=verify.AI_COMPANY_CONTEXT,
        )

        # The exchange-filings provider is built per request, not at
        # startup, because the symbols it watches come from the watchlist
        # that arrived with this request. Building it here also keeps it
        # per-request state: this server is threaded, so mutating a shared
        # provider's symbol list would race between concurrent refreshes.
        providers = list(self.providers)
        fetcher = self.fetcher_factory(now)
        symbols = sorted({t.nse_symbol for t in terms if t.nse_symbol})
        if self.is_live and symbols:
            from ai_signal.live.nse_filings import NseFilingsProvider
            from ai_signal.providers import RoutingFetcher

            filings = NseFilingsProvider(
                symbols=symbols, lookback_hours=config.lookback_hours
            )
            providers.append(filings)
            fetcher = RoutingFetcher(primary=fetcher, specialists=[filings.fetcher()])

        try:
            result = pipeline.run(
                terms,
                providers=providers,
                fetcher=fetcher,
                now=now,
                config=config,
            )
            report = sweep_result_to_report(
                result,
                created_by="force-refresh-live" if self.is_live else "force-refresh-demo",
                demo=not self.is_live,
                demo_note=None if self.is_live else (
                    "This sweep used fictional articles from the offline preview."
                ),
            )
            # A feed that is entirely down looks identical, from the report
            # alone, to "genuinely no news" -- an empty section otherwise
            # gives no way to tell those apart. Surface real feed status
            # explicitly, independent of whether the sweep found anything.
            for provider in providers:
                if hasattr(provider, "health"):
                    status = provider.health()
                    if status:
                        summary = "; ".join(f"{k}: {v}" for k, v in status.items())
                        report["notes"].append({
                            "label": f"{provider.name} feed status",
                            "text": summary,
                        })

            # Run sweep used to be a preview that vanished the moment
            # this tab closed: every other browser saw a blank Report tab
            # even seconds after a real sweep had just run. Saved here, not
            # only from the scheduled job, so a manual click becomes part
            # of the same shared history anyone can read back from
            # /api/archive. Demo-mode runs are deliberately excluded --
            # fixture data has no business in the real archive.
            if self.is_live:
                try:
                    store.save_report(self.storage, report, now)
                except Exception as exc:
                    report["notes"].append({
                        "label": "archive",
                        "text": f"this report could not be saved to shared storage: {exc}",
                    })
        except Exception as exc:  # a sweep must not crash the server
            self._send_json(500, {"error": f"sweep failed: {exc}"})
            return

        self._send_json(200, report)


def main() -> None:
    # Cloud Run assigns the port and reaches the container from outside, so
    # both are taken from the environment when present. Locally neither is
    # set and it stays a loopback-only server on the usual port.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT") or DEFAULT_PORT)
    host = os.environ.get("AI_SIGNAL_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")

    providers, fetcher_factory, is_live, note = build_backend()
    Handler.providers = providers
    Handler.fetcher_factory = staticmethod(fetcher_factory)
    Handler.is_live = is_live
    Handler.storage = store.from_env()

    server = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"AI Signal server running at http://{shown}:{port}/index.html")
    print(("[LIVE] " if is_live else "[DEMO] ") + note)
    print(f"Watchlist and archive: {store.describe(Handler.storage)}")
    if is_live:
        print("Every page it opens is a real HTTPS request -- see ai_signal/live/http_fetcher.py.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
