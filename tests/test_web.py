from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from aria.config import WebConfig
from aria.tools import ToolContext, ToolRegistry
from aria.tools.web import (
    SearchConnectionError,
    SearchResult,
    SearXNGProvider,
    WebPage,
    WebToolService,
    WebpageBlockedError,
    register_web_tools,
)
from aria.tools.web.extraction import extract_readable_text
from aria.tools.web.provider import SearchProvider
from aria.tools.web.searxng import canonicalize_url, deduplicate_results
from aria.tools.web import url_policy


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://searxng.test/search")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


def test_canonicalize_url_only_removes_tracking_parameters() -> None:
    assert canonicalize_url("HTTPS://Example.COM/path/?utm_source=x&keep=1") == "https://example.com/path?keep=1"


def test_result_deduplication_discards_tracking_duplicate() -> None:
    results = [
        SearchResult("One", "https://example.com/a?utm_campaign=x", "first"),
        SearchResult("Duplicate", "https://EXAMPLE.com/a/", "second"),
        SearchResult("Two", "https://example.com/b?ref=keep", "third"),
    ]
    assert [item.title for item in deduplicate_results(results)] == ["One", "Two"]


def test_provider_normalizes_and_limits_results(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SearXNGProvider("http://searxng.test", max_retries=0)
    response = FakeResponse(
        {
            "results": [
                {"title": "  Python  ", "url": "https://python.org/", "content": " docs ", "engine": "test"},
                {"title": "", "url": "https://ignored.test", "content": "ignored"},
                {"title": "Duplicate", "url": "https://PYTHON.org", "content": "duplicate"},
                {"title": "Second", "url": "https://example.com", "content": "ok"},
            ]
        }
    )
    monkeypatch.setattr(provider, "_request", lambda params: response)

    results = provider.search("python", max_results=2)

    assert results == [SearchResult("Python", "https://python.org/", "docs", engine="test"), SearchResult("Second", "https://example.com", "ok")]


def test_provider_reports_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SearXNGProvider("http://searxng.test", max_retries=0)
    monkeypatch.setattr(provider, "_request", lambda params: FakeResponse(ValueError("bad json")))

    with pytest.raises(Exception, match="invalid JSON"):
        provider.search("test")


def test_provider_retries_transient_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SearXNGProvider("http://searxng.test", max_retries=1)
    calls = 0

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("temporary")
            return FakeResponse({"results": []})

    monkeypatch.setattr("aria.tools.web.searxng.httpx.Client", FakeClient)
    assert provider.search("test") == []
    assert calls == 2


def test_public_url_policy_blocks_private_and_allows_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_policy, "_resolved_addresses", lambda hostname: {url_policy.ipaddress.ip_address("127.0.0.1")})
    with pytest.raises(WebpageBlockedError):
        url_policy.validate_public_url("http://internal.test")
    assert url_policy.validate_public_url("http://internal.test", {"internal.test"}) == "http://internal.test"


def test_url_policy_rejects_credentials_and_non_http() -> None:
    with pytest.raises(WebpageBlockedError):
        url_policy.validate_public_url("ftp://example.com/file")
    with pytest.raises(WebpageBlockedError):
        url_policy.validate_public_url("https://user:password@example.com")


def test_extraction_removes_scripts_and_preserves_content() -> None:
    html = "<html><head><title>Page</title><script>bad()</script></head><body><h1>Heading</h1><p>Readable text.</p></body></html>"
    text = extract_readable_text(html)
    assert "Readable text." in text
    assert "bad()" not in text


def test_web_service_caches_pages_and_tracks_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    class Provider(SearchProvider):
        def search(
            self,
            query: str,
            *,
            max_results: int = 5,
            language: str = "en",
            time_range: str | None = None,
            categories: list[str] | None = None,
        ) -> list[SearchResult]:
            return []

        def health_check(self) -> bool:
            return True

    config = WebConfig(allowed_hosts=("internal.test",), page_cache_ttl=60)
    service = WebToolService(Provider(), config)
    page = WebPage("http://internal.test/page", "Page", "content", 200, "text/html")
    calls = 0

    def fetch(_url: str) -> WebPage:
        nonlocal calls
        calls += 1
        return page

    monkeypatch.setattr(service, "_fetch_with_redirects", fetch)
    assert service.page_output({"url": page.url}).startswith("WEBPAGE CONTENT")
    assert service.page_output({"url": page.url}).startswith("WEBPAGE CONTENT")
    assert calls == 1
    assert service.sources()[0].url == page.url


def test_registered_web_tools_enforce_call_limits() -> None:
    class Provider(SearchProvider):
        def search(
            self,
            query: str,
            *,
            max_results: int = 5,
            language: str = "en",
            time_range: str | None = None,
            categories: list[str] | None = None,
        ) -> list[SearchResult]:
            return [SearchResult("Result", "https://example.com", "snippet")]

        def health_check(self) -> bool:
            return True

    service = WebToolService(Provider(), WebConfig(max_search_calls=1, max_total_web_calls=1))
    service.begin_turn()
    registry = ToolRegistry()
    register_web_tools(registry, service)
    context = ToolContext(Path("."))
    assert not registry.execute("web_search", {"query": "one"}, context).is_error
    assert registry.execute("web_search", {"query": "two"}, context).is_error
