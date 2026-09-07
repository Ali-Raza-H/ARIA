"""Provider-neutral data models for web research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SearchResult:
    """A normalized result independent of the search backend."""

    title: str
    url: str
    content: str
    engine: str | None = None
    score: float | None = None
    published_date: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class WebPage:
    """Readable content retrieved from a public webpage."""

    url: str
    title: str
    text: str
    status_code: int
    content_type: str


@dataclass(frozen=True)
class ResearchSource:
    """A source identifier used to make citations stable within one turn."""

    id: str
    title: str
    url: str
    retrieved_at: datetime

    @classmethod
    def now(cls, source_id: str, title: str, url: str) -> "ResearchSource":
        return cls(source_id, title, url, datetime.now(timezone.utc))
