"""Search-provider abstraction for replacing SearXNG later."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import SearchResult


class SearchProvider(ABC):
    """Backend-neutral search contract."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        language: str = "en",
        time_range: str | None = None,
        categories: list[str] | None = None,
    ) -> list[SearchResult]:
        """Search for normalized results."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return whether the provider is reachable and exposes JSON search."""
        raise NotImplementedError
