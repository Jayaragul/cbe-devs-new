import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import build_pages


class TestPagesBuild(unittest.TestCase):
    def test_build_compiles_a_snapshot_and_carries_forward_recent_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = datetime.now(timezone.utc) - timedelta(days=2)
            old_report = {"date": old.date().isoformat(), "demoData": False}
            history = root / "previous.json"
            history.write_text(json.dumps({"reports": [old_report]}), encoding="utf-8")

            def run_sweep(store, now, *, require_rss):
                self.assertTrue(require_rss)
                seed = json.loads(store.read_text(build_pages.storage.WATCHLIST_KEY))
                self.assertEqual(seed["windowHours"], 168)
                return {"date": now.date().isoformat(), "totalVerified": 20, "demoData": False}, []

            output = root / "site"
            with patch.object(build_pages, "run_sweep", side_effect=run_sweep):
                self.assertEqual(build_pages.build(output, history), 20)

            dashboard = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('<meta name="ai-signal-mode" content="snapshot">', dashboard)
            self.assertTrue((output / "assets/cbe-devs-mark.png").is_file())
            self.assertEqual({path.name for path in output.iterdir()}, {"index.html", "assets", "data"})
            snapshot = json.loads((output / "data/briefing.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["reports"][0]["totalVerified"], 20)
            self.assertEqual(snapshot["reports"][1], old_report)

    def test_archive_keeps_latest_per_day_and_omits_demo_and_expired_reports(self):
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        newest = {"date": "2026-11-27", "demoData": False, "totalVerified": 8}
        previous = [
            {"date": "2026-11-27", "demoData": False, "totalVerified": 3},
            {"date": "2026-11-26", "demoData": True},
            {"date": "2026-11-14", "demoData": False},
            {"date": "2026-11-13", "demoData": False},
        ]
        self.assertEqual(build_pages.archive_reports(newest, previous, now), [newest, previous[2]])
