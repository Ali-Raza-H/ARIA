"""SearXNG JSON API provider."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ...logging.setup import log_error, log_info
from .exceptions import SearchConnectionError, SearchResponseError, SearchTimeoutError
from .models import SearchResult
from .provider import SearchProvider

_TRACKING_PARAMETERS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def canonicalize_url(url: str) -> str:
    """Canonicalize comparison-only URL details without changing page identity."""
    parsed = urlparse(url.strip())
    query = "&".join(
        part for part in parsed.query.split("&")
        if part and part.split("=", 1)[0].lower() not in _TRACKING_PARAMETERS
    )
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        key = canonicalize_url(result.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique


class SearXNGProvider(SearchProvider):
    """Search a user-managed local SearXNG instance over its JSON endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        user_agent: str = "ARIA/1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.user_agent = user_agent

    def _request(self, params: dict[str, str]) -> httpx.Response:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": self.user_agent}) as client:
                    response = client.get(f"{self.base_url}/search", params=params)
                if response.status_code >= 500 and attempt + 1 < attempts:
                    time.sleep(0.25 * (2**attempt))
                    continue
                response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise SearchTimeoutError("SearXNG request timed out") from exc
            except httpx.HTTPStatusError as exc:
                raise SearchConnectionError(f"SearXNG returned HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise SearchConnectionError("Unable to connect to SearXNG") from exc
        raise SearchConnectionError("Unable to connect to SearXNG")

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        language: str = "en",
        time_range: str | None = None,
        categories: list[str] | None = None,
    ) -> list[SearchResult]:
        if not isinstance(query, str) or not query.strip():
            raise SearchResponseError("Search query must be non-empty")
        if max_results <= 0:
            raise SearchResponseError("max_results must be positive")
        params = {"q": query.strip(), "format": "json", "language": language}
        if time_range:
            params["time_range"] = time_range
        if categories:
            params["categories"] = ",".join(categories)
        started = time.monotonic()
        response = self._request(params)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SearchResponseError("SearXNG returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise SearchResponseError("SearXNG returned an invalid result payload")
        normalized: list[SearchResult] = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            content = item.get("content", "")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.strip():
                continue
            parsed = urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            normalized.append(
                SearchResult(
                    title=" ".join(title.split()),
                    url=url.strip(),
                    content=str(content or "").strip()[:2000],
                    engine=str(item["engine"]) if item.get("engine") else None,
                    score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                    published_date=str(item["publishedDate"]) if item.get("publishedDate") else None,
                    category=str(item["category"]) if item.get("category") else None,
                )
            )
        results = deduplicate_results(normalized)[:max_results]
        log_info(f'web.search query="{query.strip()[:160]}" results={len(results)} duration={time.monotonic() - started:.2f}s')
        return results

    def health_check(self) -> bool:
        try:
            self._request({"q": "test", "format": "json", "language": "en"})
            return True
        except Exception as exc:
            log_error(f"web.search health check failed: {type(exc).__name__}: {exc}")
            return False
