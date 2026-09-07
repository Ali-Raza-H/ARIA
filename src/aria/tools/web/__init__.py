"""Provider-neutral web research tools for ARIA."""

from .exceptions import (
    SearchConnectionError,
    SearchResponseError,
    SearchTimeoutError,
    UnsupportedContentTypeError,
    WebSearchError,
    WebpageBlockedError,
    WebpageFetchError,
)
from .models import ResearchSource, SearchResult, WebPage
from .provider import SearchProvider
from .searxng import SearXNGProvider
from .tools import WebToolService, register_web_tools

__all__ = [
    "ResearchSource",
    "SearchProvider",
    "SearchResult",
    "SearXNGProvider",
    "UnsupportedContentTypeError",
    "WebPage",
    "WebSearchError",
    "WebToolService",
    "WebpageBlockedError",
    "WebpageFetchError",
    "SearchConnectionError",
    "SearchResponseError",
    "SearchTimeoutError",
    "register_web_tools",
]
