"""URL and SSRF policy checks for webpage retrieval."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

from .exceptions import WebpageBlockedError

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "host.docker.internal"}


def _resolved_addresses(hostname: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebpageBlockedError(f"Could not resolve webpage host: {hostname}") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for record in records:
        try:
            addresses.add(ipaddress.ip_address(record[4][0]))
        except ValueError:
            continue
    return addresses


def _is_private_or_reserved(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def validate_public_url(url: str, allowed_hosts: Iterable[str] | None = None) -> str:
    """Validate HTTP(S) URLs and resolve hosts before a request is made.

    ``allowed_hosts`` is an explicit escape hatch for local deployments such as
    a user's own intranet or SearXNG instance. Hostnames and exact IP literals
    are matched case-insensitively; all other private destinations remain
    blocked.
    """
    if not isinstance(url, str) or not url.strip():
        raise WebpageBlockedError("url must be a non-empty HTTP(S) URL")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebpageBlockedError("Only HTTP and HTTPS webpage URLs are allowed")
    if parsed.username or parsed.password:
        raise WebpageBlockedError("URLs containing credentials are not allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    allowlist = {host.rstrip(".").lower() for host in (allowed_hosts or set())}
    is_explicitly_allowed = hostname in allowlist
    if hostname in _BLOCKED_HOSTNAMES and not is_explicitly_allowed:
        raise WebpageBlockedError(f"Webpage host is blocked: {hostname}")
    if is_explicitly_allowed:
        return url.strip()
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = {literal}
    except ValueError:
        addresses = _resolved_addresses(hostname)
    if not addresses:
        raise WebpageBlockedError(f"Could not resolve webpage host: {hostname}")
    if not is_explicitly_allowed and any(_is_private_or_reserved(address) for address in addresses):
        raise WebpageBlockedError(f"Private or local webpage host is blocked: {hostname}")
    return url.strip()
