"""One unattended sweep, for the scheduled run.

Reads the watchlist from storage, runs the sweep, and writes both the
report and a log line back to storage:

    archive/sweep-<stamp>.json    the report
    logs/sweep-<stamp>.log        what happened, including failures

Storage is chosen by the environment (see ai_signal/live/storage.py), so
the same script runs against a local directory in development and against
the bucket on Cloud Run with no code change.

A failure still writes its log before exiting non-zero: a scheduled job
that dies silently is indistinguishable from one that never fired.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ai_signal import pipeline, temporal, verify
from ai_signal.live import storage as store
from ai_signal.live import watchlist_store
from ai_signal.live.http_fetcher import HttpPageFetcher
from ai_signal.live.nse_filings import NseFilingsProvider
from ai_signal.live.report_json import sweep_result_to_report
from ai_signal.live.rss_provider import RssSearchProvider
from ai_signal.providers import RoutingFetcher


def build_web_sources(env: dict | None = None):
    """Build the web discovery/fetch stack from an explicit environment."""
    env = os.environ if env is None else env
    providers = [RssSearchProvider()]
    fetcher = HttpPageFetcher()
    notes: list[str] = []
    if env.get("AI_SIGNAL_GOOGLE_NEWS", "").strip().lower() in {"1", "true", "yes", "on"}:
        from ai_signal.live.google_news import GoogleNewsPageFetcher, GoogleNewsSearchProvider

        providers.append(GoogleNewsSearchProvider())
        fetcher = GoogleNewsPageFetcher(primary=fetcher)
        notes.append("Google News RSS: enabled (personal/non-commercial use only)")
    return providers, fetcher, notes


def run_sweep(
    storage: store.Storage, now: datetime, *, require_rss: bool = False
) -> tuple[dict, list[str]]:
    """Execute one sweep. Returns the report and the log lines."""
    log: list[str] = []

    document = watchlist_store.load_document(storage)
    window = watchlist_store.window_hours(document)
    config = pipeline.SweepConfig(
        window_hours=window,
        lookback_hours=pipeline.lookback_for(window),
        company_context_query=verify.AI_COMPANY_CONTEXT,
    )

    terms = watchlist_store.to_terms(document)
    log.append(f"watchlist: {len(terms)} enabled terms, {window}h reporting window")
    if not terms:
        raise ValueError("the stored watchlist has no enabled terms")

    providers, fetcher, source_notes = build_web_sources()
    log.extend(source_notes)

    symbols = sorted({t.nse_symbol for t in terms if t.nse_symbol})
    if symbols:
        filings = NseFilingsProvider(
            symbols=symbols, lookback_hours=config.lookback_hours
        )
        providers.append(filings)
        fetcher = RoutingFetcher(primary=fetcher, specialists=[filings.fetcher()])
        log.append(f"exchange filings: watching {', '.join(symbols)}")

    result = pipeline.run(terms, providers, fetcher, now, config)

    verified = sum(len(v) for v in result.included.values())
    log.append(f"verified {verified} item(s) in the last {config.window_hours}h")
    for bucket, items in result.included.items():
        log.append(f"  {bucket.value}: {len(items)}")
    for reason, count in result.rejections_by_reason().items():
        log.append(f"  rejected ({reason}): {count}")
    # A dead feed shrinks the result silently unless it is said out loud.
    for limitation in result.limitations:
        log.append(f"  LIMITATION: {limitation}")

    report = sweep_result_to_report(result, created_by="scheduled-sweep", demo=False)
    rss = next(provider for provider in providers if isinstance(provider, RssSearchProvider))
    feed_health = rss.health()
    if require_rss and not any(
        status.startswith("ok:") and status != "ok: 0 items"
        for status in feed_health.values()
    ):
        raise RuntimeError(f"No RSS feed returned entries. {feed_health}")
    if feed_health:
        report["notes"].append({
            "label": "RSS feed status",
            "text": " | ".join(f"{name}: {status}" for name, status in feed_health.items()),
        })
    return report, log


def main() -> int:
    now = datetime.now(temporal.IST)
    # Same stamp format save_report uses internally, computed from the same
    # `now` -- the log and the report it describes always end up named for
    # the same run without the two having to coordinate on it.
    stamp = now.strftime("%Y%m%d-%H%M%S")
    log_key = f"{store.LOGS_PREFIX}sweep-{stamp}.log"
    storage = store.from_env()

    header = f"{now:%Y-%m-%d %H:%M} IST | storage {store.describe(storage)}"
    log = [header]

    try:
        report, sweep_log = run_sweep(storage, now)
        log.extend(sweep_log)
    except Exception:
        log.append("SWEEP FAILED")
        log.append(traceback.format_exc())
        storage.write_text(log_key, "\n".join(log))
        print("\n".join(log), file=sys.stderr)
        return 1

    archive_key = store.save_report(storage, report, now)
    log.append(f"{archive_key} written")
    storage.write_text(log_key, "\n".join(log))

    print("\n".join(log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
