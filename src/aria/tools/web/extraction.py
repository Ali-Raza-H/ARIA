"""Readable content extraction for fetched web documents."""

from __future__ import annotations

from bs4 import BeautifulSoup
import trafilatura


def extract_title(document: str) -> str:
    """Extract a best-effort page title without trusting page instructions."""
    soup = BeautifulSoup(document, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return " ".join(title.split())


def extract_readable_text(document: str, *, is_html: bool = True) -> str:
    """Extract readable text and fall back to conservative HTML text cleanup."""
    if not is_html:
        return " ".join(document.split())
    extracted = trafilatura.extract(
        document,
        include_comments=False,
        include_tables=True,
        include_links=False,
        output_format="txt",
    )
    if extracted:
        return extracted.strip()
    soup = BeautifulSoup(document, "html.parser")
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    return soup.get_text(" ", strip=True)
