"""Runtime settings for the URL shortener service."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    db_path: str = "shortener.db"
    base_url: str = "http://localhost:8000"
    code_length: int = 7
    rate_limit_per_minute: int = 30
    max_url_length: int = 2048
    # Only honour X-Forwarded-For when running behind a proxy we control.
    trust_proxy_headers: bool = False
    # Destination blocklist: extra domains and/or a file with one domain per line.
    blocklist_extra: tuple[str, ...] = ()
    blocklist_file: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.getenv("SHORTENER_DB", cls.db_path),
            base_url=os.getenv("PUBLIC_BASE_URL", cls.base_url).rstrip("/"),
            code_length=int(os.getenv("SHORTENER_CODE_LENGTH", cls.code_length)),
            rate_limit_per_minute=int(os.getenv("SHORTENER_RATE_LIMIT", cls.rate_limit_per_minute)),
            trust_proxy_headers=os.getenv("TRUST_PROXY", "0") == "1",
            blocklist_extra=_split(os.getenv("SHORTENER_BLOCKLIST")),
            blocklist_file=os.getenv("SHORTENER_BLOCKLIST_FILE") or None,
        )
