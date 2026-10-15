"""Destination URL validation.

The service only redirects, it never fetches a destination, so blocking private
hosts is abuse hygiene (no short links that point at an internal address), not
an SSRF defence.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


class InvalidURL(ValueError):
    """Raised when a destination URL is rejected. `reason` is safe to show to clients."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def validate_url(raw: object, max_length: int = 2048) -> str:
    """Return the normalised URL or raise InvalidURL."""
    if not isinstance(raw, str):
        raise InvalidURL("url must be a string")
    url = raw.strip()
    if not url:
        raise InvalidURL("url is empty")
    if len(url) > max_length:
        raise InvalidURL(f"url is longer than {max_length} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise InvalidURL("url contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError as exc:
        raise InvalidURL("url is malformed") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidURL("only http and https URLs are allowed")
    host = parts.hostname
    if not host:
        raise InvalidURL("url has no host")
    if parts.username or parts.password:
        raise InvalidURL("credentials in the URL are not allowed")
    if _is_private_host(host):
        raise InvalidURL("private and loopback hosts are not allowed")
    return url
