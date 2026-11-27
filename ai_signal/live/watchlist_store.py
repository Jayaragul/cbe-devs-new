"""Reading and updating the one watchlist document.

The watchlist is the only state a user edits and expects to outlive a
restart, so it is kept as a single JSON object in storage rather than in
browser localStorage (per-browser, invisible to colleagues) or in the
container image (reset on every deploy).

`watchlist.json` in the repo is the *seed*: it is copied into storage the
first time the bucket has no watchlist, and ignored ever after. That gives
a fresh deployment something sensible to show without the file continuing
to override what people subsequently edit.
"""

from __future__ import annotations

import json

from .. import pipeline
from ..model import Bucket
from . import appdirs
from . import storage as store

#: The watchlist document's own default when windowHours is absent --
#: matches SweepConfig's own default, so a document with no opinion on
#: this produces exactly today's existing behaviour.
DEFAULT_WINDOW_HOURS = 24

#: The seed shipped with the code, used only when storage is empty.
SEED = appdirs.app_root() / "watchlist.json"

_BUCKETS = {b.value: b for b in Bucket}


def load_document(storage: store.Storage) -> dict:
    """The watchlist as a plain dict, seeding storage on first use."""
    raw = storage.read_text(store.WATCHLIST_KEY)
    if raw is None:
        seed = json.loads(SEED.read_text(encoding="utf-8")) if SEED.is_file() else {"terms": []}
        storage.write_text(store.WATCHLIST_KEY, json.dumps(seed, indent=2, ensure_ascii=False))
        return seed
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{store.WATCHLIST_KEY} is not valid JSON: {exc}") from exc


def save_document(storage: store.Storage, document: dict) -> None:
    """Replace the stored watchlist."""
    if not isinstance(document.get("terms"), list):
        raise ValueError("a watchlist document needs a 'terms' list")
    storage.write_text(
        store.WATCHLIST_KEY, json.dumps(document, indent=2, ensure_ascii=False)
    )


def window_hours(document: dict, default: int = DEFAULT_WINDOW_HOURS) -> int:
    """The reporting window stored in the document, degrading to `default`
    for anything that isn't a sane positive value within
    pipeline.MAX_WINDOW_HOURS -- a missing, corrupted, or hand-edited-wrong
    value falls back quietly rather than breaking the sweep over a display
    setting, the same spirit as to_terms() dropping a malformed row instead
    of raising.
    """
    raw = document.get("windowHours")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < pipeline.MIN_WINDOW_HOURS or value > pipeline.MAX_WINDOW_HOURS:
        return default
    return value


def to_terms(document: dict) -> list[pipeline.WatchTerm]:
    """Turn a watchlist document into the terms a sweep runs on.

    Disabled rows and rows without a name are dropped here, so callers get
    exactly what should be searched.
    """
    terms: list[pipeline.WatchTerm] = []
    for row in document.get("terms") or []:
        if not row.get("enabled", True):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        bucket = _BUCKETS.get(
            str(row.get("category") or "clients").strip().lower(), Bucket.CLIENTS
        )
        terms.append(
            pipeline.WatchTerm(
                name=name,
                bucket=bucket,
                aliases=[str(a) for a in (row.get("aliases") or [])],
                # Written camelCase by the page, snake_case by the files.
                nse_symbol=str(
                    row.get("nseSymbol") or row.get("nse_symbol") or ""
                ).strip(),
            )
        )
    return terms
