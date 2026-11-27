# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AI Signal desktop build.

Produces a one-directory bundle (dist/AISignal/), not --onefile: the dashboard, artwork,
and watchlist.json are bundled under
dist/AISignal/_internal/ -- found at runtime via appdirs.app_root(), see
that module's docstring -- rather than being re-extracted to a fresh
temp directory on every single launch, which --onefile would do.

Build with:
    pyinstaller desktop/AISignal.spec --noconfirm

Output: dist/AISignal/AISignal.exe plus its _internal/ support files. Hand the
whole dist/AISignal/ folder to Inno Setup (see desktop/ai_signal_setup.iss) to
produce the actual one-file installer.

Paths below are computed from SPECPATH (this file's own directory, set
by PyInstaller), not hardcoded to wherever this was first built -- the
generated spec PyInstaller writes via its CLI flags bakes in whatever
absolute path the machine that ran it happened to use, which breaks the
moment anyone else checks out the repo somewhere else.
"""

from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(REPO_ROOT / "desktop" / "ai_signal_desktop.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[
        (str(REPO_ROOT / "index.html"), "."),
        (str(REPO_ROOT / "assets" / "signal-orbit.webp"), "assets"),
        (str(REPO_ROOT / "assets" / "dashboard.css"), "assets"),
        (str(REPO_ROOT / "assets" / "cbe-devs-mark.png"), "assets"),
        (str(REPO_ROOT / "watchlist.json"), "."),
    ],
    # local_server.py imports these lazily, inside function bodies, not
    # at module level (brave.py / nse_filings.py / google_news.py are
    # each only needed on one particular code path) -- explicit here as
    # a safety net against PyInstaller's static import analysis missing
    # one, rather than assuming it always catches every lazy import.
    hiddenimports=[
        "ai_signal.live.google_news",
        "ai_signal.live.nse_filings",
        "ai_signal.live.brave",
        "ai_signal.live.demo_provider",
        "ai_signal.live.http_fetcher",
        "ai_signal.live.rss_provider",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # This build is local-storage-only (see ai_signal_desktop.py), so the GCS
    # backend's dependency tree is genuinely dead weight here -- storage.py
    # imports google.cloud.storage lazily, inside GcsStorage.__init__, so
    # PyInstaller's static analysis still finds and bundles it (and its own
    # grpc/cryptography/pywin32 dependencies) even though that code path
    # never runs. Measured: excluding these took the build from 50MB to
    # 20MB, and a real Force Refresh against the trimmed build still
    # completed real HTTPS requests correctly (RSS feeds, Google News) --
    # Python's own ssl module, not the removed `cryptography` package, is
    # what backs urllib's HTTPS support, so this does not touch it.
    excludes=[
        "grpc",
        "google.cloud",
        "google.auth",
        "google.api_core",
        "google.oauth2",
        "google_crc32c",
        "cryptography",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AISignal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AISignal",
)
