"""Fictional AI news fixtures for exercising the full pipeline offline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .. import temporal
from ..model import Bucket, Candidate, Query, SourceType
from ..providers import PageContent
from ..query_match import matches as query_matches

IST = temporal.IST


@dataclass(frozen=True)
class DemoArticle:
    url: str
    title: str
    body: str
    source_name: str
    source_type: SourceType
    hours_ago: float  # freshness relative to whenever the demo runs
    tags: str = ""  # extra matchable words not naturally in title/body

    def searchable_text(self) -> str:
        return f"{self.title} {self.body} {self.tags}"


DEMO_ARTICLES: tuple[DemoArticle, ...] = (
    DemoArticle(
        url="https://sample.ai-signal.invalid/reasoning-model",
        title="OpenAI introduces a reasoning model for long-running tasks",
        body="OpenAI announced a reasoning model that can resume multi-step work after a pause and track state between tool calls.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=2,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/agent-sandbox",
        title="Anthropic launches a sandbox for testing AI agents",
        body="Anthropic launched a sandbox environment where developers can replay agent tool calls and inspect their effects.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=4,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/multimodal-model",
        title="Google DeepMind releases a multimodal model for video analysis",
        body="Google DeepMind released a multimodal model with improved support for transcripts, scene changes, and time-coded video questions.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=7,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/inference-accelerator",
        title="NVIDIA unveils an inference accelerator for compact servers",
        body="NVIDIA announced a GPU for AI inference designed for data centers with tighter power and space limits.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=3,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/copilot-controls",
        title="Microsoft adds permission controls for coding agents",
        body="Microsoft announced that GitHub Copilot administrators can limit which tools a coding agent can access.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=6,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/model-benchmark",
        title="Researchers release a benchmark for open-weight models",
        body="An independent research group released a model benchmark and dataset for comparing open-weight models on tasks with long documents.",
        source_name="AI Signal sample",
        source_type=SourceType.ORIGINAL_REPORTING,
        hours_ago=1,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/ai-safety-guidance",
        title="Standards group publishes guidance on reporting AI incidents",
        body="An independent standards group released AI safety guidance with a common format for reporting harmful model behavior.",
        source_name="AI Signal sample",
        source_type=SourceType.ORIGINAL_REPORTING,
        hours_ago=5,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/cloud-inference",
        title="Independent cloud operators debut shared GPU capacity",
        body="A group of independent cloud operators announced pooled GPU capacity to reduce the cost of AI inference at peak demand.",
        source_name="AI Signal sample",
        source_type=SourceType.ORIGINAL_REPORTING,
        hours_ago=10,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/earlier-nvidia-sdk",
        title="NVIDIA introduces an update to its inference toolkit",
        body="NVIDIA released an inference toolkit update with expanded support for older accelerators.",
        source_name="AI Signal sample",
        source_type=SourceType.COMPANY_PRESS_RELEASE,
        hours_ago=72,
    ),
    DemoArticle(
        url="https://sample.ai-signal.invalid/optical-film",
        title="Meta Materials Inc launches a new optical film",
        body="Meta Materials Inc launched an optical film for transparent displays.",
        source_name="AI Signal sample",
        source_type=SourceType.ORIGINAL_REPORTING,
        hours_ago=8,
    ),
)


class DemoSearchProvider:
    """Matches a watchlist query against the offline sample articles."""

    name = "demo"

    def search(self, query: Query, limit: int = 20) -> list[Candidate]:
        out: list[Candidate] = []
        for article in DEMO_ARTICLES:
            if not query_matches(article.searchable_text(), query.text):
                continue
            out.append(
                Candidate(
                    url=article.url,
                    title=article.title,
                    bucket=query.bucket,
                    snippet=article.body[:180],
                    found_by=[query],
                )
            )
            if len(out) >= limit:
                break
        return out


@dataclass
class DemoFetcher:
    """A `PageFetcher` serving the same fixture set, with freshness computed
    relative to an explicit `reference_time` -- deliberately NOT the real
    wall clock read internally. An earlier version called `datetime.now()`
    directly, on the theory that "articles look fresh whenever the demo
    runs" was a harmless convenience; it quietly broke every test pinned to
    a fixed `now` the moment a single calendar day passed in the real
    world, because `pipeline.run`'s window was anchored to the test's fixed
    clock while this fetcher's timestamps kept marching forward with real
    time -- exactly the hidden-clock nondeterminism this package is
    supposed to refuse everywhere else. Callers pass the *same* `now` they
    give `pipeline.run` (see `local_server.py`), so "looks fresh whenever
    you click Force refresh" still holds for the live server while the
    test suite stays pinned and reproducible.
    """

    reference_time: datetime
    articles_by_url: dict = field(
        default_factory=lambda: {a.url: a for a in DEMO_ARTICLES}
    )

    def fetch(self, url: str) -> PageContent:
        article = self.articles_by_url.get(url)
        if article is None:
            return PageContent(body="", ok=False, error="not in the demo fixture set")
        published_at = self.reference_time - timedelta(hours=article.hours_ago)
        return PageContent(
            body=article.body,
            published=temporal.DateFinding(
                published_at, date_only=False, provenance="demo fixture"
            ),
            source_type=article.source_type,
            source_name=article.source_name,
            ok=True,
        )
