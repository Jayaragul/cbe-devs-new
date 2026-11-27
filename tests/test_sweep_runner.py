"""Construction tests for the unattended Cloud Run Job entrypoint."""

import unittest

import sweep_runner
from ai_signal.live.google_news import GoogleNewsPageFetcher, GoogleNewsSearchProvider
from ai_signal.live.http_fetcher import HttpPageFetcher


class TestBuildWebSources(unittest.TestCase):
    def test_default_stack_uses_plain_http_fetcher(self):
        providers, fetcher, notes = sweep_runner.build_web_sources({})
        self.assertEqual([p.name for p in providers], ["rss"])
        self.assertIsInstance(fetcher, HttpPageFetcher)
        self.assertEqual(notes, [])

    def test_google_mode_wraps_an_existing_http_fetcher(self):
        providers, fetcher, notes = sweep_runner.build_web_sources(
            {"AI_SIGNAL_GOOGLE_NEWS": "1"}
        )
        self.assertIsInstance(providers[1], GoogleNewsSearchProvider)
        self.assertIsInstance(fetcher, GoogleNewsPageFetcher)
        self.assertIsInstance(fetcher.primary, HttpPageFetcher)
        self.assertIn("Google News RSS: enabled", notes[0])


if __name__ == "__main__":
    unittest.main()
