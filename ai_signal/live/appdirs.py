"""Where this build's own files are, and where a user's own data goes.

Two different questions, answered separately on purpose:

    app_root()       -- this build's own files: the dashboard, its artwork,
                        and the seed watchlist. Read-only, part of
                        the install, and gone the moment it's upgraded
                        or uninstalled.
    user_data_dir()  -- where a person's own data defaults to. Writable,
                        survives an upgrade or reinstall, and is never
                        inside app_root() -- which, for the packaged
                        desktop build, is ordinarily somewhere under
                        Program Files an unprivileged user account
                        cannot write to at all.

Both understand running frozen under PyInstaller (see desktop/) as well
as running from a checked-out source tree, since this one module backs
local_server.py, watchlist_store.py, and the desktop launcher alike --
three places that would otherwise each have to work out "where am I"
for themselves, and had, before this existed, each assumed the same
`Path(__file__).resolve().parent.parent.parent` depth independently.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Directory holding this build's own bundled files."""
    if getattr(sys, "frozen", False):
        # PyInstaller sets sys._MEIPASS in both onefile (an extracted temp
        # directory, recreated each launch) and onedir (the folder the
        # exe itself sits in) builds -- the documented way to find
        # bundled data files. `__file__` is not reliable here: a frozen
        # module's source lives inside a bundled archive, not at a real,
        # walkable path on disk.
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass).resolve() if meipass else Path(sys.executable).resolve().parent
    # ai_signal/live/appdirs.py -> ai_signal/live -> ai_signal -> repo root.
    return Path(__file__).resolve().parent.parent.parent


def user_data_dir(app_name: str = "AISignal") -> Path:
    """Where a person's own local data defaults to, per platform
    convention. Not created here -- callers that actually write to it
    are responsible for `mkdir(parents=True, exist_ok=True)`.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / app_name
