"""Destination blocklist.

A static, operator-owned list of domains the service refuses to shorten. It cannot
catch newly registered malicious domains; that needs an external reputation source.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

MATCH_MODES = ("exact_host", "host_and_subdomains")
DEFAULT_MATCH_MODE = "@@MATCH_MODE@@"

# Demo entries on the reserved .example TLD; real deployments extend the list through
# SHORTENER_BLOCKLIST or SHORTENER_BLOCKLIST_FILE.
BUILTIN_BLOCKLIST = frozenset({"malware.example", "phishing.example"})


class BlockedDestination(Exception):
    """The destination is on the blocklist."""

    def __init__(self, reason: str = "destination is blocked") -> None:
        super().__init__(reason)
        self.reason = reason


def _normalise(host: str) -> str:
    return host.strip().lower().rstrip(".")


class Blocklist:
    def __init__(self, domains, match_mode: str = DEFAULT_MATCH_MODE) -> None:
        if match_mode not in MATCH_MODES:
            raise ValueError(f"unknown match mode {match_mode!r}")
        self._domains = frozenset(_normalise(d) for d in domains if d and d.strip())
        self._match = match_mode

    @classmethod
    def from_settings(cls, settings) -> "Blocklist":
        domains = set(BUILTIN_BLOCKLIST)
        domains.update(settings.blocklist_extra)
        if settings.blocklist_file:
            # A configured-but-unreadable file is a misconfiguration: fail at startup, not open.
            for line in Path(settings.blocklist_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.add(line)
        return cls(domains)

    def __len__(self) -> int:
        return len(self._domains)

    def is_blocked(self, url: str) -> bool:
        host = urlsplit(url).hostname
        if not host:
            return False
        host = _normalise(host)
        if host in self._domains:
            return True
        if self._match == "host_and_subdomains":
            parts = host.split(".")
            return any(".".join(parts[i:]) in self._domains for i in range(1, len(parts)))
        return False
