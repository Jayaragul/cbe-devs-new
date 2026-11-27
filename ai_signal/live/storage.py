"""Where the watchlist, the archive and the logs live.

Cloud Run gives a container no durable disk: anything written to the local
filesystem is gone at the next cold start, and two instances serving the
same users do not see each other's writes. So the watchlist -- the one
piece of state a user edits and expects to persist -- cannot live in the
image or in browser storage. It needs one shared object that every
instance reads and writes.

Two backends implement the same four operations. `LocalStorage` is the
default and uses the filesystem, so development and the whole test suite
run with no cloud and no credentials. `GcsStorage` talks to a bucket, and
imports `google-cloud-storage` only when it is actually constructed --
that keeps the dependency optional and the rest of the pipeline at zero
third-party imports.

Layout inside the bucket:

    watchlist/watchlist.json     the live watchlist, read and updated
    archive/sweep-<stamp>.json   one report per sweep
    logs/sweep-<stamp>.log       one log per sweep

Retention for `archive/` is deliberately *not* implemented here. Deleting
a month-old report is a bucket lifecycle rule, which Google applies
server-side whether or not this code ever runs again; an application-level
sweep-and-delete would only work while something is scheduled, and would
be one bug away from deleting the watchlist. See `lifecycle.json`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

#: Object keys. Kept here so the server, the runner and the lifecycle rule
#: cannot drift apart on where things go.
WATCHLIST_KEY = "watchlist/watchlist.json"
ARCHIVE_PREFIX = "archive/"
LOGS_PREFIX = "logs/"


class Storage(Protocol):
    """Four operations, deliberately the smallest useful surface."""

    def read_text(self, key: str) -> str | None:
        """Contents of `key`, or None when it does not exist."""

    def write_text(self, key: str, text: str) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete(self, key: str) -> None: ...


class LocalStorage:
    """Filesystem backend. The default, and what the tests use."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        # A key is always a relative, forward-slashed object name. Refusing
        # absolute and parent paths keeps a malformed key from escaping the
        # root and writing somewhere else on disk.
        rel = PurePosixKey(key)
        return self.root.joinpath(*rel.parts)

    def read_text(self, key: str) -> str | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def write_text(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        out = [
            f"{prefix.rstrip('/')}/{p.name}"
            for p in base.iterdir()
            if p.is_file()
        ]
        return sorted(out)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()


class GcsStorage:
    """Google Cloud Storage backend.

    `google-cloud-storage` is imported in the constructor rather than at
    module scope so that importing this module -- which `local_server` does
    unconditionally -- never requires the package to be installed.
    """

    def __init__(self, bucket: str, prefix: str = ""):
        try:
            from google.cloud import storage  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "AI_SIGNAL_STORAGE=gcs needs the Cloud Storage client:\n"
                "    pip install google-cloud-storage"
            ) from exc

        self._bucket = storage.Client().bucket(bucket)
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_text(self, key: str) -> str | None:
        from google.cloud.exceptions import NotFound  # noqa: PLC0415

        blob = self._bucket.blob(self._key(key))
        try:
            return blob.download_as_text()
        except NotFound:
            return None

    def write_text(self, key: str, text: str) -> None:
        blob = self._bucket.blob(self._key(key))
        blob.upload_from_string(text, content_type="application/json")

    def list_keys(self, prefix: str) -> list[str]:
        full = self._key(prefix)
        names = [b.name for b in self._bucket.list_blobs(prefix=full)]
        if self.prefix:
            cut = len(self.prefix) + 1
            names = [n[cut:] for n in names]
        return sorted(n for n in names if not n.endswith("/"))

    def delete(self, key: str) -> None:
        from google.cloud.exceptions import NotFound  # noqa: PLC0415

        try:
            self._bucket.blob(self._key(key)).delete()
        except NotFound:
            pass


class PurePosixKey:
    """The safe parts of an object key, as path components.

    Object stores have a flat namespace where "/" is just a character, but
    `LocalStorage` turns a key into a real path -- so "..", a leading "/"
    and a drive letter all have to be dropped rather than followed.
    """

    def __init__(self, key: str):
        parts = []
        for seg in str(key).replace("\\", "/").split("/"):
            if not seg or seg == "." or seg == ".." or ":" in seg:
                continue
            parts.append(seg)
        self.parts = tuple(parts)


def from_env(env: dict | None = None) -> Storage:
    """Build the backend named by the environment.

    AI_SIGNAL_STORAGE   "local" (default) or "gcs"
    AI_SIGNAL_BUCKET    bucket name, required when AI_SIGNAL_STORAGE=gcs
    AI_SIGNAL_PREFIX    optional key prefix inside the bucket
    AI_SIGNAL_DATA_DIR  local root, default "./data/ai-signal"
    """
    env = os.environ if env is None else env
    kind = (env.get("AI_SIGNAL_STORAGE") or "local").strip().lower()

    if kind == "gcs":
        bucket = (env.get("AI_SIGNAL_BUCKET") or "").strip()
        if not bucket:
            raise RuntimeError("AI_SIGNAL_STORAGE=gcs requires AI_SIGNAL_BUCKET")
        return GcsStorage(bucket, env.get("AI_SIGNAL_PREFIX", ""))

    if kind == "local":
        return LocalStorage(env.get("AI_SIGNAL_DATA_DIR") or "data/ai-signal")

    raise RuntimeError(f"unknown AI_SIGNAL_STORAGE {kind!r}: expected 'local' or 'gcs'")


def describe(storage: Storage) -> str:
    """One line naming where state is being kept, for the startup banner."""
    if isinstance(storage, GcsStorage):
        where = f"gs://{storage.bucket_name}"
        return f"{where}/{storage.prefix}" if storage.prefix else where
    if isinstance(storage, LocalStorage):
        return str(storage.root.resolve())
    return type(storage).__name__


def archive_key(now: datetime, disambiguator: str = "") -> str:
    """The object key one sweep's report is saved under.

    Down to the microsecond, not just the minute: a manual Force Refresh
    and the scheduled sweep both save through this same function, and two
    people clicking the button close together must not silently overwrite
    one another's report. `disambiguator` exists for `save_report` below,
    for the rarer case where even that is not enough.
    """
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    suffix = f"-{disambiguator}" if disambiguator else ""
    return f"{ARCHIVE_PREFIX}sweep-{stamp}{suffix}.json"


def save_report(storage: Storage, report: dict, now: datetime) -> str:
    """Persists one sweep's report -- manual or scheduled alike -- so every
    run becomes part of the shared, durable history anyone with the app
    open can read back, not just the one browser tab that triggered it.
    Returns the key it was saved under.

    Microsecond precision alone was measured to still collide: three Force
    Refresh calls fired back to back in a test landed on the same key, and
    the second write silently overwrote the first. Checked, not assumed:
    if the key this run would use is already taken, a numbered suffix is
    added until a free one is found, so a save can never overwrite an
    existing report.
    """
    key = archive_key(now)
    attempt = 2
    while storage.read_text(key) is not None:
        key = archive_key(now, disambiguator=str(attempt))
        attempt += 1
    storage.write_text(key, json.dumps(report, indent=2, ensure_ascii=False))
    return key


def list_archive(storage: Storage, limit: int = 60) -> list[dict]:
    """The most recent `limit` saved reports, newest first.

    A report that fails to parse -- a partial write, caught mid-upload --
    is skipped rather than raised: one bad file must not take down the
    archive view for everyone else.
    """
    keys = sorted(storage.list_keys(ARCHIVE_PREFIX), reverse=True)[:limit]
    reports = []
    for key in keys:
        raw = storage.read_text(key)
        if raw is None:
            continue
        try:
            reports.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return reports
