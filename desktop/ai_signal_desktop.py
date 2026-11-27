"""Start the packaged AI Signal server and open its dashboard."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

# Mirrors local_server.py's own sys.path setup: harmless when frozen
# (PyInstaller's bundled imports do not need it), necessary when this
# file is run straight from a source checkout for testing.
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from ai_signal.live import appdirs  # noqa: E402


def _configure_environment() -> None:
    data_dir = appdirs.user_data_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AI_SIGNAL_STORAGE", "local")
    os.environ.setdefault("AI_SIGNAL_DATA_DIR", str(data_dir))
    os.environ.setdefault("AI_SIGNAL_GOOGLE_NEWS", "0")


def _wait_then_open_browser(port: int, path: str, timeout_s: float = 15.0) -> None:
    """Polls the port rather than sleeping a fixed amount: opens the
    browser the moment the server actually answers, whether that takes
    50ms or several seconds on a slow machine, and simply gives up
    quietly if nothing ever does (main() will have printed why by then).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        return
    webbrowser.open(f"http://127.0.0.1:{port}/{path}")


def main() -> None:
    # The whole body is one try/except: a console build exists specifically
    # so a problem has somewhere to be *seen* -- an uncaught exception
    # reaching the top under a frozen exe otherwise closes this window
    # immediately, traceback and all, before anyone can read it. Measured
    # gap, not a hypothetical one: the original version of this function
    # only guarded the one call it expected to fail (the server's own
    # port-bind), so a problem anywhere else -- including in
    # _configure_environment() below, which runs before that guard even
    # starts -- still vanished without a trace.
    try:
        _configure_environment()

        # Imported only now: local_server reads several of the environment
        # variables set above (directly, and indirectly through storage.py)
        # the moment its own functions run, so there is no ordering hazard
        # in waiting -- this is simply so the intent above reads
        # top-to-bottom.
        from ai_signal.live import local_server

        port = int(os.environ.get("PORT") or local_server.DEFAULT_PORT)
        threading.Thread(
            target=_wait_then_open_browser,
            args=(port, "index.html"),
            daemon=True,
        ).start()

        print("AI Signal is starting -- your browser will open automatically.")
        print("This window is AI Signal's server. Closing it stops AI Signal.\n")
        local_server.main()
    except OSError as exc:
        print(f"\nAI Signal could not start: {exc}")
        print(
            "If AI Signal is already open in another window, check your browser -- "
            "it may already be there."
        )
        input("Press Enter to close this window...")
    except Exception:
        print("\nAI Signal hit an unexpected problem and needs to stop.")
        traceback.print_exc()
        input("\nPress Enter to close this window...")


if __name__ == "__main__":
    main()
