"""Tests for ai_signal.live.appdirs -- no network, no real filesystem
writes (user_data_dir only computes a path; callers create it).

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live import appdirs


class TestAppRoot(unittest.TestCase):
    def test_unfrozen_resolves_to_the_repo_root(self):
        """Not running under PyInstaller: the existing dev-mode behaviour,
        unchanged -- three parents up from this module's own file."""
        root = appdirs.app_root()
        self.assertTrue((root / "ai_signal").is_dir())
        self.assertTrue((root / "watchlist.json").is_file())

    def test_frozen_with_meipass_uses_it(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", r"C:\fake\bundle", create=True):
            self.assertEqual(appdirs.app_root(), Path(r"C:\fake\bundle").resolve())

    def test_frozen_without_meipass_falls_back_to_the_executable_dir(self):
        """Onedir builds without _MEIPASS set (defensive fallback; real
        PyInstaller sets _MEIPASS in both onefile and onedir modes, but
        nothing here should assume that forever)."""
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", r"C:\Program Files\AISignal\AISignal.exe"):
            self.assertEqual(
                appdirs.app_root(),
                Path(r"C:\Program Files\AISignal").resolve(),
            )


class TestUserDataDir(unittest.TestCase):
    def test_windows_prefers_localappdata(self):
        with patch.object(sys, "platform", "win32"), \
             patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}, clear=False):
            self.assertEqual(
                appdirs.user_data_dir(), Path(r"C:\Users\x\AppData\Local") / "AISignal"
            )

    def test_windows_falls_back_to_appdata_without_localappdata(self):
        import os
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        env["APPDATA"] = r"C:\Users\x\AppData\Roaming"
        with patch.object(sys, "platform", "win32"), \
             patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                appdirs.user_data_dir(), Path(r"C:\Users\x\AppData\Roaming") / "AISignal"
            )

    def test_macos_uses_application_support(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                appdirs.user_data_dir(),
                Path.home() / "Library" / "Application Support" / "AISignal",
            )

    def test_app_name_is_respected(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertTrue(str(appdirs.user_data_dir("OtherApp")).endswith("OtherApp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
