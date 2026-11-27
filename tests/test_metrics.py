"""Tests for ai_signal.metrics -- no network.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.metrics import MetricsCollector, PhaseMetrics, extract_status


class TestExtractStatus(unittest.TestCase):
    def test_reads_the_code_out_of_an_http_fetcher_style_message(self):
        self.assertEqual(extract_status("HTTP 503: Service Unavailable"), 503)

    def test_reads_the_code_out_of_a_google_news_style_message(self):
        self.assertEqual(extract_status("failed: HTTP 429 Too Many Requests"), 429)

    def test_a_message_with_no_status_code_returns_none(self):
        self.assertIsNone(extract_status("could not connect: timed out"))

    def test_none_and_empty_string_are_handled(self):
        self.assertIsNone(extract_status(None))
        self.assertIsNone(extract_status(""))


class TestPhaseMetricsP95(unittest.TestCase):
    def test_p95_of_no_samples_is_none(self):
        self.assertIsNone(PhaseMetrics(name="x").p95_latency_ms())

    def test_p95_of_one_sample_is_that_sample(self):
        pm = PhaseMetrics(name="x")
        pm._latencies_ms = [42.0]
        self.assertEqual(pm.p95_latency_ms(), 42.0)

    def test_p95_of_twenty_even_samples_is_the_nineteenth(self):
        """1..20ms: the 95th percentile by nearest-rank is the 19th smallest."""
        pm = PhaseMetrics(name="x")
        pm._latencies_ms = list(range(1, 21))
        self.assertEqual(pm.p95_latency_ms(), 19)

    def test_p95_is_insensitive_to_input_order(self):
        pm = PhaseMetrics(name="x")
        pm._latencies_ms = [50, 10, 30, 20, 40]
        self.assertEqual(pm.p95_latency_ms(), 50)


class TestMetricsCollector(unittest.TestCase):
    def test_a_fresh_phase_has_no_entry_until_recorded(self):
        self.assertEqual(MetricsCollector().summary(), [])

    def test_records_success_and_failure_counts(self):
        m = MetricsCollector()
        m.record("fetch", latency_ms=10, ok=True)
        m.record("fetch", latency_ms=20, ok=False, error="HTTP 503: x")
        m.record("fetch", latency_ms=30, ok=False, error="HTTP 429: x")
        (summary,) = m.summary()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["failure"], 2)
        self.assertEqual(summary["status_503"], 1)
        self.assertEqual(summary["status_429"], 1)

    def test_phases_are_tracked_independently(self):
        m = MetricsCollector()
        m.record("discovery", latency_ms=5, ok=True)
        m.record("fetch", latency_ms=15, ok=True)
        names = {row["phase"] for row in m.summary()}
        self.assertEqual(names, {"discovery", "fetch"})

    def test_phase_context_manager_accumulates_duration(self):
        m = MetricsCollector()
        with m.phase("fetch"):
            m.record("fetch", latency_ms=1, ok=True)
        (summary,) = m.summary()
        self.assertGreaterEqual(summary["duration_s"], 0.0)

    def test_concurrent_record_calls_do_not_lose_updates(self):
        """The whole point of the lock: hammer .record() from many threads
        at once and confirm every single call is counted, not just most."""
        m = MetricsCollector()
        n_threads, calls_per_thread = 20, 50

        def hammer():
            for _ in range(calls_per_thread):
                m.record("fetch", latency_ms=1.0, ok=True)

        threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        (summary,) = m.summary()
        self.assertEqual(summary["requests"], n_threads * calls_per_thread)
        self.assertEqual(summary["success"], n_threads * calls_per_thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
