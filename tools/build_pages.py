"""Build a static GitHub Pages briefing from the repository's public watchlist."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_signal import temporal
from ai_signal.live import storage, watchlist_store
from sweep_runner import run_sweep


ARCHIVE_DAYS = 14
ASSETS = ("assets/dashboard.css", "assets/signal-orbit.webp", "assets/cbe-devs-mark.png")
SEED_FIELDS = ("name", "category", "aliases", "enabled", "notes", "nse_symbol", "nseSymbol")


def archive_reports(current: dict, previous: list[dict], now: datetime) -> list[dict]:
    oldest = (now.date() - timedelta(days=ARCHIVE_DAYS - 1)).isoformat()
    today = now.date().isoformat()
    by_date = {current["date"]: current}
    for report in previous:
        date = report.get("date", "")
        if oldest <= date <= today and report.get("demoData") is False:
            by_date.setdefault(date, report)
    return [by_date[date] for date in sorted(by_date, reverse=True)]


def build(output: Path, previous: Path | None) -> int:
    seed = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    watchlist = {
        "windowHours": watchlist_store.window_hours(seed),
        "terms": [{key: row[key] for key in SEED_FIELDS if key in row} for row in seed["terms"]],
    }
    history = []
    if previous is not None and previous.is_file():
        history = json.loads(previous.read_text(encoding="utf-8"))["reports"]

    now = datetime.now(temporal.IST)
    with tempfile.TemporaryDirectory(prefix="ai-signal-pages-") as temp_dir:
        store = storage.LocalStorage(temp_dir)
        watchlist_store.save_document(store, watchlist)
        current, log = run_sweep(store, now, require_rss=True)
    for line in log:
        print(line)

    snapshot = {"watchlist": watchlist, "reports": archive_reports(current, history, now)}
    dashboard = (ROOT / "index.html").read_text(encoding="utf-8")
    marker = '<meta name="ai-signal-mode" content="server">'
    if dashboard.count(marker) != 1:
        raise ValueError("Dashboard is missing its publication mode marker")

    (output / "assets").mkdir(parents=True, exist_ok=True)
    (output / "data").mkdir(exist_ok=True)
    for asset in ASSETS:
        shutil.copyfile(ROOT / asset, output / asset)
    (output / "index.html").write_text(
        dashboard.replace(marker, '<meta name="ai-signal-mode" content="snapshot">'),
        encoding="utf-8",
    )
    (output / "data" / "briefing.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return current["totalVerified"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("_site"))
    parser.add_argument("--previous", type=Path, help="Briefing JSON from an earlier build")
    args = parser.parse_args()
    count = build(args.output, args.previous)
    print(f"Built {args.output} with {count} verified stories")
