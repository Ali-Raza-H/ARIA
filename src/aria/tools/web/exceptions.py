"""Errors raised by the web research subsystem."""


class WebSearchError(Exception):
    """Base class for expected web research failures."""


class SearchConnectionError(WebSearchError):
    """The search provider could not be reached."""


class SearchTimeoutError(WebSearchError):
    """A search or page request exceeded its timeout."""


class SearchResponseError(WebSearchError):
    """The provider returned an invalid or unusable response."""


class WebpageFetchError(WebSearchError):
    """A webpage could not be fetched."""


class WebpageBlockedError(WebSearchError):
    """A URL was rejected by SSRF or URL policy checks."""


class UnsupportedContentTypeError(WebSearchError):
    """The fetched resource is not a supported text document."""
