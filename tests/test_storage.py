"""Tests for the storage backend and the watchlist document -- no network.

`GcsStorage` is deliberately not exercised against a real bucket: the
contract these tests pin down is the one both backends implement, and the
Cloud Storage client has its own test suite. What is tested here is the
part we own -- key safety, seeding, replacement, and the env wiring.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.live import storage as store
from ai_signal.live import watchlist_store
from ai_signal.model import Bucket


class TestLocalStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = store.LocalStorage(self.tmp.name)

    def test_a_missing_key_reads_as_none_rather_than_raising(self):
        self.assertIsNone(self.s.read_text("archive/nothing.json"))

    def test_write_then_read_round_trips_unicode(self):
        self.s.write_text("watchlist/watchlist.json", '{"a": "Küçük ₹"}')
        self.assertEqual(self.s.read_text("watchlist/watchlist.json"), '{"a": "Küçük ₹"}')

    def test_writing_creates_intermediate_folders(self):
        self.s.write_text("logs/deep/er/sweep.log", "ok")
        self.assertTrue((Path(self.tmp.name) / "logs" / "deep" / "er" / "sweep.log").is_file())

    def test_list_keys_returns_only_that_prefix(self):
        self.s.write_text("archive/a.json", "1")
        self.s.write_text("archive/b.json", "2")
        self.s.write_text("logs/c.log", "3")
        self.assertEqual(self.s.list_keys("archive/"), ["archive/a.json", "archive/b.json"])

    def test_list_keys_on_an_empty_prefix_is_empty_not_an_error(self):
        self.assertEqual(self.s.list_keys("archive/"), [])

    def test_delete_is_idempotent(self):
        self.s.write_text("archive/a.json", "1")
        self.s.delete("archive/a.json")
        self.s.delete("archive/a.json")  # must not raise
        self.assertIsNone(self.s.read_text("archive/a.json"))

    def test_a_key_cannot_escape_the_root(self):
        """A key is an object name, not a path. "../" in one must not write
        outside the data directory."""
        self.s.write_text("../../escaped.json", "nope")
        self.assertFalse((Path(self.tmp.name).parent.parent / "escaped.json").exists())
        self.assertEqual(self.s.read_text("escaped.json"), "nope")


class TestStorageFromEnv(unittest.TestCase):
    def test_local_is_the_default(self):
        self.assertIsInstance(store.from_env({}), store.LocalStorage)

    def test_local_root_comes_from_the_environment(self):
        s = store.from_env({"AI_SIGNAL_STORAGE": "local", "AI_SIGNAL_DATA_DIR": "somewhere"})
        self.assertEqual(s.root, Path("somewhere"))

    def test_gcs_without_a_bucket_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            store.from_env({"AI_SIGNAL_STORAGE": "gcs"})
        self.assertIn("AI_SIGNAL_BUCKET", str(caught.exception))

    def test_an_unknown_backend_names_what_was_expected(self):
        with self.assertRaises(RuntimeError) as caught:
            store.from_env({"AI_SIGNAL_STORAGE": "s3"})
        self.assertIn("local", str(caught.exception))


class TestWatchlistDocument(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = store.LocalStorage(self.tmp.name)

    def test_first_read_seeds_storage_from_the_shipped_file(self):
        doc = watchlist_store.load_document(self.s)
        self.assertTrue(doc["terms"])
        # And it is now persisted, not re-seeded on every read.
        self.assertIsNotNone(self.s.read_text(store.WATCHLIST_KEY))

    def test_an_edit_survives_and_is_not_overwritten_by_the_seed(self):
        watchlist_store.load_document(self.s)  # seed
        watchlist_store.save_document(self.s, {"terms": [
            {"name": "Only This", "category": "clients", "enabled": True}
        ]})
        again = watchlist_store.load_document(self.s)
        self.assertEqual([t["name"] for t in again["terms"]], ["Only This"])

    def test_a_document_without_terms_is_refused(self):
        with self.assertRaises(ValueError):
            watchlist_store.save_document(self.s, {"nope": []})

    def test_corrupt_stored_json_says_so_rather_than_seeding_over_it(self):
        """Silently replacing an unreadable watchlist would destroy whatever
        a user had actually saved."""
        self.s.write_text(store.WATCHLIST_KEY, "{not json")
        with self.assertRaises(ValueError):
            watchlist_store.load_document(self.s)


class TestDocumentToTerms(unittest.TestCase):
    def terms(self, rows):
        return watchlist_store.to_terms({"terms": rows})

    def test_disabled_rows_are_dropped(self):
        got = self.terms([
            {"name": "On", "category": "clients", "enabled": True},
            {"name": "Off", "category": "clients", "enabled": False},
        ])
        self.assertEqual([t.name for t in got], ["On"])

    def test_a_blank_name_is_dropped(self):
        self.assertEqual(self.terms([{"name": "   ", "category": "clients"}]), [])

    def test_the_nse_symbol_is_read_in_either_spelling(self):
        camel = self.terms([{"name": "A", "category": "clients", "nseSymbol": "KITEX"}])
        snake = self.terms([{"name": "B", "category": "clients", "nse_symbol": "KITEX"}])
        self.assertEqual(camel[0].nse_symbol, "KITEX")
        self.assertEqual(snake[0].nse_symbol, "KITEX")

    def test_categories_map_to_buckets_and_default_to_clients(self):
        got = self.terms([
            {"name": "A", "category": "competitors"},
            {"name": "B", "category": "topics"},
            {"name": "C", "category": "nonsense"},
        ])
        self.assertEqual(
            [t.bucket for t in got],
            [Bucket.COMPETITORS, Bucket.TOPICS, Bucket.CLIENTS],
        )


class TestWindowHours(unittest.TestCase):
    def test_reads_a_valid_value(self):
        self.assertEqual(watchlist_store.window_hours({"windowHours": 168}), 168)

    def test_missing_falls_back_to_the_default(self):
        self.assertEqual(watchlist_store.window_hours({}), watchlist_store.DEFAULT_WINDOW_HOURS)

    def test_a_custom_default_is_honoured(self):
        self.assertEqual(watchlist_store.window_hours({}, default=48), 48)

    def test_garbage_falls_back_rather_than_raising(self):
        """A hand-edited or corrupted document must not take the sweep down
        over a display setting -- same spirit as to_terms() dropping a
        malformed row instead of raising."""
        for bad in ("not a number", None, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(
                    watchlist_store.window_hours({"windowHours": bad}),
                    watchlist_store.DEFAULT_WINDOW_HOURS,
                )

    def test_zero_and_negative_fall_back(self):
        for bad in (0, -1, -168):
            with self.subTest(bad=bad):
                self.assertEqual(
                    watchlist_store.window_hours({"windowHours": bad}),
                    watchlist_store.DEFAULT_WINDOW_HOURS,
                )

    def test_absurdly_large_falls_back(self):
        """Capped at the archive's own 30-day retention -- a window wider
        than how long reports are kept would report on deleted history."""
        self.assertEqual(
            watchlist_store.window_hours({"windowHours": 24 * 365}),
            watchlist_store.DEFAULT_WINDOW_HOURS,
        )

    def test_the_maximum_itself_is_accepted(self):
        from ai_signal import pipeline
        self.assertEqual(
            watchlist_store.window_hours({"windowHours": pipeline.MAX_WINDOW_HOURS}),
            pipeline.MAX_WINDOW_HOURS,
        )


class TestArchive(unittest.TestCase):
    """save_report/list_archive: the shared path a manual Force Refresh and
    the scheduled sweep both now go through, so every run -- not just the
    daily one -- becomes part of the durable, shared history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = store.LocalStorage(self.tmp.name)

    def at(self, *args, **kwargs):
        return datetime(*args, tzinfo=timezone.utc, **kwargs)

    def test_save_report_returns_a_key_under_the_archive_prefix(self):
        key = store.save_report(self.s, {"totalVerified": 1}, self.at(2026, 9, 29, 6, 0, 0))
        self.assertTrue(key.startswith(store.ARCHIVE_PREFIX))
        self.assertIsNotNone(self.s.read_text(key))

    def test_two_saves_in_the_same_minute_do_not_collide(self):
        """Manual Force Refresh runs can happen seconds apart -- the key
        must not be minute-granular, or the second click's report would
        silently overwrite the first's."""
        a = store.save_report(self.s, {"totalVerified": 1}, self.at(2026, 9, 29, 6, 0, 10))
        b = store.save_report(self.s, {"totalVerified": 2}, self.at(2026, 9, 29, 6, 0, 45))
        self.assertNotEqual(a, b)
        self.assertEqual(json.loads(self.s.read_text(a))["totalVerified"], 1)
        self.assertEqual(json.loads(self.s.read_text(b))["totalVerified"], 2)

    def test_list_archive_is_newest_first(self):
        store.save_report(self.s, {"totalVerified": 1}, self.at(2026, 9, 29, 6, 0, 0))
        store.save_report(self.s, {"totalVerified": 2}, self.at(2026, 9, 29, 8, 0, 0))
        store.save_report(self.s, {"totalVerified": 3}, self.at(2026, 9, 29, 7, 0, 0))
        got = store.list_archive(self.s)
        self.assertEqual([r["totalVerified"] for r in got], [2, 3, 1])

    def test_list_archive_on_an_empty_bucket_is_empty_not_an_error(self):
        self.assertEqual(store.list_archive(self.s), [])

    def test_list_archive_respects_the_limit(self):
        for i in range(5):
            store.save_report(self.s, {"totalVerified": i}, self.at(2026, 9, 29, 6, i, 0))
        self.assertEqual(len(store.list_archive(self.s, limit=2)), 2)

    def test_two_saves_at_the_exact_same_instant_still_do_not_collide(self):
        """Measured, not hypothetical: three rapid Force Refresh calls in a
        real HTTP-level test landed on an identical microsecond-precision
        key, and the second save silently overwrote the first. save_report
        must check for that and disambiguate rather than assume it away."""
        now = self.at(2026, 9, 29, 6, 0, 0, 123456)
        a = store.save_report(self.s, {"totalVerified": 1}, now)
        b = store.save_report(self.s, {"totalVerified": 2}, now)
        c = store.save_report(self.s, {"totalVerified": 3}, now)
        self.assertEqual(len({a, b, c}), 3)
        got = sorted(json.loads(self.s.read_text(k))["totalVerified"] for k in (a, b, c))
        self.assertEqual(got, [1, 2, 3])

    def test_a_corrupt_report_is_skipped_not_fatal(self):
        good = store.save_report(self.s, {"totalVerified": 1}, self.at(2026, 9, 29, 6, 0, 0))
        self.s.write_text(f"{store.ARCHIVE_PREFIX}sweep-20260929-070000.json", "{not json")
        got = store.list_archive(self.s)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["totalVerified"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
