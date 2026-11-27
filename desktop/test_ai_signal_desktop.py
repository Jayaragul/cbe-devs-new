"""Tests for the desktop launcher's own logic -- no real server started,
no real browser opened (webbrowser.open is mocked throughout).

Lives alongside ai_signal_desktop.py rather than in tests/ since it is
specific to the desktop distribution, not the library the rest of the
test suite covers -- but runs the same way:

Run: python -m unittest discover -s desktop -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_signal_desktop


class TestConfigureEnvironment(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("AI_SIGNAL_STORAGE", "AI_SIGNAL_DATA_DIR", "AI_SIGNAL_GOOGLE_NEWS")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_sets_local_storage_and_leaves_google_news_off(self):
        with patch("ai_signal.live.appdirs.user_data_dir") as fake_dir:
            fake_dir.return_value = Path(os.environ.get("TEMP", "/tmp")) / "ai-signal-test-appdata"
            ai_signal_desktop._configure_environment()
        self.assertEqual(os.environ["AI_SIGNAL_STORAGE"], "local")
        self.assertEqual(os.environ["AI_SIGNAL_GOOGLE_NEWS"], "0")
        self.assertTrue(os.environ["AI_SIGNAL_DATA_DIR"].endswith("data"))
        self.assertTrue(Path(os.environ["AI_SIGNAL_DATA_DIR"]).is_dir())

    def test_does_not_override_an_operator_supplied_value(self):
        os.environ["AI_SIGNAL_GOOGLE_NEWS"] = "1"
        with patch("ai_signal.live.appdirs.user_data_dir") as fake_dir:
            fake_dir.return_value = Path(os.environ.get("TEMP", "/tmp")) / "ai-signal-test-appdata2"
            ai_signal_desktop._configure_environment()
        self.assertEqual(os.environ["AI_SIGNAL_GOOGLE_NEWS"], "1")


class TestWaitThenOpenBrowser(unittest.TestCase):
    def test_opens_the_browser_once_something_answers_the_port(self):
        import socketserver
        import threading

        with socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler) as srv:
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with patch("webbrowser.open") as fake_open:
                    ai_signal_desktop._wait_then_open_browser(port, "index.html", timeout_s=5)
                fake_open.assert_called_once_with(
                    f"http://127.0.0.1:{port}/index.html"
                )
            finally:
                srv.shutdown()

    def test_gives_up_quietly_if_nothing_ever_answers(self):
        with patch("webbrowser.open") as fake_open:
            ai_signal_desktop._wait_then_open_browser(1, "x.html", timeout_s=0.3)
        fake_open.assert_not_called()


class TestMainNeverVanishesOnACrash(unittest.TestCase):
    """The whole point of keeping a console window: an unexpected failure
    must be readable, not a window that flashes shut. Patches
    _configure_environment itself, since that is the one call that runs
    before any other guard in main() even starts -- the specific gap the
    original version of this function had.
    """

    def test_a_crash_in_configure_environment_is_shown_not_swallowed(self):
        with patch.object(
            ai_signal_desktop, "_configure_environment",
            side_effect=RuntimeError("disk is full"),
        ), patch("builtins.input", return_value="") as fake_input, \
           patch("builtins.print") as fake_print:
            ai_signal_desktop.main()  # must not raise
        fake_input.assert_called_once()
        self.assertTrue(
            any("unexpected problem" in str(c.args) for c in fake_print.call_args_list)
        )

    def test_an_oserror_still_gets_its_own_friendlier_message(self):
        # _wait_then_open_browser itself is stubbed out, not just
        # webbrowser.open: main() starts it in a real daemon thread that
        # `with patch(...)` does not wait for, so a thread left polling a
        # real socket for up to 15s would otherwise outlive this test and
        # leak a stray webbrowser.open call into whichever test happens to
        # be running when it finally fires -- caught exactly that way
        # once already.
        with patch.object(ai_signal_desktop, "_configure_environment"), \
             patch.object(ai_signal_desktop, "_wait_then_open_browser"), \
             patch("ai_signal.live.local_server.main", side_effect=OSError("port in use")), \
             patch("builtins.input", return_value="") as fake_input, \
             patch("builtins.print") as fake_print:
            ai_signal_desktop.main()
        fake_input.assert_called_once()
        self.assertTrue(
            any("could not start" in str(c.args) for c in fake_print.call_args_list)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
