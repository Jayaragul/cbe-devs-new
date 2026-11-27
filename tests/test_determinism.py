"""Proofs that the pipeline is deterministic, and that runs are reproducible.

Three distinct claims are tested here, because they are not the same claim:

  1. No AI model, no randomness, no hidden clock. Checked structurally.
  2. Same inputs -> byte-identical report. Including under a different
     PYTHONHASHSEED, which is the classic way set/dict iteration order leaks
     into output and makes a "deterministic" program vary between processes.
  3. A recorded run replays exactly, offline.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import os
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_signal import dedup, pipeline, record, temporal
from ai_signal.model import Bucket, Candidate, SourceType
from ai_signal.providers import PageContent, StaticFetcher, StaticProvider

IST = temporal.IST
NOW = datetime(2026, 9, 7, 11, 18, 46, tzinfo=IST)

PKG = ROOT / "ai_signal"

#: Modules that must never appear in the package: inference engines, numeric
#: stacks that carry their own nondeterminism, and network clients.
BANNED_IMPORTS = {
    "openai", "anthropic", "google", "cohere", "mistralai", "ollama",
    "torch", "tensorflow", "transformers", "sentence_transformers",
    "numpy", "scipy", "sklearn", "faiss",
    "requests", "httpx", "aiohttp", "urllib3", "socket",
    "random", "secrets",
}


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


class TestNoModelsNoRandomness(unittest.TestCase):
    """Claim 1, enforced structurally rather than by reading the code."""

    def _imported_names(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_no_module_imports_a_model_or_randomness_library(self):
        for path in sorted(PKG.glob("*.py")):
            with self.subTest(module=path.name):
                offenders = self._imported_names(path) & BANNED_IMPORTS
                self.assertEqual(
                    offenders, set(),
                    msg=f"{path.name} imports {sorted(offenders)}",
                )

    def test_package_reads_no_wall_clock(self):
        """`now` must arrive as an argument, never be read internally."""
        for path in sorted(PKG.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for forbidden in ("datetime.now(", "datetime.today(", "time.time("):
                    self.assertNotIn(forbidden, src)

    def test_pipeline_takes_now_as_a_parameter(self):
        import inspect

        self.assertIn("now", inspect.signature(pipeline.run).parameters)


class TestTotalOrdering(unittest.TestCase):
    """Ties must be broken by a stable key, not by input order."""

    def make(self, url: str) -> Candidate:
        # Identical on every ranking field, so only the URL can separate them.
        return Candidate(
            url=url,
            title="KPR Mill commissions garment unit",
            bucket=Bucket.CLIENTS,
            body="KPR Mill announced it commissioned a garment unit in Tiruppur.",
            entities=["K.P.R. Mill"],
            source_type=SourceType.TRADE_PUBLICATION,
            published_at=at(7, 9, 0),
        )

    def test_canonical_choice_is_independent_of_input_order(self):
        a, b, c = self.make("https://x.example/a"), self.make(
            "https://x.example/b"
        ), self.make("https://x.example/c")
        forward, _ = dedup.pick_canonical([a, b, c])
        backward, _ = dedup.pick_canonical([c, b, a])
        self.assertEqual(forward.url, backward.url)
        self.assertEqual(forward.url, "https://x.example/a")


class _Fixture:
    """The 7 Sep 2026 sweep shape, reused by the determinism tests."""

    URL_HIT = (
        "https://www.fibre2fashion.com/news/apparel-news/"
        "bangladesh-s-rmg-sector-bets-on-technology-313242-newsdetails.htm"
    )
    URL_STALE = (
        "https://www.fibre2fashion.com/news/upcoming-technology-product-news/"
        "coats-digital-debuts-gsd-realmotion-312778-newsdetails.htm"
    )
    URL_DUP = "https://aggregator.example/bangladesh-rmg-technology-productivity"
    TOPIC_Q = '("garment manufacturing") AND ("AI")'

    BODY_HIT = (
        "Bangladesh apparel exporters are adopting digital monitoring, "
        "data-driven production and AI tools to raise productivity in garment "
        "manufacturing. Factories that deployed the systems reported gains of "
        "up to 25 per cent, and several have adopted automated defect "
        "detection on the textile shop floor."
    )

    @classmethod
    def terms(cls):
        return [
            pipeline.WatchTerm("K.P.R. Mill", Bucket.CLIENTS, ["KPR Mill"]),
            pipeline.WatchTerm("Coats Digital", Bucket.COMPETITORS, ["GSDCost"]),
            pipeline.WatchTerm(cls.TOPIC_Q, Bucket.TOPICS),
        ]

    @classmethod
    def provider(cls):
        hit = Candidate(url=cls.URL_HIT, title="Bangladesh's RMG sector bets on "
                        "technology for a productivity edge", bucket=Bucket.TOPICS)
        dup = Candidate(url=cls.URL_DUP, title="Bangladesh's RMG sector bets on "
                        "technology for a productivity edge", bucket=Bucket.TOPICS)
        stale = Candidate(url=cls.URL_STALE, title="Coats Digital debuts GSD "
                          "RealMotion to transform time studies",
                          bucket=Bucket.COMPETITORS)
        return StaticProvider(
            name="fixture",
            results={
                cls.TOPIC_Q: [hit, dup],
                '"Coats Digital"': [stale],
            },
        )

    @classmethod
    def fetcher(cls):
        return StaticFetcher(
            pages={
                cls.URL_HIT: PageContent(
                    body=cls.BODY_HIT,
                    published=temporal.DateFinding(at(7), True, "byline"),
                    source_type=SourceType.TRADE_PUBLICATION,
                    source_name="Fibre2Fashion",
                ),
                cls.URL_DUP: PageContent(
                    body=cls.BODY_HIT,
                    published=temporal.DateFinding(at(7), True, "byline"),
                    source_type=SourceType.AGGREGATOR,
                    source_name="aggregator.example",
                ),
                cls.URL_STALE: PageContent(
                    body=(
                        "Coats Digital announced GSD RealMotion, which uses AI "
                        "and computer vision to turn phone video into method "
                        "studies for apparel and garment engineers."
                    ),
                    published=temporal.DateFinding(
                        datetime(2026, 8, 19, tzinfo=IST), True, "byline"
                    ),
                    source_type=SourceType.TRADE_PUBLICATION,
                    source_name="Fibre2Fashion",
                ),
            }
        )

    @classmethod
    def run(cls):
        return pipeline.run(cls.terms(), [cls.provider()], cls.fetcher(), NOW)


class TestRepeatable(unittest.TestCase):
    """Claim 2: same inputs, identical decisions."""

    def test_two_runs_in_one_process_have_the_same_digest(self):
        a = record.report_digest(_Fixture.run())
        b = record.report_digest(_Fixture.run())
        self.assertEqual(a, b)

    def test_ten_runs_never_diverge(self):
        digests = {record.report_digest(_Fixture.run()) for _ in range(10)}
        self.assertEqual(len(digests), 1)

    def test_digest_is_sensitive_to_a_real_change(self):
        """A determinism test that always passes proves nothing, so confirm
        the digest actually moves when the outcome moves."""
        baseline = record.report_digest(_Fixture.run())
        shifted = pipeline.run(
            _Fixture.terms(), [_Fixture.provider()], _Fixture.fetcher(),
            NOW, pipeline.SweepConfig(window_hours=24 * 30),  # admits the Aug item
        )
        self.assertNotEqual(baseline, record.report_digest(shifted))

    def test_duplicate_is_collapsed_to_the_trade_source(self):
        """Guards the fixture itself: the aggregator copy must not win."""
        res = _Fixture.run()
        kept = res.included[Bucket.TOPICS]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].candidate.url, _Fixture.URL_HIT)
        self.assertEqual(kept[0].duplicates, [_Fixture.URL_DUP])


class TestHashSeedIndependence(unittest.TestCase):
    """Claim 2, across processes.

    Python randomises string hashing per process, so set and dict iteration
    order can differ between runs. If any of that order reached the output,
    the digest would change between seeds while staying stable within one
    process -- the subtlest form of this bug, and invisible to the tests
    above.
    """

    SCRIPT = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tests.test_determinism import _Fixture\n"
        "from ai_signal import record\n"
        "print(record.report_digest(_Fixture.run()))\n"
    )

    def _digest_with_seed(self, seed: str) -> str:
        # `-S` skips site-packages entirely. The package is stdlib-only, so
        # this both isolates the run from unrelated installs on this machine
        # and independently proves the no-dependency claim: if any module
        # reached for a third-party import, this would fail outright.
        # The parent environment is inherited rather than replaced, because
        # a bare env breaks socket initialisation on Windows.
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-S", "-c", self.SCRIPT % str(ROOT)],
            capture_output=True, text=True, cwd=str(ROOT), env=env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        return proc.stdout.strip()

    def test_digest_is_identical_under_different_hash_seeds(self):
        digests = {self._digest_with_seed(s) for s in ("0", "1", "12345", "99991")}
        self.assertEqual(
            len(digests), 1,
            msg=f"set/dict iteration order is leaking into output: {digests}",
        )


class TestReplay(unittest.TestCase):
    """Claim 3: a recorded run re-executes exactly, offline."""

    def _record(self) -> tuple[record.Cassette, str]:
        cassette = record.Cassette(now=NOW)
        provider = record.RecordingProvider(_Fixture.provider(), cassette)
        fetcher = record.RecordingFetcher(_Fixture.fetcher(), cassette)
        result = pipeline.run(_Fixture.terms(), [provider], fetcher, NOW)
        return cassette, record.report_digest(result)

    def test_replay_reproduces_the_original_digest(self):
        cassette, original = self._record()
        replayed = pipeline.run(
            _Fixture.terms(),
            [record.ReplayProvider("fixture", cassette)],
            record.ReplayFetcher(cassette),
            NOW,
        )
        self.assertEqual(record.report_digest(replayed), original)

    def test_cassette_survives_a_round_trip_through_disk(self):
        cassette, original = self._record()
        with tempfile.TemporaryDirectory() as tmp:
            path = cassette.save(Path(tmp) / "sweep.json")
            reloaded = record.Cassette.load(path)
        self.assertEqual(reloaded.digest(), cassette.digest())
        replayed = pipeline.run(
            _Fixture.terms(),
            [record.ReplayProvider("fixture", reloaded)],
            record.ReplayFetcher(reloaded),
            reloaded.now,
        )
        self.assertEqual(record.report_digest(replayed), original)

    def test_cassette_json_is_canonical(self):
        cassette, _ = self._record()
        once, twice = cassette.to_json(), cassette.to_json()
        self.assertEqual(once, twice)
        # Sorted keys, so the text is diffable and hashable.
        self.assertEqual(json.loads(once), json.loads(twice))
        self.assertEqual(cassette.digest(), record.Cassette.from_json(once).digest())

    def test_cassette_records_what_was_read(self):
        cassette, _ = self._record()
        stats = cassette.stats()
        self.assertEqual(stats["pages"], 3)
        self.assertGreaterEqual(stats["searches"], 3)

    def test_replaying_under_a_different_clock_is_refused(self):
        cassette, _ = self._record()
        with self.assertRaises(ValueError) as ctx:
            record.replay(NOW.replace(hour=23), cassette)
        self.assertIn("reporting window would differ", str(ctx.exception))

    def test_missing_recording_is_loud_not_silently_empty(self):
        cassette, _ = self._record()
        provider = record.ReplayProvider("fixture", cassette)
        unseen = pipeline.Query if False else None  # keep import surface small
        from ai_signal.model import Query

        with self.assertRaises(KeyError):
            provider.search(Query("never recorded", Bucket.TOPICS), 20)

    def test_a_stale_cassette_version_is_rejected(self):
        cassette, _ = self._record()
        raw = json.loads(cassette.to_json())
        raw["version"] = 999
        with self.assertRaises(ValueError) as ctx:
            record.Cassette.from_json(json.dumps(raw))
        self.assertIn("re-record", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
